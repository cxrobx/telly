"""Web sign-in, sessions, API scoping, watchlist/library sync (spec §3, §7)."""

from __future__ import annotations

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from telly import api as api_mod
from telly import db
from telly.config import get_settings
from telly.crypto import COOKIE, decrypt_token, make_session
from telly.models import (Follow, LibraryItem, PlexItem, Recommendation, Title, User,
                          WatchlistItem, utcnow)
from telly.plexdata import PlexDataReport, sync_library, sync_watchlists

ALICE, BOB, GONE = 111, 222, 333


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TELLY_DATABASE_URL", f"sqlite:///{tmp_path / 'w.db'}")
    monkeypatch.setenv("TELLY_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("TELLY_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TELLY_SHARED_SECRET", "x")
    monkeypatch.setenv("TELLY_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TELLY_LLM_ENABLED", "false")
    # a developer's backend/.env may carry the real one; tests opt in explicitly
    monkeypatch.setenv("TELLY_PLEXBOT_AUTH_SECRET", "")
    get_settings.cache_clear()
    db._engine = db._factory = None
    db.Base.metadata.create_all(db.get_engine())
    with db.session_scope() as s:
        s.add_all([User(plex_id=ALICE, pms_account_id=1, username="alice", is_owner=True, overseerr_id=1),
                   User(plex_id=BOB, pms_account_id=BOB, username="bob"),
                   User(plex_id=GONE, pms_account_id=GONE, username="gone", removed_at=utcnow())])
        s.flush()
        s.add_all([Title(tmdb_id=1, media_type="tv", name="Alice Show"),
                   Title(tmdb_id=2, media_type="tv", name="Bob Show"),
                   Follow(plex_id=ALICE, tmdb_id=1, source="inferred"),
                   Follow(plex_id=BOB, tmdb_id=2, source="inferred"),
                   Recommendation(plex_id=ALICE, tmdb_id=50, media_type="movie", rank=1, score=1,
                                  title="Alice Pick", reason="r"),
                   Recommendation(plex_id=BOB, tmdb_id=60, media_type="tv", rank=1, score=1,
                                  title="Bob Pick", reason="r")])
    yield
    get_settings.cache_clear()
    db._engine = db._factory = None


@pytest.fixture
def client(env):
    from telly.app import create_app
    with TestClient(create_app(), base_url="http://localhost:8940") as c:
        yield c


def as_user(client, pid):
    client.cookies.set(COOKIE, make_session(pid))
    return client


class FakePlexTV:
    def __init__(self, user_id=ALICE, approved=True, watch=None, status=200):
        self.user_id, self.approved, self.watch, self.status = user_id, approved, watch or [], status

    def create_pin(self):
        return {"id": 777, "code": "c0de"}

    def auth_url(self, code, forward_url=None):
        return f"https://app.plex.tv/auth#?code={code}"

    def pin_token(self, pin_id):
        assert pin_id == 777
        return "PLEX-TOKEN-SECRET" if self.approved else None

    def user(self, token):
        return {"id": self.user_id, "username": "someone"}

    def watchlist(self, token):
        if self.status != 200:
            raise httpx.HTTPStatusError("x", request=httpx.Request("GET", "http://x"),
                                        response=httpx.Response(self.status))
        return self.watch


# ── sign-in + sessions ───────────────────────────────────────────────────


def test_everything_needs_a_session(client):
    for path in ("/api/me", "/api/home", "/api/timeline", "/api/recs", "/api/following"):
        assert client.get(path).status_code == 401, path


def test_dev_login_is_off_unless_configured(client):
    assert client.get("/api/auth/dev", follow_redirects=False).status_code == 404


def test_sign_in_sets_a_session_and_stores_the_token_encrypted(client, monkeypatch):
    monkeypatch.setattr(api_mod, "PlexTV", lambda: FakePlexTV(ALICE))
    assert "auth_url" in client.post("/api/auth/start", json={}).json()
    r = client.post("/api/auth/finish", json={})
    assert r.json()["done"] is True and client.cookies.get(COOKIE)
    assert client.get("/api/me").json()["username"] == "alice"
    with db.session_scope() as s:
        stored = s.get(User, ALICE).plex_token_enc
    assert stored and "PLEX-TOKEN-SECRET" not in stored
    assert decrypt_token(stored) == "PLEX-TOKEN-SECRET"


def test_finish_only_polls_the_pin_this_browser_started(client, monkeypatch):
    monkeypatch.setattr(api_mod, "PlexTV", lambda: FakePlexTV(ALICE))
    r = client.post("/api/auth/finish", json={"pin_id": 777})  # no pin cookie → refused
    assert r.status_code == 400 and not client.cookies.get(COOKIE)


def test_pending_sign_in_says_not_done(client, monkeypatch):
    monkeypatch.setattr(api_mod, "PlexTV", lambda: FakePlexTV(ALICE, approved=False))
    client.post("/api/auth/start", json={})
    assert client.post("/api/auth/finish", json={}).json() == {"done": False}


def test_non_members_cannot_sign_in(client, monkeypatch):
    monkeypatch.setattr(api_mod, "PlexTV", lambda: FakePlexTV(999))
    client.post("/api/auth/start", json={})
    assert client.post("/api/auth/finish", json={}).status_code == 403
    assert not client.cookies.get(COOKIE)


def test_removed_members_sessions_stop_working(client):
    assert as_user(client, GONE).get("/api/me").status_code == 401


def test_tampered_cookie_is_refused(client):
    client.cookies.set(COOKIE, make_session(ALICE)[:-2] + "xx")
    assert client.get("/api/me").status_code == 401


def test_writes_must_be_json(client):
    r = as_user(client, ALICE).post("/api/settings", data={"phone_alerts": "true"})
    assert r.status_code == 415


# ── scoping ──────────────────────────────────────────────────────────────


def test_each_person_sees_only_their_own_data(client):
    as_user(client, ALICE)
    assert [x["name"] for x in client.get("/api/following").json()["shows"]] == ["Alice Show"]
    assert [x["title"] for x in client.get("/api/recs").json()["picks"]] == ["Alice Pick"]
    as_user(client, BOB)
    assert [x["name"] for x in client.get("/api/following").json()["shows"]] == ["Bob Show"]
    assert [x["title"] for x in client.get("/api/recs").json()["picks"]] == ["Bob Pick"]


def test_thumbs_down_hides_a_pick_for_that_person_only(client):
    as_user(client, BOB).post("/api/recs/feedback", json={"tmdb_id": 60, "media_type": "tv", "value": -1})
    assert client.get("/api/recs").json()["picks"] == []
    assert len(as_user(client, ALICE).get("/api/recs").json()["picks"]) == 1


def test_members_page_is_owner_only_and_has_no_viewing_data(client):
    assert as_user(client, BOB).get("/api/members").status_code == 403
    rows = as_user(client, ALICE).get("/api/members").json()["members"]
    assert {r["username"] for r in rows} == {"alice", "bob", "gone"}
    assert all(set(r) == {"username", "signed_in", "watchlist_connected", "discord_linked",
                          "phone_alerts", "removed"} for r in rows)


def test_request_goes_to_overseerr_as_that_person(client, monkeypatch):
    calls = []

    class FakeOverseerr:
        def request(self, oid, media_type, tmdb_id):
            calls.append((oid, media_type, tmdb_id))
            return {"status": 1, "id": 9}

    monkeypatch.setattr(api_mod, "OverseerrClient", FakeOverseerr)
    assert as_user(client, ALICE).post("/api/request", json={"tmdb_id": 5, "media_type": "tv"}).status_code == 200
    assert calls == [(1, "tv", 5)]
    r = as_user(client, BOB).post("/api/request", json={"tmdb_id": 5, "media_type": "tv"})
    assert r.status_code == 400 and calls == [(1, "tv", 5)]  # Bob has no Overseerr account


# ── watchlist + library ──────────────────────────────────────────────────


def _connect(pid):
    from telly.crypto import encrypt_token
    with db.session_scope() as s:
        s.get(User, pid).plex_token_enc = encrypt_token("tok")


def test_watchlist_shows_become_follows_and_leave_with_it(env):
    _connect(ALICE)
    wl = [{"tmdb_id": 9, "media_type": "tv", "title": "Nine", "added_at": 1790000000},
          {"tmdb_id": 8, "media_type": "movie", "title": "Eight", "added_at": None}]
    with db.session_scope() as s:
        sync_watchlists(s, FakePlexTV(watch=wl), PlexDataReport())
    with db.session_scope() as s:
        assert s.get(Follow, (ALICE, 9)).source == "watchlist"
        assert s.get(Title, (9, "tv")) is not None           # will be polled
        assert s.get(Follow, (ALICE, 8)) is None             # movies aren't follows
        assert len(list(s.scalars(select(WatchlistItem)))) == 2
    with db.session_scope() as s:
        sync_watchlists(s, FakePlexTV(watch=[]), PlexDataReport())
    with db.session_scope() as s:
        assert s.get(Follow, (ALICE, 9)) is None


def test_unfollowed_watchlist_show_stays_unfollowed(env):
    _connect(ALICE)
    with db.session_scope() as s:
        s.add(Follow(plex_id=ALICE, tmdb_id=9, source="watchlist", state="unfollowed"))
    wl = [{"tmdb_id": 9, "media_type": "tv", "title": "Nine", "added_at": None}]
    with db.session_scope() as s:
        sync_watchlists(s, FakePlexTV(watch=wl), PlexDataReport())
        sync_watchlists(s, FakePlexTV(watch=[]), PlexDataReport())
    with db.session_scope() as s:
        assert s.get(Follow, (ALICE, 9)).state == "unfollowed"


def test_revoked_token_is_dropped_and_the_watchlist_frozen(env):
    _connect(ALICE)
    with db.session_scope() as s:
        s.add(WatchlistItem(plex_id=ALICE, tmdb_id=9, media_type="tv", title="Nine"))
    report = PlexDataReport()
    with db.session_scope() as s:
        sync_watchlists(s, FakePlexTV(status=401), report)
    with db.session_scope() as s:
        assert s.get(User, ALICE).plex_token_enc is None
        assert s.get(WatchlistItem, (ALICE, 9, "tv")) is not None
    assert report.dead_tokens == [ALICE]


class FakeLibPlex:
    def __init__(self, items):
        self.items = items  # rating_key -> tmdb_id

    def sections(self):
        return [{"key": "1", "type": "movie", "title": "Movies"},
                {"key": "3", "type": "movie", "title": "Personal Videos"}]

    def section_items(self, key):
        return [{"rating_key": rk, "title": rk, "year": None} for rk in self.items] if key == "1" else []

    def item(self, rk):
        tmdb = self.items.get(rk)
        return {"Guid": [{"id": f"tmdb://{tmdb}"}]} if tmdb else {"Guid": []}


def test_library_mirror_adds_and_removes(env):
    with db.session_scope() as s:
        sync_library(s, FakeLibPlex({"10": 100, "11": 101, "12": None}), PlexDataReport())
    with db.session_scope() as s:
        assert {r.tmdb_id for r in s.scalars(select(LibraryItem))} == {100, 101}
        assert s.get(Title, (100, "movie")) is None  # the library doesn't create titles
        assert s.get(PlexItem, "12").match_status == "no_tmdb_guid"
    with db.session_scope() as s:
        sync_library(s, FakeLibPlex({"10": 100}), PlexDataReport())
    with db.session_scope() as s:
        assert {r.tmdb_id for r in s.scalars(select(LibraryItem))} == {100}


def test_imdb_upload_lands_in_the_uploaders_profile_only(client, monkeypatch):
    from telly import api as api_mod
    monkeypatch.setattr(api_mod, "_resolve_imdb_now", lambda pid: None)  # matching is tested in test_taste
    csv = "Const,Your Rating,Date Rated,Title,Title Type\ntt0306414,10,2024-03-02,The Wire,TV Series\n"
    r = as_user(client, BOB).post("/api/taste/imdb", json={"csv": csv})
    assert r.status_code == 200 and r.json()["added"] == 1
    assert client.get("/api/taste").json()["imdb_pending"] == 1
    assert as_user(client, ALICE).get("/api/taste").json()["imdb_pending"] == 0


def test_imdb_upload_rejects_non_exports_and_non_json(client):
    as_user(client, ALICE)
    r = client.post("/api/taste/imdb", json={"csv": "name,year\nx,1\n"})
    assert r.status_code == 400 and "Const" in r.json()["detail"]
    assert client.post("/api/taste/imdb", files={"file": ("r.csv", b"Const\n")}).status_code == 415


def test_taste_toggle_and_told_are_the_callers_own(client, monkeypatch):
    from telly import api as api_mod, taste as taste_mod
    monkeypatch.setattr(taste_mod, "_details", lambda s, tmdb, tid, mt: ("The Wire", "/p.jpg"))
    as_user(client, ALICE)
    assert client.post("/api/taste/told", json={"tmdb_id": 1438, "media_type": "tv"}).json()["liked"] is True
    items = client.get("/api/taste/items").json()["items"]
    assert [(i["title"], i["sources"], i["use_for_picks"]) for i in items] == [("The Wire", ["told"], True)]
    assert client.post("/api/taste/use", json={"tmdb_id": 1438, "media_type": "tv", "use": False}).json()["use"] is False
    assert client.get("/api/taste/items").json()["items"][0]["use_for_picks"] is False
    as_user(client, BOB)
    assert client.get("/api/taste/items").json()["items"] == []
    assert client.post("/api/taste/use", json={"tmdb_id": 1438, "media_type": "tv", "use": True}).status_code == 404


NEWBIE = 444


class FirstPicksTMDB:
    def recommendations(self, media_type, tmdb_id):
        assert (media_type, tmdb_id) == ("tv", 1438)
        return [{"id": 1100, "name": "Like The Wire", "vote_count": 900, "vote_average": 8.0}]

    def trending(self, media_type, window="week"):
        return [{"id": 900 if media_type == "tv" else 901, "name": f"Big {media_type}",
                 "vote_count": 900, "vote_average": 7.0}]


def test_a_new_member_gets_trending_at_sign_in_and_picks_once_they_say_what_they_like(client, monkeypatch):
    from telly import recs, taste as taste_mod
    monkeypatch.setattr(recs, "TMDBClient", FirstPicksTMDB)
    monkeypatch.setattr(taste_mod, "_details", lambda s, tmdb, tid, mt: ("The Wire", "/p.jpg"))
    with db.session_scope() as s:
        s.add(User(plex_id=NEWBIE, pms_account_id=NEWBIE, username="newbie", overseerr_id=9))
    monkeypatch.setattr(api_mod, "PlexTV", lambda: FakePlexTV(NEWBIE))
    client.post("/api/auth/start", json={})
    assert client.post("/api/auth/finish", json={}).json()["done"] is True

    home = client.get("/api/home").json()
    assert home["for_you"] == [] and home["needs_taste"] is True
    assert {p["title"] for p in home["trending"]} == {"Big tv", "Big movie"}

    client.post("/api/taste/told", json={"tmdb_id": 1438, "media_type": "tv"})
    home = client.get("/api/home").json()
    assert home["for_you"][0]["title"] == "Like The Wire" and home["needs_taste"] is False


def test_the_taste_prompt_can_be_closed_and_stays_closed(client):
    with db.session_scope() as s:
        s.add(User(plex_id=NEWBIE, pms_account_id=NEWBIE, username="newbie"))
    as_user(client, NEWBIE)
    assert client.get("/api/home").json()["needs_taste"] is True
    assert client.post("/api/home/taste-prompt/dismiss", json={}).json()["ok"] is True
    assert client.get("/api/home").json()["needs_taste"] is False
    as_user(client, BOB)  # someone else's prompt is untouched (Bob has no taste either)
    assert client.get("/api/home").json()["needs_taste"] is True


def test_plexbot_token_is_for_the_callers_own_overseerr_account(client, monkeypatch):
    import base64, json as _json
    monkeypatch.setenv("TELLY_PLEXBOT_AUTH_SECRET", "plexbot-secret")
    get_settings.cache_clear()
    r = as_user(client, ALICE).get("/api/plexbot-token")
    assert r.status_code == 200
    payload = r.json()["token"].split(".")[0]
    claims = _json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["id"] == "1" and claims["exp"] - claims["iat"] == 3600  # Alice's Overseerr id
    assert as_user(client, BOB).get("/api/plexbot-token").status_code == 404  # no Overseerr account
    client.cookies.clear()
    assert client.get("/api/plexbot-token").status_code == 401


def test_plexbot_token_refuses_when_unconfigured(client):
    assert as_user(client, ALICE).get("/api/plexbot-token").status_code == 503

