"""Dismiss / Mark watched on What's new (news.py), through the web API and the MCP.

Made-up people, shows and ids only: the repo is public.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from telly import db, requester
from telly.config import get_settings
from telly.crypto import COOKIE, make_session
from telly.models import (Event, Follow, IdentityLink, NewsDismissal, Play, Title, User, WatchEntry,
                          utcnow)

SECRET = "test-shared-secret"
ALICE, BOB = 111, 222
SHARED, ALICE_ONLY, BOB_ONLY, ENDED = 10, 11, 12, 13


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TELLY_DATABASE_URL", f"sqlite:///{tmp_path / 'news.db'}")
    monkeypatch.setenv("TELLY_SHARED_SECRET", SECRET)
    monkeypatch.setenv("TELLY_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("TELLY_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TELLY_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TELLY_LLM_ENABLED", "false")
    monkeypatch.setenv("TELLY_PLEXBOT_AUTH_SECRET", "")
    get_settings.cache_clear()
    db._engine = db._factory = None
    db.Base.metadata.create_all(db.get_engine())
    now = utcnow()
    with db.session_scope() as s:
        s.add_all([User(plex_id=ALICE, pms_account_id=1, username="alice", is_owner=True),
                   User(plex_id=BOB, pms_account_id=BOB, username="bob")])
        s.flush()
        s.add_all([
            Title(tmdb_id=SHARED, media_type="tv", name="Shared Show", status="Returning Series",
                  last_episode={"season": 2, "episode": 10}),
            Title(tmdb_id=ALICE_ONLY, media_type="tv", name="Alice Show", status="Returning Series",
                  last_episode={"season": 1, "episode": 3}),
            Title(tmdb_id=BOB_ONLY, media_type="tv", name="Bob Show", status="Returning Series",
                  last_episode={"season": 1, "episode": 1}),
            Title(tmdb_id=ENDED, media_type="tv", name="Over Show", status="Ended",
                  last_episode={"season": 3, "episode": 8}),
            Follow(plex_id=ALICE, tmdb_id=SHARED, source="manual"),
            Follow(plex_id=ALICE, tmdb_id=ALICE_ONLY, source="manual"),
            Follow(plex_id=ALICE, tmdb_id=ENDED, source="manual"),
            Follow(plex_id=BOB, tmdb_id=SHARED, source="manual"),
            Follow(plex_id=BOB, tmdb_id=BOB_ONLY, source="manual"),
            IdentityLink(surface="discord", external_id="d-alice", plex_id=ALICE, source="link"),
            IdentityLink(surface="discord", external_id="d-bob", plex_id=BOB, source="link"),
        ])

        def ev(i, tmdb_id, kind, payload, ago):
            s.add(Event(id=i, tmdb_id=tmdb_id, kind=kind, payload=payload, dedupe_key=f"k{i}",
                        detected_at=now - timedelta(days=ago)))
        ev(1, SHARED, "episodes_aired", {"season": 2, "from_episode": 9, "to_episode": 9}, 3)
        ev(2, SHARED, "episodes_aired", {"season": 2, "from_episode": 10, "to_episode": 10}, 1)
        ev(3, SHARED, "renewed", {"season": 3, "via": "status"}, 2)
        ev(4, ALICE_ONLY, "episodes_aired", {"season": 1, "from_episode": 3, "to_episode": 3}, 1)
        ev(5, BOB_ONLY, "episodes_aired", {"season": 1, "from_episode": 1, "to_episode": 1}, 1)
        ev(6, ENDED, "episodes_aired", {"season": 3, "from_episode": 8, "to_episode": 8}, 1)
        ev(7, SHARED, "renewed", {"season": 3, "via": "news", "quiet": True}, 2)
    yield
    get_settings.cache_clear()
    db._engine = db._factory = None


@pytest.fixture
def client(env):
    from telly.app import create_app
    with TestClient(create_app(), base_url="http://localhost:8940") as c:
        yield c


def web(client, pid):
    client.cookies.set(COOKIE, make_session(pid))
    return client


def post(client, path):
    return client.post(path, json={})


def ids(client, pid, path="/api/whats-new"):
    r = web(client, pid).get(path)
    assert r.status_code == 200
    items = r.json()["events"] if path == "/api/whats-new" else r.json()["whats_new"]
    return [x["event_id"] for x in items]


def entry(pid, tmdb_id):
    with db.session_scope() as s:
        e = s.get(WatchEntry, (pid, tmdb_id, "tv"))
        return None if e is None else {"status": e.status, "rating": e.rating, "source": e.source}


def call(client, discord_id, tool, **args):
    token = requester.mint("discord", discord_id, secret=SECRET)
    r = client.post("/mcp", headers={"Accept": "application/json, text/event-stream",
                                     "Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}})
    assert r.status_code == 200, r.text
    return json.loads(r.json()["result"]["content"][0]["text"])


# ── what's listed ─────────────────────────────────────────────────────────


def test_items_carry_event_id_and_watchable(client):
    items = {x["event_id"]: x for x in web(client, ALICE).get("/api/whats-new").json()["events"]}
    assert set(items) == {2, 3, 4, 6}  # E9 is superseded by E10; quiet news never shows
    assert items[2]["watchable"] is True and items[3]["watchable"] is False


def test_dismiss_hides_it_only_for_the_caller(client):
    r = post(web(client, ALICE), "/api/whats-new/2/dismiss")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "event_id": 2, "reason": "dismissed"}
    assert 2 not in ids(client, ALICE)
    assert 2 not in ids(client, ALICE, "/api/home")
    assert 1 not in ids(client, ALICE)  # dismissing the latest release doesn't bring back the one before
    assert 2 in ids(client, BOB)  # Bob follows the same show: his list is untouched
    assert 2 in ids(client, BOB, "/api/home")


def test_dismiss_is_idempotent_and_keeps_one_row(client):
    post(web(client, ALICE), "/api/whats-new/3/dismiss")
    assert post(client, "/api/whats-new/3/dismiss").json()["reason"] == "dismissed"
    with db.session_scope() as s:
        assert len(s.scalars(select(NewsDismissal)).all()) == 1


def test_restore_brings_it_back(client):
    post(web(client, ALICE), "/api/whats-new/3/dismiss")
    r = post(client, "/api/whats-new/3/restore")
    assert r.json() == {"ok": True, "event_id": 3, "restored": True, "history_reverted": False}
    assert 3 in ids(client, ALICE)
    # restoring something never dismissed is a harmless no-op
    assert post(client, "/api/whats-new/3/restore").json()["restored"] is False


# ── watched → History ─────────────────────────────────────────────────────


def test_watched_on_a_running_show_sets_watching(client):
    r = post(web(client, ALICE), "/api/whats-new/4/watched")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "event_id": 4, "reason": "watched",
                        "history": {"title": "Alice Show", "tmdb_id": ALICE_ONLY, "media_type": "tv",
                                    "status": "watching", "changed": True}}
    assert 4 not in ids(client, ALICE)
    assert entry(ALICE, ALICE_ONLY)["status"] == "watching"
    assert entry(BOB, ALICE_ONLY) is None
    hist = {x["tmdb_id"]: x for x in client.get("/api/history").json()["items"]}
    assert hist[ALICE_ONLY]["status"] == "watching" and hist[ALICE_ONLY]["status_set"] is True


def test_watched_on_the_last_episode_of_an_ended_show_is_finished(client):
    r = post(web(client, ALICE), "/api/whats-new/6/watched")
    assert r.json()["history"]["status"] == "finished"
    assert entry(ALICE, ENDED)["status"] == "finished"


def test_watched_keeps_a_status_they_set(client):
    with db.session_scope() as s:
        s.add(WatchEntry(plex_id=ALICE, tmdb_id=ALICE_ONLY, media_type="tv", title="Alice Show",
                         status="dropped", rating=-1, source="manual"))
    r = post(web(client, ALICE), "/api/whats-new/4/watched")
    assert r.json()["history"] == {"title": "Alice Show", "tmdb_id": ALICE_ONLY, "media_type": "tv",
                                   "status": "dropped", "changed": False}
    assert entry(ALICE, ALICE_ONLY) == {"status": "dropped", "rating": -1, "source": "manual"}
    post(client, "/api/whats-new/4/restore")  # undo leaves their own status alone too
    assert entry(ALICE, ALICE_ONLY) == {"status": "dropped", "rating": -1, "source": "manual"}


def test_watched_writes_nothing_when_plex_already_has_them_current(client):
    with db.session_scope() as s:
        s.add(Play(history_key="h1", plex_id=ALICE, rating_key="r1", media_type="tv",
                   tmdb_id=ALICE_ONLY, season=1, episode=2, viewed_at=utcnow() - timedelta(days=2)))
    r = post(web(client, ALICE), "/api/whats-new/4/watched")
    assert r.json()["history"]["status"] == "watching" and r.json()["history"]["changed"] is False
    assert entry(ALICE, ALICE_ONLY) is None  # Plex plays are never copied into watch_entries


def test_watched_on_a_stalled_plex_show_says_watching(client):
    with db.session_scope() as s:
        s.add(Play(history_key="h2", plex_id=ALICE, rating_key="r2", media_type="tv",
                   tmdb_id=ALICE_ONLY, season=1, episode=1, viewed_at=utcnow() - timedelta(days=90)))
    assert post(web(client, ALICE), "/api/whats-new/4/watched").json()["history"]["changed"] is True
    assert entry(ALICE, ALICE_ONLY) == {"status": "watching", "rating": None, "source": "plex"}
    post(client, "/api/whats-new/4/restore")
    assert entry(ALICE, ALICE_ONLY) is None  # back to what Plex says


def test_restore_after_watched_takes_back_the_history_write(client):
    post(web(client, ALICE), "/api/whats-new/4/watched")
    r = post(client, "/api/whats-new/4/restore")
    assert r.json() == {"ok": True, "event_id": 4, "restored": True, "history_reverted": True}
    assert entry(ALICE, ALICE_ONLY) is None
    assert 4 in ids(client, ALICE)


def test_restore_keeps_a_status_they_changed_since(client):
    post(web(client, ALICE), "/api/whats-new/4/watched")
    client.post("/api/history/status", json={"tmdb_id": ALICE_ONLY, "media_type": "tv", "status": "finished"})
    assert post(client, "/api/whats-new/4/restore").json()["history_reverted"] is False
    assert entry(ALICE, ALICE_ONLY)["status"] == "finished"


def test_dismissed_then_watched_upgrades_once(client):
    post(web(client, ALICE), "/api/whats-new/4/dismiss")
    assert post(client, "/api/whats-new/4/watched").json()["history"]["changed"] is True
    again = post(client, "/api/whats-new/4/watched").json()
    assert again["reason"] == "watched" and again["history"]["changed"] is False
    with db.session_scope() as s:
        rows = s.scalars(select(NewsDismissal)).all()
        assert [(r.plex_id, r.event_id, r.reason) for r in rows] == [(ALICE, 4, "watched")]


def test_only_a_release_can_be_watched(client):
    r = post(web(client, ALICE), "/api/whats-new/3/watched")
    assert r.status_code == 400
    assert entry(ALICE, SHARED) is None
    assert 3 in ids(client, ALICE)


# ── access ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["dismiss", "watched", "restore"])
def test_foreign_missing_and_quiet_events_are_404(client, action):
    web(client, ALICE)
    for event_id in (5, 7, 999):  # Bob's show, quiet news, no such event
        assert post(client, f"/api/whats-new/{event_id}/{action}").status_code == 404
    assert entry(ALICE, BOB_ONLY) is None and entry(BOB, BOB_ONLY) is None
    with db.session_scope() as s:
        assert s.scalars(select(NewsDismissal)).all() == []


def test_bob_cannot_restore_alices_dismissal(client):
    post(web(client, ALICE), "/api/whats-new/4/dismiss")
    assert post(web(client, BOB), "/api/whats-new/4/restore").status_code == 404
    assert 4 not in ids(client, ALICE)


def test_writes_need_a_session_and_json(client):
    for action in ("dismiss", "watched", "restore"):
        assert client.post(f"/api/whats-new/2/{action}", json={}).status_code == 401
    web(client, ALICE)
    for action in ("dismiss", "watched", "restore"):
        assert client.post(f"/api/whats-new/2/{action}").status_code == 415
        assert client.post(f"/api/whats-new/2/{action}", data={"a": "b"}).status_code == 415
    assert 2 in ids(client, ALICE)


# ── through plexbot (MCP) ─────────────────────────────────────────────────


def test_mcp_dismiss_and_watched_are_the_callers_own(client):
    events = call(client, "d-alice", "whats_new")["events"]
    assert {e["event_id"] for e in events} == {2, 3, 4, 6}
    assert call(client, "d-alice", "dismiss_news", event_id=3)["reason"] == "dismissed"
    out = call(client, "d-alice", "mark_news_watched", event_id=4)
    assert out["history"]["status"] == "watching"
    assert {e["event_id"] for e in call(client, "d-alice", "whats_new")["events"]} == {2, 6}
    assert {e["event_id"] for e in call(client, "d-bob", "whats_new")["events"]} == {2, 3, 5}
    assert "error" in call(client, "d-bob", "dismiss_news", event_id=4)  # not Bob's to see
    assert "error" in call(client, "d-bob", "mark_news_watched", event_id=3)
