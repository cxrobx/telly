"""Recommendations and news renewals."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from telly import llm, recs, renewals
from telly.models import (Event, Feedback, Follow, LibraryItem, Play, Recommendation,
                          RenewalCheck, Title, User, WatchlistItem, utcnow)
from telly.shows import describe, upcoming

NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)
A, B = 111, 222


def rec(id_, title, votes=500, rating=7.5, **kw):
    return {"id": id_, "name": title, "vote_count": votes, "vote_average": rating,
            "first_air_date": "2024-01-01", "overview": f"{title} overview", **kw}


class FakeTMDB:
    def __init__(self, by_seed, trending=None):
        self.by_seed, self._trending = by_seed, trending or {}

    def recommendations(self, media_type, tmdb_id):
        return self.by_seed.get((media_type, tmdb_id), [])

    def trending(self, media_type, window="week"):
        return self._trending.get(media_type, [])


@pytest.fixture
def people(s):
    s.add_all([User(plex_id=A, pms_account_id=1, username="a", overseerr_id=1),
               User(plex_id=B, pms_account_id=B, username="b", overseerr_id=2)])
    s.flush()
    s.add_all([Title(tmdb_id=1, media_type="tv", name="Recent Show"),
               Title(tmdb_id=2, media_type="tv", name="Old Show"),
               Title(tmdb_id=3, media_type="movie", name="A Movie")])
    hk = 0

    def play(pid, tmdb, mt, n, days_ago):
        nonlocal hk
        for e in range(n):
            hk += 1
            s.add(Play(history_key=str(hk), plex_id=pid, media_type=mt, tmdb_id=tmdb, rating_key="x",
                       season=1 if mt == "tv" else None, episode=e + 1 if mt == "tv" else None,
                       viewed_at=NOW - timedelta(days=days_ago)))
    play(A, 1, "tv", 8, 3)
    play(A, 2, "tv", 8, 700)
    play(A, 3, "movie", 1, 10)
    play(B, 2, "tv", 2, 5)
    s.commit()
    return s


def test_recent_viewing_outweighs_old_viewing(people):
    seeds = recs.seeds_for(people, A, now=NOW)
    names = [x.name for x in seeds]
    assert names.index("Recent Show") < names.index("Old Show")


def test_thumbs_up_becomes_a_top_seed(people):
    people.add(Feedback(plex_id=A, tmdb_id=77, media_type="tv", value=1, title="Loved It"))
    people.commit()
    assert recs.seeds_for(people, A, now=NOW)[0].name == "Loved It"


def test_candidates_skip_watched_followed_watchlisted_disliked_and_obscure(people):
    people.add_all([Follow(plex_id=A, tmdb_id=11, source="manual"),
                    WatchlistItem(plex_id=A, tmdb_id=12, media_type="tv", title="W"),
                    Feedback(plex_id=A, tmdb_id=13, media_type="tv", value=-1)])
    people.commit()
    tmdb = FakeTMDB({("tv", 1): [rec(2, "Old Show"), rec(11, "Followed"), rec(12, "Watchlisted"),
                                  rec(13, "Disliked"), rec(14, "Obscure", votes=3), rec(15, "Good")]})
    got = [c.title for c in recs.candidates(people, tmdb, A, recs.seeds_for(people, A, now=NOW), {})]
    assert got == ["Good"]


def test_overlap_across_seeds_and_trending_boost_ranking(people):
    tmdb = FakeTMDB({("tv", 1): [rec(20, "Both"), rec(21, "One")],
                     ("tv", 2): [rec(20, "Both")]},
                    trending={"tv": [rec(22, "Hot")]})
    seeds = recs.seeds_for(people, A, now=NOW)
    ranked = recs.candidates(people, tmdb, A, seeds, tmdb._trending)
    assert ranked[0].title == "Both"
    hot = next(c for c in ranked if c.title == "Hot")
    assert hot.trending and recs.template_reason(hot) == "Trending this week."


def test_build_without_llm_uses_templated_reasons_and_library_flags(people, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    people.add(LibraryItem(tmdb_id=30, media_type="tv", rating_key="rk"))
    people.commit()
    tmdb = FakeTMDB({("tv", 1): [rec(30, "On The Server"), rec(31, "Not Yet")]})
    assert recs.build_for(people, tmdb, A, {}) == 2
    rows = {r.title: r for r in people.scalars(select(Recommendation).where(Recommendation.plex_id == A))}
    assert rows["On The Server"].in_library and not rows["Not Yet"].in_library
    assert rows["On The Server"].reason.startswith("Because you like Recent Show")


def test_llm_rerank_is_used_when_sane_and_ignored_when_not(people, monkeypatch):
    shortlist = [recs.Candidate(i, "tv", f"T{i}", 2024, "", None, None, 7.0, score=10 - i)
                 for i in range(1, 13)]
    monkeypatch.setattr(llm, "available", lambda: True)
    good = [{"key": f"tv:{i}", "reason": f"why {i}"} for i in (12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2)]
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: good)
    picked = recs.rerank([], shortlist)
    assert picked[0][0].tmdb_id == 12 and picked[0][1] == "why 12"
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: [{"key": "tv:999", "reason": "made up"}])
    assert recs.rerank([], shortlist) is None  # invented ids → fall back to code order
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: None)
    assert recs.rerank([], shortlist) is None


def test_build_all_keeps_people_separate(people, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    tmdb = FakeTMDB({("tv", 1): [rec(40, "For A")], ("tv", 2): [rec(41, "For Both")]})
    recs.build_all(people, tmdb)
    a = {r.title for r in people.scalars(select(Recommendation).where(Recommendation.plex_id == A))}
    b = {r.title for r in people.scalars(select(Recommendation).where(Recommendation.plex_id == B))}
    assert "For A" in a and "For A" not in b and b == {"For Both"}


# ── trending ─────────────────────────────────────────────────────────────

CRIME, DRAMA, KIDS, ANIM = 80, 18, 10762, 16


def hot(id_, title, days_old, genres, votes=500, rating=7.5):
    return rec(id_, title, votes=votes, rating=rating, genre_ids=genres,
               first_air_date=(NOW.date() - timedelta(days=days_old)).isoformat())


def _trending(people, by_seed, trending):
    tmdb = FakeTMDB(by_seed, trending={"tv": trending})
    pool = recs.candidates(people, tmdb, A, recs.seeds_for(people, A, now=NOW), tmdb._trending)
    return pool


def test_trending_puts_fresh_fitting_titles_above_bigger_stale_or_misfit_ones(people):
    # their seeds' neighborhood is crime drama
    by_seed = {("tv", 1): [hot(60 + i, f"Crime {i}", 900, [CRIME, DRAMA]) for i in range(5)]}
    pool = _trending(people, by_seed, [
        hot(71, "Kids Cartoon", 10, [KIDS, ANIM]),        # #1 this week, brand new, nothing like them
        hot(70, "Big Old Hit", 3000, [CRIME, DRAMA]),     # #2, their kind of thing, but a 2018 show
        hot(72, "New Crime Show", 20, [CRIME, DRAMA]),    # #3, new, their kind of thing
        hot(73, "Tiny Premiere", 5, [CRIME], votes=15),   # too new for the 50-vote floor
    ])
    got = [c.title for c, _, _ in recs.trending_for(pool, set(), today=NOW.date())]
    assert got[0] == "New Crime Show"
    assert got.index("Big Old Hit") < got.index("Kids Cartoon")
    assert "Kids Cartoon" in got and "Tiny Premiere" in got  # a misfit sinks, it isn't hidden
    reasons = {c.title: r for c, _, r in recs.trending_for(pool, set(), today=NOW.date())}
    assert reasons["New Crime Show"] == "#3 in TV this week · just out"


def test_trending_names_a_direct_match_and_skips_picks_and_seen(people):
    people.add(Follow(plex_id=A, tmdb_id=81, source="manual"))
    people.commit()
    by_seed = {("tv", 1): [hot(80, "Also Recommended", 30, [CRIME])]}
    pool = _trending(people, by_seed, [hot(80, "Also Recommended", 30, [CRIME]),
                                       hot(81, "Followed", 30, [CRIME]),
                                       hot(82, "Already A Pick", 30, [CRIME])])
    out = recs.trending_for(pool, {(82, "tv")}, today=NOW.date())
    assert [c.title for c, _, _ in out] == ["Also Recommended"]
    assert out[0][2] == "#1 in TV this week · like Recent Show"


def test_trending_without_history_is_buzz_times_freshness():
    old = recs.Candidate(1, "movie", "Old", 2010, "", None, None, 7.0, trend_rank=0,
                         released=NOW.date() - timedelta(days=4000))
    new = recs.Candidate(2, "movie", "New", 2026, "", None, None, 7.0, trend_rank=1,
                         released=NOW.date() - timedelta(days=7))
    assert [c.title for c, _, _ in recs.trending_for([old, new], set(), today=NOW.date())] == ["New", "Old"]


def test_build_stores_trending_that_never_repeats_a_home_pick(people, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    tmdb = FakeTMDB({("tv", 1): [rec(90, "Pick And Trending")]},
                    trending={"tv": [rec(90, "Pick And Trending"), rec(91, "Only Trending")]})
    monkeypatch.setattr(recs, "HOME_PICKS", 1)  # the home page shows only the top pick
    recs.build_for(people, tmdb, A, tmdb._trending)
    picks = [p["title"] for p in recs.for_user(people, A)]
    trend = [p["title"] for p in recs.trending_for_user(people, A)]
    assert picks[0] == "Pick And Trending" and trend == ["Only Trending"]
    assert recs.trending_for_user(people, B) == []
    recs.give_feedback(people, A, 91, "tv", -1)  # not for me → gone from the rail too
    assert recs.trending_for_user(people, A) == []


# ── renewals ─────────────────────────────────────────────────────────────


def show(s, **kw):
    t = Title(tmdb_id=kw.get("id", 5), media_type="tv", name=kw.get("name", "Fallout"),
              status=kw.get("status", "Returning Series"),
              seasons=kw.get("seasons", [{"season": 1, "air_date": "2024-04-10", "episode_count": 8},
                                         {"season": 2, "air_date": "2025-12-17", "episode_count": 8}]),
              last_episode=kw.get("last", {"season": 2, "episode": 8, "air_date": "2026-02-03"}),
              next_episode=kw.get("next"), networks=["Prime Video"], year=2024)
    s.add(t)
    s.flush()
    s.add(Follow(plex_id=A, tmdb_id=t.tmdb_id, source="manual"))
    s.commit()
    return t


class Page:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


YES = {"renewed": True, "season": 3, "source_url": "https://variety.com/2026/tv/news/fallout-renewed-season-3",
       "source_name": "Variety", "announced": "2026-05-01", "quote": "renewed for a third season"}


def test_judge_accepts_a_trusted_confirmed_renewal():
    t = Title(tmdb_id=5, media_type="tv", name="Fallout")
    ok, why, payload = renewals.judge(t, 3, YES, fetch=lambda *a, **k: Page(200, "Fallout has been renewed for season 3"))
    assert ok and payload["source_name"] == "Variety" and payload["via"] == "news"


@pytest.mark.parametrize("answer,page,why", [
    ({**YES, "renewed": False}, None, "not renewed"),
    ({**YES, "season": 4}, None, "isn't the next one"),
    ({**YES, "source_url": "https://fanrumors.blog/fallout-s3"}, None, "untrusted"),
    ({**YES, "source_url": "https://variety.com.evil.io/x"}, None, "untrusted"),
    (YES, Page(200, "Fallout review: episode 5 recap"), "doesn't mention"),
])
def test_judge_rejects(answer, page, why):
    t = Title(tmdb_id=5, media_type="tv", name="Fallout")
    ok, reason, _ = renewals.judge(t, 3, answer, fetch=lambda *a, **k: page or Page(200, "fallout renewed"))
    assert not ok and why in reason


def test_trusted_site_that_blocks_bots_is_taken_on_the_domain():
    t = Title(tmdb_id=5, media_type="tv", name="Fallout")
    assert renewals.judge(t, 3, YES, fetch=lambda *a, **k: Page(403))[0]

    def boom(*a, **k):
        raise httpx.ConnectError("x")
    assert renewals.judge(t, 3, YES, fetch=boom)[0]


def test_due_only_between_seasons_with_nothing_on_tmdb(s):
    s.add(User(plex_id=A, pms_account_id=1, username="a"))
    s.flush()
    show(s, id=5)                                                        # due
    show(s, id=6, name="Mid Season", next={"season": 2, "episode": 5, "air_date": "2026-10-01"})
    show(s, id=7, name="Listed", seasons=[{"season": 3, "air_date": None, "episode_count": 0}])
    show(s, id=8, name="Ended", status="Ended")
    show(s, id=9, name="Just Finished", last={"season": 2, "episode": 8, "air_date": date.today().isoformat()})
    show(s, id=10, name="Checked")
    s.add(RenewalCheck(tmdb_id=10, checked_at=utcnow()))
    s.commit()
    assert [t.name for t in renewals.due(s)] == ["Fallout"]


def test_run_stores_one_event_that_tmdb_later_dedupes_into(s, monkeypatch):
    s.add(User(plex_id=A, pms_account_id=1, username="a"))
    s.flush()
    show(s, id=5)
    report = renewals.run(s, asker=lambda t, n: YES, fetch=lambda *a, **k: Page(200, "fallout renewed"))
    assert report.renewed == ["Fallout S3 (Variety)"]
    ev = s.scalar(select(Event))
    assert ev.dedupe_key == "renewed:5:3"  # same key detect.py uses for TMDB's season row
    assert describe(ev.kind, ev.payload, "Fallout") == "Fallout was renewed for season 3 (Variety, May 1, 2026)"
    assert renewals.due(s) == []          # checked this week, and already known renewed
    tl = upcoming(s, A, today=date(2026, 9, 27))
    assert tl["renewed_no_date"][0]["source"] == "Variety"


def test_run_is_a_no_op_without_headless_claude(s, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    assert renewals.run(s).checked == []


def test_old_news_is_recorded_quietly_and_never_alerted(s):
    from telly.alerts import deliver

    class Sink:
        sent = []

        def send(self, *a, **k):
            Sink.sent.append(a)

    s.add(User(plex_id=A, pms_account_id=1, username="a"))
    s.flush()
    show(s, id=5)
    from telly.models import IdentityLink
    s.add(IdentityLink(surface="discord", external_id="d", plex_id=A, source="link"))
    s.commit()
    old = {**YES, "announced": "2025-05-08"}
    renewals.run(s, asker=lambda t, n: old, fetch=lambda *a, **k: Page(200, "fallout renewed"))
    ev = s.scalar(select(Event))
    assert ev.payload["quiet"] is True
    assert describe(ev.kind, ev.payload, "Fallout") == "Fallout was renewed for season 3 (Variety, May 8, 2025)"
    deliver(s, Sink(), Sink())
    assert Sink.sent == []
    from telly.shows import whats_new
    assert whats_new(s, A) == []                                   # not in the news feed…
    assert upcoming(s, A)["renewed_no_date"][0]["season"] == 3     # …but on the timeline


def test_fresh_news_alerts():
    assert renewals._is_old("2026-09-20", today=date(2026, 9, 27)) is False
    assert renewals._is_old("2026-07-01", today=date(2026, 9, 27)) is True
    assert renewals._is_old(None) is False


def test_announcement_date_trusts_the_url_month_over_the_model():
    url = "https://deadline.com/2025/05/fallout-renewed-season-3-prime-video-1236394147/"
    assert renewals.announced_on("2026-09-27", url) == "2025-05-01"   # model said "today"
    assert renewals.announced_on("2025-05-08", url) == "2025-05-08"   # model's is earlier & plausible
    assert renewals.announced_on(None, url) == "2025-05-01"
    assert renewals.announced_on("2026-01-02", "https://variety.com/2026/tv/news/x") == "2026-01-02"
    assert renewals.announced_on(None, "https://variety.com/tv/x") is None


def test_without_llm_a_returning_pick_keeps_its_earlier_reason(people, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    people.add(Recommendation(plex_id=A, tmdb_id=30, media_type="tv", rank=1, score=1, title="Kept",
                              reason="A reason Haiku wrote last night."))
    people.commit()
    tmdb = FakeTMDB({("tv", 1): [rec(30, "Kept"), rec(31, "New One")]})
    recs.build_for(people, tmdb, A, {})
    rows = {r.title: r.reason for r in people.scalars(select(Recommendation).where(Recommendation.plex_id == A))}
    assert rows["Kept"] == "A reason Haiku wrote last night."
    assert rows["New One"].startswith("Because you like")


def test_members_without_overseerr_get_no_nightly_run(people, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    people.get(User, B).overseerr_id = None
    people.commit()
    tmdb = FakeTMDB({("tv", 1): [rec(40, "For A")], ("tv", 2): [rec(41, "For B")]})
    out = recs.build_all(people, tmdb)
    assert set(out) == {A}
    assert not list(people.scalars(select(Recommendation).where(Recommendation.plex_id == B)))

