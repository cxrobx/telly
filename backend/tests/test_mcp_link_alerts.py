"""Identity, MCP scoping, /link, and alert fan-out (spec §4–6), through the real app."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from telly import db, requester
from telly.alerts import deliver
from telly.config import get_settings
from telly.models import (AlertPref, Delivery, Event, Follow, IdentityLink, LinkCode, Title, User,
                          utcnow)

SECRET = "test-shared-secret"
ALICE, BOB, GONE = 111, 222, 333  # plex ids


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TELLY_DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("TELLY_SHARED_SECRET", SECRET)
    monkeypatch.setenv("TELLY_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TELLY_PUBLIC_URL", "https://telly.test")
    get_settings.cache_clear()
    db._engine = db._factory = None
    db.Base.metadata.create_all(db.get_engine())
    with db.session_scope() as s:
        s.add_all([
            User(plex_id=ALICE, pms_account_id=1, username="alice", is_owner=True),
            User(plex_id=BOB, pms_account_id=BOB, username="bob"),
            User(plex_id=GONE, pms_account_id=GONE, username="gone", removed_at=utcnow()),
        ])
        s.flush()  # members first: no ORM relationships, so the FK order is ours to keep
        s.add_all([
            Title(tmdb_id=1, media_type="tv", name="Alice Show", status="Returning Series"),
            Title(tmdb_id=2, media_type="tv", name="Bob Show", status="Returning Series"),
            Follow(plex_id=ALICE, tmdb_id=1, source="inferred"),
            Follow(plex_id=BOB, tmdb_id=2, source="inferred"),
            IdentityLink(surface="discord", external_id="d-alice", plex_id=ALICE, source="link"),
            IdentityLink(surface="discord", external_id="d-bob", plex_id=BOB, source="link"),
            IdentityLink(surface="discord", external_id="d-gone", plex_id=GONE, source="link"),
        ])
    yield
    get_settings.cache_clear()
    db._engine = db._factory = None


@pytest.fixture
def client(env):
    from telly.app import create_app
    with TestClient(create_app(), base_url="http://localhost:8940") as c:
        yield c


def tok(surface, uid, **kw):
    return requester.mint(surface, uid, secret=kw.pop("secret", SECRET), **kw)


def rpc(client, token, method, params=None):
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def call(client, token, tool, **args):
    r = rpc(client, token, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "result" in body, body
    return json.loads(body["result"]["content"][0]["text"])


# ── the token ────────────────────────────────────────────────────────────


def test_token_round_trip_and_rejections():
    assert requester.verify(tok("discord", "d1"), secret=SECRET).uid == "d1"
    good = tok("discord", "d1")
    assert requester.verify(good[:-1] + ("0" if good[-1] != "0" else "1"), secret=SECRET) is None
    assert requester.verify(tok("discord", "d1", secret="other"), secret=SECRET) is None
    assert requester.verify(tok("discord", "d1", ttl=-1), secret=SECRET) is None
    assert requester.verify(tok("email", "d1"), secret=SECRET) is None
    assert requester.verify(tok("discord", "d1"), secret="") is None  # unset secret fails closed


def test_accepts_plexbots_token_format(monkeypatch):
    """Same vector as plex-agent/tests/test_telly.py::CONTRACT_TOKEN; change both together."""
    contract = (
        "eyJzdXJmYWNlIjoiZGlzY29yZCIsInVpZCI6IjEyMzQ1Njc4OSIsIm5hbWUiOiJDaHJpcyIsImV4cCI6MTc5MDAwMDkwMH0"
        ".e8b6bad269af2c4ff5ef6163b3e52cae9dbf76dfce6ad588f483323220acb5bd"
    )
    monkeypatch.setattr(requester.time, "time", lambda: 1790000000)
    assert requester.verify(contract, secret="contract-secret") == requester.Requester(
        "discord", "123456789", "Chris")
    monkeypatch.setattr(requester.time, "time", lambda: 1790000901)
    assert requester.verify(contract, secret="contract-secret") is None  # 15 minutes later


@pytest.mark.parametrize("token", [None, "garbage", "a.b"])
def test_mcp_refuses_without_a_valid_token(client, token):
    assert rpc(client, token, "tools/list").status_code == 401


def test_mcp_refuses_expired_and_foreign_tokens(client):
    assert rpc(client, tok("discord", "d-alice", ttl=-5), "tools/list").status_code == 401
    assert rpc(client, tok("discord", "d-alice", secret="nope"), "tools/list").status_code == 401


# ── scoping ──────────────────────────────────────────────────────────────


def test_no_tool_can_name_a_user(client):
    tools = rpc(client, tok("discord", "d-alice"), "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} >= {"whats_new", "upcoming", "followed_shows", "follow_show"}
    banned = {"user", "user_id", "plex_id", "username", "account", "discord_id", "uid", "surface", "who"}
    for t in tools:
        assert not banned & set(t["inputSchema"].get("properties", {})), t["name"]


def test_each_caller_sees_only_their_own_shows(client):
    alice = call(client, tok("discord", "d-alice"), "followed_shows")
    bob = call(client, tok("discord", "d-bob"), "followed_shows")
    assert [x["name"] for x in alice["shows"]] == ["Alice Show"]
    assert [x["name"] for x in bob["shows"]] == ["Bob Show"]


def test_unlinked_discord_user_is_told_to_link_and_gets_nobodys_data(client):
    out = call(client, tok("discord", "d-stranger"), "followed_shows")
    assert out["linked"] is False and "!link" in out["message"]
    assert "shows" not in out


def test_removed_member_is_treated_as_unlinked(client):
    assert call(client, tok("discord", "d-gone"), "followed_shows")["linked"] is False


class FakeOverseerr:
    def __init__(self, mapping):
        self.mapping, self.calls = mapping, 0

    def plex_id_for(self, uid):
        self.calls += 1
        return self.mapping.get(uid)


def test_web_surface_resolves_through_overseerr_and_caches(env):
    o = FakeOverseerr({7: BOB})
    with db.session_scope() as s:
        assert requester.resolve(s, requester.Requester("web", "7"), o) == BOB
        assert requester.resolve(s, requester.Requester("web", "7"), o) == BOB
    assert o.calls == 1


def test_web_surface_for_a_non_member_resolves_to_nobody(env):
    with db.session_scope() as s:
        assert requester.resolve(s, requester.Requester("web", "8"), FakeOverseerr({8: 999})) is None
        assert requester.resolve(s, requester.Requester("web", "9"), FakeOverseerr({})) is None


def test_follow_and_unfollow_touch_only_the_caller(client):
    call(client, tok("discord", "d-bob"), "unfollow_show", tmdb_id=1)  # Bob unfollows Alice's show
    with db.session_scope() as s:
        assert s.get(Follow, (ALICE, 1)).state == "following"
        assert s.get(Follow, (BOB, 1)).state == "unfollowed"


def test_phone_alerts_create_a_private_topic_for_the_caller_only(client):
    out = call(client, tok("discord", "d-alice"), "alert_settings", phone_alerts=True)
    assert out["phone_alerts"] and out["discord_linked"] and "ntfy.sh/telly-" in out["phone_setup"]
    with db.session_scope() as s:
        assert s.get(AlertPref, ALICE).ntfy_topic.startswith("telly-")
        assert s.get(AlertPref, BOB) is None


# ── /link ────────────────────────────────────────────────────────────────


class FakePlexTV:
    def __init__(self, user_id, approved=True):
        self.user_id, self.approved = user_id, approved

    def create_pin(self):
        return {"id": 42, "code": "abcd"}

    def auth_url(self, code, forward_url=None):
        return f"https://app.plex.tv/auth#?code={code}"

    def pin_token(self, pin_id):
        return "user-token" if self.approved else None

    def user(self, token):
        return {"id": self.user_id, "username": "whoever"}


def _code(client, discord_id="d-new", name="New Person"):
    r = client.post("/internal/link-codes", json={"discord_name": name},
                    headers={"Authorization": f"Bearer {tok('discord', discord_id)}"})
    assert r.status_code == 200, r.text
    return r.json()["url"].split("code=")[1]


def test_link_codes_need_a_discord_token(client):
    assert client.post("/internal/link-codes", json={}).status_code == 401
    r = client.post("/internal/link-codes", json={},
                    headers={"Authorization": f"Bearer {tok('web', '1')}"})
    assert r.status_code == 401


def test_link_happy_path_then_the_code_is_spent(client, monkeypatch):
    from telly import app as app_mod
    code = _code(client)
    assert client.post("/api/link/describe", json={"code": code}).json() == {"discord_name": "New Person"}
    monkeypatch.setattr(app_mod, "PlexTV", lambda: FakePlexTV(BOB, approved=False))
    assert client.post("/api/link/start", json={"code": code}).status_code == 200
    assert client.post("/api/link/finish", json={"code": code}).json() == {"done": False}
    monkeypatch.setattr(app_mod, "PlexTV", lambda: FakePlexTV(BOB))
    assert client.post("/api/link/finish", json={"code": code}).json()["done"] is True
    with db.session_scope() as s:
        assert s.get(IdentityLink, ("discord", "d-new")).plex_id == BOB
    assert client.post("/api/link/finish", json={"code": code}).status_code == 400  # single use
    assert call(client, tok("discord", "d-new"), "followed_shows")["shows"][0]["name"] == "Bob Show"


def test_link_refuses_a_plex_account_not_on_the_server(client, monkeypatch):
    from telly import app as app_mod
    monkeypatch.setattr(app_mod, "PlexTV", lambda: FakePlexTV(999))
    code = _code(client)
    client.post("/api/link/start", json={"code": code})
    r = client.post("/api/link/finish", json={"code": code})
    assert r.status_code == 400 and "isn't a member" in r.json()["detail"]
    with db.session_scope() as s:
        assert s.get(IdentityLink, ("discord", "d-new")) is None


def test_expired_code_is_refused(client):
    code = _code(client)
    with db.session_scope() as s:
        s.get(LinkCode, code).expires_at = utcnow() - timedelta(seconds=1)
    assert client.post("/api/link/describe", json={"code": code}).status_code == 400


def test_relinking_moves_the_discord_id_to_the_new_plex_account(client, monkeypatch):
    from telly import app as app_mod
    monkeypatch.setattr(app_mod, "PlexTV", lambda: FakePlexTV(BOB))
    code = _code(client, discord_id="d-alice")
    client.post("/api/link/start", json={"code": code})
    client.post("/api/link/finish", json={"code": code})
    with db.session_scope() as s:
        assert s.get(IdentityLink, ("discord", "d-alice")).plex_id == BOB


# ── alerts ───────────────────────────────────────────────────────────────


class Sink:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, target, *args, **kw):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((target, args[0] if args else None))


def _event(s, tmdb_id=1, age=timedelta(0), key="renewed:1:2"):
    ev = Event(tmdb_id=tmdb_id, kind="renewed", payload={"season": 2, "via": "status"},
               dedupe_key=key, detected_at=utcnow() - age)
    s.add(ev)
    s.flush()
    return ev


def test_each_follower_gets_one_alert_per_channel_ever(env):
    discord, ntfy = Sink(), Sink()
    with db.session_scope() as s:
        s.add(AlertPref(plex_id=ALICE, discord_enabled=True, ntfy_topic="telly-alice"))
        _event(s)
        s.commit()
        r1 = deliver(s, discord, ntfy)
        r2 = deliver(s, discord, ntfy)
    assert (r1.sent, r2.sent) == (2, 0)
    assert discord.sent == [("d-alice", "📺 Alice Show was renewed for season 2")]
    assert ntfy.sent == [("telly-alice", "Telly")]


def test_non_followers_unfollowers_and_removed_members_get_nothing(env):
    discord = Sink()
    with db.session_scope() as s:
        s.add(Follow(plex_id=GONE, tmdb_id=1, source="inferred"))
        s.get(Follow, (ALICE, 1)).state = "unfollowed"
        _event(s)
        s.commit()
        deliver(s, discord, Sink())
    assert discord.sent == []


def test_no_discord_link_means_no_dm(env):
    discord = Sink()
    with db.session_scope() as s:
        s.delete(s.get(IdentityLink, ("discord", "d-alice")))
        _event(s)
        s.commit()
        deliver(s, discord, Sink())
    assert discord.sent == []


def test_stale_events_are_never_sent(env):
    discord = Sink()
    with db.session_scope() as s:
        _event(s, age=timedelta(hours=get_settings().alert_max_age_hours + 1))
        s.commit()
        deliver(s, discord, Sink())
    assert discord.sent == []


def test_failed_sends_retry_up_to_the_limit_then_stop(env):
    bad = Sink(fail=True)
    with db.session_scope() as s:
        _event(s)
        s.commit()
        for _ in range(5):
            deliver(s, bad, Sink())
        d = s.scalar(select(Delivery))
    assert d.status == "failed" and d.attempts == get_settings().alert_max_attempts


def test_a_send_interrupted_mid_flight_is_never_repeated(env):
    discord = Sink()
    with db.session_scope() as s:
        ev = _event(s)
        s.add(Delivery(plex_id=ALICE, event_id=ev.id, channel="discord", status="sending", attempts=1))
        s.commit()
        deliver(s, discord, Sink())
    assert discord.sent == []


def test_record_taste_finds_the_title_and_records_it_for_the_caller(client, monkeypatch):
    from telly import mcp_server

    class Search:
        def search_multi(self, q):
            return [{"id": 9480, "media_type": "movie", "title": "Daredevil", "release_date": "2003-02-14"},
                    {"id": 61889, "media_type": "tv", "name": "Marvel's Daredevil", "first_air_date": "2015-04-10"}]

        def tv(self, i):
            return {"name": "Marvel's Daredevil"}

        def movie(self, i):
            return {"title": "Daredevil"}

    monkeypatch.setattr(mcp_server, "TMDBClient", Search)
    out = call(client, tok("discord", "d-bob"), "record_taste", title="daredevil", liked=True)
    assert (out["tmdb_id"], out["liked"]) == (9480, True)
    assert out["other_matches"][0]["tmdb_id"] == 61889
    with db.session_scope() as s:
        from telly.models import TasteSignal
        assert s.get(TasteSignal, (BOB, 9480, "movie", "told")) is not None
        assert s.get(TasteSignal, (ALICE, 9480, "movie", "told")) is None


def test_telling_plexbot_a_first_like_builds_first_picks_now(client, monkeypatch):
    from telly import llm, mcp_server, recs
    from telly.models import Recommendation

    class TMDB:
        def search_multi(self, q):
            return [{"id": 1438, "media_type": "tv", "name": "The Wire", "first_air_date": "2002-06-02"}]

        def tv(self, i):
            return {"name": "The Wire"}

        def recommendations(self, media_type, tmdb_id):
            return [{"id": 1100, "name": "Like The Wire", "vote_count": 900, "vote_average": 8.0}]

        def trending(self, media_type, window="week"):
            return []

    monkeypatch.setattr(mcp_server, "TMDBClient", TMDB)
    monkeypatch.setattr(recs, "TMDBClient", TMDB)
    monkeypatch.setattr(llm, "available", lambda: False)
    started = []
    monkeypatch.setattr(recs, "warm_in_background", lambda pid: started.append(recs.warm(pid)))
    with db.session_scope() as s:
        s.get(User, BOB).overseerr_id = 2
    out = call(client, tok("discord", "d-bob"), "record_taste", title="the wire", liked=True)
    assert started == [True] and "first picks" in out["note"]
    with db.session_scope() as s:
        assert [r.title for r in s.query(Recommendation).filter_by(plex_id=BOB)] == ["Like The Wire"]
    out = call(client, tok("discord", "d-bob"), "record_watched", tmdb_id=1438, media_type="tv")
    assert started == [True] and out["note"] == "Picks update in tonight's refresh."  # has picks now
