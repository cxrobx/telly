"""Tests for plexbot memory-aware recs, recent-watch awareness, and What's new fresh releases.

Made-up people, shows, memories and ids only: the repo is public.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from telly import db, llm, memory, ratings, recs, requester
from telly.clients.tmdb import TMDBClient
from telly.config import get_settings
from telly.crypto import COOKIE, make_session
from telly.models import (Feedback, Follow, IdentityLink, LibraryItem, Memory, NewReleasePicks,
                          Play, Recommendation, Title, TrendingPick, User, WatchEntry, utcnow)

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
TODAY = NOW.date()
ALICE, BOB = 1001, 2002
SECRET = "test-recs-shared-secret"


def item(tid, title, media_type="tv", votes=500, rating=8.0, released="2026-10-01",
         genres=(18,), **kw):
    key = "name" if media_type == "tv" else "title"
    dkey = "first_air_date" if media_type == "tv" else "release_date"
    return {"id": tid, key: title, "vote_count": votes, "vote_average": rating,
            dkey: released, "genre_ids": list(genres), "overview": f"{title} synopsis", **kw}


class RichFakeTMDB:
    def __init__(self, by_seed=None, trending=None, new_rel=None, airing=None, by_genre=None):
        self.by_seed = by_seed or {}
        self._trending = trending or {}
        self._new_rel = new_rel or {}
        self._airing = airing or []
        self._by_genre = by_genre or {}
        self.genre_calls = []

    def recommendations(self, media_type, tmdb_id):
        return self.by_seed.get((media_type, tmdb_id), [])

    def trending(self, media_type, window="week"):
        return self._trending.get(media_type, [])

    def new_releases(self, media_type, since, until, page=1):
        return self._new_rel.get((media_type, page), [])

    def airing_recently(self, since, until, page=1):
        return self._airing if page == 1 else []

    def discover_genre(self, media_type, genre_id, page=1):
        self.genre_calls.append((media_type, genre_id, page))
        return self._by_genre.get((media_type, genre_id, page), [])

    def anime_discovery(self, media_type, page=1):
        return []


@pytest.fixture
def two_users(s):
    s.add_all([
        User(plex_id=ALICE, pms_account_id=1, username="alice", overseerr_id=11),
        User(plex_id=BOB, pms_account_id=2, username="bob", overseerr_id=22),
    ])
    s.commit()
    return s


# ── Plexbot interest profile (memories) ───────────────────────────────────


def test_profile_memories_scoping_kinds_and_expiry(two_users):
    s = two_users
    memory.save(s, ALICE, "Loves slow-burn sci-fi and espionage thrillers", "preference", "chat")
    memory.save(s, ALICE, "Watching Space Opera S2 this month", "plan", "chat")
    memory.save(s, ALICE, "Living room Apple TV outputs 4K HDR", "setup", "chat")
    memory.save(s, BOB, "Huge horror fan", "preference", "chat")
    # Add an expired plan for Alice directly
    s.add(Memory(
        plex_id=ALICE, kind="plan", text="Old expired plan", source="chat",
        updated_at=utcnow() - timedelta(days=90),
        expires_at=utcnow() - timedelta(days=30),
    ))
    s.commit()

    alice_mems = recs.profile_memories(s, ALICE)
    texts = [m["text"] for m in alice_mems]
    assert "Loves slow-burn sci-fi and espionage thrillers" in texts
    assert "Watching Space Opera S2 this month" in texts
    assert "Living room Apple TV outputs 4K HDR" not in texts  # setup excluded
    assert "Old expired plan" not in texts                    # expired plan excluded
    assert "Huge horror fan" not in texts                     # Bob's memory excluded


def test_memory_profile_boosts_liked_genres_and_dampens_avoidances():
    mems = [
        {"kind": "preference", "text": "Loves sci-fi and Japanese anime; no horror or slasher movies"},
        {"kind": "plan", "text": "Looking for a tight mystery thriller"},
    ]
    prof = recs.parse_memory_profile(mems)
    assert 10765 in prof.liked_tv_genres and 878 in prof.liked_movie_genres
    assert prof.likes_anime is True
    assert 27 in prof.disliked_genres  # horror

    scifi = recs.Candidate(1, "movie", "Starfall", 2026, "", None, None, 8.0, genre_ids=[878])
    horror = recs.Candidate(2, "movie", "Crypt Night", 2026, "", None, None, 7.5, genre_ids=[27])
    neutral = recs.Candidate(3, "movie", "Quiet Town", 2026, "", None, None, 7.5, genre_ids=[37])

    assert recs.memory_fit(scifi, prof) > 1.1
    assert recs.memory_fit(horror, prof) < 0.85
    assert recs.memory_fit(neutral, prof) == 1.0
    assert 0.6 <= recs.memory_fit(horror, prof) <= 1.35
    assert 0.6 <= recs.memory_fit(scifi, prof) <= 1.35


def test_memory_only_member_gets_recommendations_from_genre_discovery(two_users, monkeypatch):
    s = two_users
    monkeypatch.setattr(llm, "available", lambda: False)
    memory.save(s, ALICE, "Loves sci-fi and cyberpunk shows", "preference", "told")
    s.commit()

    tmdb = RichFakeTMDB(
        trending={"tv": [item(50, "Popular Romcom", "tv", genres=(35, 10749))]},
        by_genre={
            ("tv", 10765, 1): [item(51, "Neon Orbit", "tv", genres=(10765, 18), rating=8.4)],
            ("movie", 878, 1): [item(52, "Solar Drift", "movie", genres=(878,), rating=8.1)],
        },
    )
    count = recs.build_for(s, tmdb, ALICE, tmdb._trending)
    assert count >= 2
    picks = recs.for_user(s, ALICE)
    titles = [p["title"] for p in picks]
    assert "Neon Orbit" in titles and "Solar Drift" in titles
    assert titles.index("Neon Orbit") < titles.index("Popular Romcom")
    # Reason references their saved interest profile
    neon = next(p for p in picks if p["title"] == "Neon Orbit")
    assert "sci-fi" in neon["reason"].lower()


# ── Recent watch awareness ────────────────────────────────────────────────


def test_recent_watches_are_never_crowded_out_by_many_older_favorites(two_users):
    s = two_users
    # 15 older Loved titles (more than MAX_SEEDS=12)
    for i in range(1, 16):
        s.add(Title(tmdb_id=i, media_type="tv", name=f"Classic {i}"))
        s.add(WatchEntry(plex_id=ALICE, tmdb_id=i, media_type="tv", rating=2, status="finished",
                         updated_at=NOW - timedelta(days=100 + i)))
    # 2 recent Plex watches (within 30 days) + 1 currently watching manual entry + 1 dropped recent
    s.add_all([
        Title(tmdb_id=901, media_type="tv", name="Fresh Binge"),
        Title(tmdb_id=902, media_type="movie", name="Last Night Film"),
        Title(tmdb_id=903, media_type="tv", name="Manual Watching"),
        Title(tmdb_id=904, media_type="tv", name="Dropped Recent"),
    ])
    s.add_all([
        Play(history_key="h1", plex_id=ALICE, media_type="tv", tmdb_id=901, rating_key="rk1",
             season=1, episode=1, viewed_at=NOW - timedelta(days=1)),
        Play(history_key="h2", plex_id=ALICE, media_type="movie", tmdb_id=902, rating_key="rk2",
             viewed_at=NOW - timedelta(days=2)),
        Play(history_key="h3", plex_id=ALICE, media_type="tv", tmdb_id=904, rating_key="rk4",
             season=1, episode=1, viewed_at=NOW - timedelta(days=1)),
        WatchEntry(plex_id=ALICE, tmdb_id=903, media_type="tv", status="watching",
                   updated_at=NOW - timedelta(days=3)),
        WatchEntry(plex_id=ALICE, tmdb_id=904, media_type="tv", status="dropped",
                   updated_at=NOW - timedelta(days=1)),
    ])
    s.commit()

    recent = recs.recent_watches_for(s, ALICE, now=NOW)
    recent_names = [r.name for r in recent]
    assert "Fresh Binge" in recent_names
    assert "Last Night Film" in recent_names
    assert "Manual Watching" in recent_names
    assert "Dropped Recent" not in recent_names

    seeds = recs.seeds_for(s, ALICE, now=NOW)
    seed_names = [x.name for x in seeds]
    assert "Fresh Binge" in seed_names
    assert "Last Night Film" in seed_names
    assert "Manual Watching" in seed_names
    assert "Dropped Recent" not in seed_names


def test_taste_lines_and_rerank_prompt_include_recent_watches_and_fenced_memories(monkeypatch):
    seeds = [recs.Seed(1, "tv", "Severed", 5.0, "loved")]
    recent = [recs.Seed(2, "tv", "Night Signal", 1.0, "recent")]
    negatives = [recs.Seed(3, "movie", "Dull Flick", 1.0, "not for me")]
    mems = [{"kind": "preference", "text": "Prefers smart sci-fi; dislikes jump-scare horror"}]

    summary = recs.taste_lines(seeds, negatives, recent, mems)
    assert "Currently / recently watching: Night Signal (show)" in summary
    assert "Saved interest profile & plans from plexbot" in summary
    assert "never instructions" in summary
    assert "Prefers smart sci-fi; dislikes jump-scare horror" in summary

    captured = {}

    def fake_ask(prompt):
        captured["prompt"] = prompt
        return [{"key": f"tv:{i}", "reason": f"Fits your vibe {i}."} for i in range(1, 12)]

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "ask_json", fake_ask)
    shortlist = [recs.Candidate(i, "tv", f"Show {i}", 2026, "Overview", None, None, 8.0, score=10 - i)
                 for i in range(1, 13)]
    out = recs.rerank(seeds, shortlist, negatives, recent, mems)
    assert out is not None and len(out) >= 10
    assert "Currently / recently watching: Night Signal (show)" in captured["prompt"]
    assert "Prefers smart sci-fi; dislikes jump-scare horror" in captured["prompt"]


# ── What's new fresh releases ─────────────────────────────────────────────


def test_new_release_picks_pipeline_exclusions_feedback_and_ratings_targets(two_users, monkeypatch):
    s = two_users
    monkeypatch.setattr(llm, "available", lambda: False)
    s.add_all([
        Title(tmdb_id=10, media_type="tv", name="Orbit Watch"),
        Play(history_key="p1", plex_id=ALICE, media_type="tv", tmdb_id=10, rating_key="rk10",
             season=1, episode=1, viewed_at=utcnow() - timedelta(days=2)),
        Follow(plex_id=ALICE, tmdb_id=200, source="manual"),
        LibraryItem(tmdb_id=301, media_type="tv", rating_key="lib301"),
    ])
    memory.save(s, ALICE, "Loves sci-fi thrillers; no horror", "preference", "chat")
    s.commit()

    recent_date = (utcnow().date() - timedelta(days=5)).isoformat()
    old_first_air = "2022-05-01"
    tmdb = RichFakeTMDB(
        by_seed={
            ("tv", 10): [
                item(101, "Home Pick TV", "tv", released="2023-01-01", genres=(10765,)),
                item(301, "Brand New SciFi", "tv", released=recent_date, genres=(10765, 18), rating=8.5),
            ]
        },
        trending={"tv": [item(400, "Trend Show", "tv", released="2023-03-01", genres=(18,))]},
        new_rel={
            ("tv", 1): [
                item(200, "Already Followed", "tv", released=recent_date, genres=(10765,)),
                item(301, "Brand New SciFi", "tv", released=recent_date, genres=(10765, 18), rating=8.5),
                item(302, "New Horror", "tv", released=recent_date, genres=(9648, 27), rating=7.0),
            ],
            ("movie", 1): [
                item(303, "Fresh Thriller Film", "movie", released=recent_date, genres=(53, 878), rating=8.2),
            ],
        },
        airing=[
            item(304, "Returning Hit", "tv", released=old_first_air, genres=(10765,), votes=400, rating=8.3),
        ],
    )

    recs.build_for(s, tmdb, ALICE, tmdb._trending)
    s.commit()

    # Stored in NewReleasePicks and included in ratings.targets()
    row = s.get(NewReleasePicks, ALICE)
    assert row is not None and len(row.picks) >= 3
    rating_keys = set(ratings.targets(s))
    assert (303, "movie") in rating_keys
    assert (304, "tv") in rating_keys

    # whats_new_picks_for_user returns Alice's picks, skips followed (200) and Home Pick (101)
    picks = recs.whats_new_picks_for_user(s, ALICE)
    by_id = {(p["tmdb_id"], p["media_type"]): p for p in picks}
    assert (200, "tv") not in by_id
    assert (101, "tv") not in by_id
    assert (303, "movie") in by_id and by_id[(303, "movie")]["kind_label"] == "New movie"
    assert (304, "tv") in by_id and by_id[(304, "tv")]["kind_label"] == "New season"
    # Bob does not see Alice's stored picks
    assert recs.whats_new_picks_for_user(s, BOB) == []

    # Thumbs down immediately drops from NewReleasePicks and saves the title name
    recs.give_feedback(s, ALICE, 303, "movie", -1)
    s.commit()
    after = recs.whats_new_picks_for_user(s, ALICE)
    assert all(p["tmdb_id"] != 303 for p in after)
    fb = s.get(Feedback, (ALICE, 303, "movie"))
    assert fb is not None and fb.title == "Fresh Thriller Film"


def test_whats_new_picks_falls_back_to_trending_before_first_build(two_users):
    s = two_users
    s.add(TrendingPick(plex_id=ALICE, tmdb_id=701, media_type="movie", rank=1, score=2.0,
                       title="Hot Fallback", year=2026, reason="#1 in movies this week"))
    s.commit()
    picks = recs.whats_new_picks_for_user(s, ALICE)
    assert len(picks) == 1
    assert picks[0]["title"] == "Hot Fallback"
    assert picks[0]["kind_label"] == "Trending now"


def test_tmdb_discovery_methods_are_cached_per_client():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={"results": [{"id": 1, "name": "Cached"}]})

    http = httpx.Client(base_url="https://api.themoviedb.org/3", transport=httpx.MockTransport(handler))
    client = TMDBClient(token="fake-key", client=http)
    d1, d2 = date(2026, 9, 1), date(2026, 10, 15)

    assert client.new_releases("tv", d1, d2, page=1) == [{"id": 1, "name": "Cached"}]
    assert client.new_releases("tv", d1, d2, page=1) == [{"id": 1, "name": "Cached"}]
    assert client.airing_recently(d1, d2, page=1) == [{"id": 1, "name": "Cached"}]
    assert client.airing_recently(d1, d2, page=1) == [{"id": 1, "name": "Cached"}]
    assert client.discover_genre("movie", 878, page=1) == [{"id": 1, "name": "Cached"}]
    assert client.discover_genre("movie", 878, page=1) == [{"id": 1, "name": "Cached"}]
    assert len(calls) == 3  # each public endpoint hit exactly once


def test_rerank_new_releases_uses_llm_and_falls_back_cleanly(monkeypatch):
    shortlist = [
        recs.Candidate(i, "tv" if i % 2 else "movie", f"Fresh {i}", 2026, "Premise", None, None,
                       8.0, score=10 - i, new_kind="new_series" if i % 2 else "new_movie")
        for i in range(1, 9)
    ]
    monkeypatch.setattr(llm, "available", lambda: True)
    good = [{"key": f"{'tv' if i % 2 else 'movie'}:{i}", "reason": f"Matches your taste {i}."}
            for i in range(8, 0, -1)]
    monkeypatch.setattr(llm, "ask_json", lambda prompt: good)
    picked = recs.rerank_new_releases([], shortlist, recent=[recs.Seed(1, "tv", "Orbit", 1.0)],
                                      memories=[{"kind": "preference", "text": "Loves sci-fi"}])
    assert picked is not None and picked[0][0].tmdb_id == 8 and picked[0][1] == "Matches your taste 8."

    monkeypatch.setattr(llm, "ask_json", lambda prompt: [{"key": "tv:999", "reason": "bogus"}])
    assert recs.rerank_new_releases([], shortlist) is None


# ── Web API & MCP integration ─────────────────────────────────────────────


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("TELLY_DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("TELLY_SHARED_SECRET", SECRET)
    monkeypatch.setenv("TELLY_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("TELLY_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TELLY_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TELLY_LLM_ENABLED", "false")
    monkeypatch.setenv("TELLY_PLEXBOT_AUTH_SECRET", "")
    get_settings.cache_clear()
    db._engine = db._factory = None
    db.Base.metadata.create_all(db.get_engine())
    with db.session_scope() as s:
        s.add_all([
            User(plex_id=ALICE, pms_account_id=1, username="alice", overseerr_id=11),
            User(plex_id=BOB, pms_account_id=2, username="bob", overseerr_id=22),
        ])
        s.flush()
        s.add_all([
            IdentityLink(surface="discord", external_id="disc-alice", plex_id=ALICE, source="link"),
            IdentityLink(surface="discord", external_id="disc-bob", plex_id=BOB, source="link"),
            NewReleasePicks(plex_id=ALICE, picks=[
                {"tmdb_id": 801, "media_type": "tv", "title": "Alice New Show", "year": 2026,
                 "released": "2026-10-10", "new_kind": "new_series", "kind_label": "New series",
                 "poster_path": "/a.jpg", "backdrop_path": None, "overview": "", "because": [],
                 "trending": True, "anime": False, "in_library": False, "reason": "New series · fits your taste."},
            ]),
            TrendingPick(plex_id=ALICE, tmdb_id=801, media_type="tv", rank=1, score=2.0,
                         title="Alice New Show", year=2026, reason="#1 in TV this week"),
            TrendingPick(plex_id=ALICE, tmdb_id=802, media_type="movie", rank=2, score=1.5,
                         title="Other Trending Film", year=2026, reason="#2 in movies this week"),
        ])
    from telly.app import create_app
    with TestClient(create_app(), base_url="http://localhost:8940") as c:
        yield c
    get_settings.cache_clear()
    db._engine = db._factory = None


def _mcp_call(client, discord_id, tool, **args):
    token = requester.mint("discord", discord_id, secret=SECRET)
    r = client.post("/mcp", headers={"Accept": "application/json, text/event-stream",
                                     "Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}})
    assert r.status_code == 200, r.text
    return json.loads(r.json()["result"]["content"][0]["text"])


def test_home_whats_new_and_mcp_expose_scoped_new_releases(web_client):
    web_client.cookies.set(COOKIE, make_session(ALICE))
    home = web_client.get("/api/home").json()
    assert [p["title"] for p in home["whats_new_picks"]] == ["Alice New Show"]
    # Trending deduplicates against whats_new_picks without emptying itself
    assert [p["title"] for p in home["trending"]] == ["Other Trending Film"]

    wn = web_client.get("/api/whats-new").json()
    assert wn["events"] == []
    assert [p["title"] for p in wn["picks"]] == ["Alice New Show"]

    # MCP whats_new returns new_for_you scoped to caller
    alice_mcp = _mcp_call(web_client, "disc-alice", "whats_new")
    assert [p["title"] for p in alice_mcp["new_for_you"]] == ["Alice New Show"]
    bob_mcp = _mcp_call(web_client, "disc-bob", "whats_new")
    assert bob_mcp["new_for_you"] == []


def test_saving_preference_memory_clears_needs_taste_and_triggers_warm(web_client, monkeypatch):
    warmed = []
    monkeypatch.setattr(recs, "warm", lambda pid: warmed.append(pid) or True)
    monkeypatch.setattr(recs, "warm_in_background", lambda pid: warmed.append(pid))

    web_client.cookies.set(COOKIE, make_session(BOB))
    assert web_client.get("/api/home").json()["needs_taste"] is True

    # Setup memory does NOT warm picks or clear needs_taste
    r = web_client.post("/api/memories", json={"text": "Bedroom TV is 1080p", "kind": "setup"})
    assert r.status_code == 200
    assert warmed == []
    assert web_client.get("/api/home").json()["needs_taste"] is True

    # Preference memory clears needs_taste and schedules warm
    r = web_client.post("/api/memories", json={"text": "Loves sci-fi and heist thrillers", "kind": "preference"})
    assert r.status_code == 200
    assert warmed == [BOB]
    assert web_client.get("/api/home").json()["needs_taste"] is False

    # MCP remember also triggers warm_in_background for a preference memory when needed
    warmed.clear()
    res = _mcp_call(web_client, "disc-bob", "remember", text="Enjoys prestige drama", kind="preference")
    assert "saved" in res
    assert warmed == [BOB]
