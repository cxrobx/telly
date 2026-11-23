"""Radarr/Sonarr (docs/spec-arr.md): member status, webhooks → alerts, the owner's APNs.

The *arr here is fake (shapes copied from Radarr 6.0.4 / Sonarr 4.0.16 on 2026-11-22), so no
test can touch the NAS. All ids are made up.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from telly import apns, arr, arrhook, db, requester
from telly.alerts import deliver
from telly.config import get_settings
from telly.crypto import COOKIE, make_session
from telly.models import (AlertPref, ApnsDevice, ArrNotice, Delivery, Event, Follow, IdentityLink,
                          LibraryItem, TasteSignal, Title, User, WatchlistItem, utcnow)
from telly.shows import describe

OWNER, BOB, CARA = 111, 222, 333
SECRET = "hook-secret"
NOW = datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc)
TOKEN = "ab" * 32

# ── a fake Radarr / Sonarr ───────────────────────────────────────────────


MOVIES = [
    {"id": 7, "tmdbId": 900, "title": "Got It", "monitored": True, "hasFile": True, "isAvailable": True,
     "movieFile": {"quality": {"quality": {"id": 7, "name": "Bluray-1080p"}}}},
    {"id": 8, "tmdbId": 901, "title": "Coming", "monitored": True, "hasFile": False, "isAvailable": True},
    {"id": 9, "tmdbId": 902, "title": "Wanted", "monitored": True, "hasFile": False, "isAvailable": True},
    {"id": 10, "tmdbId": 903, "title": "Shelved", "monitored": False, "hasFile": False, "isAvailable": True},
    {"id": 11, "tmdbId": 904, "title": "Unreleased", "monitored": True, "hasFile": False, "isAvailable": False},
]
SERIES = [
    {"id": 20, "tmdbId": 1, "tvdbId": 501, "title": "Alice Show", "monitored": True,
     "statistics": {"episodeFileCount": 10, "episodeCount": 10, "totalEpisodeCount": 20}},
    {"id": 21, "tmdbId": 2, "tvdbId": 502, "title": "Gappy", "monitored": True,
     "statistics": {"episodeFileCount": 7, "episodeCount": 10}},
    {"id": 22, "tmdbId": 3, "tvdbId": 503, "title": "Pulling", "monitored": True,
     "statistics": {"episodeFileCount": 2, "episodeCount": 2}},
    {"id": 23, "tmdbId": None, "tvdbId": 504, "title": "Old Payload", "monitored": False,
     "statistics": {"episodeFileCount": 0, "episodeCount": 0}},
]
EXTRA = {"downloadClient": "SECRET-CLIENT", "indexer": "SECRET-INDEXER", "title": "Release.Name-GRP",
         "outputPath": "/volume1/secret/path", "downloadId": "SABnzbd_nzo_x"}
RQUEUE = [{"movieId": 8, "size": 1000, "sizeleft": 600, "status": "downloading",
           "estimatedCompletionTime": (NOW + timedelta(minutes=12, seconds=30)).isoformat().replace("+00:00", "Z"),
           **EXTRA}]
SQUEUE = [{"seriesId": 22, "episodeId": 1, "seasonNumber": 1, "size": 500, "sizeleft": 0, "status": "completed",
           **EXTRA},
          {"seriesId": 22, "episodeId": 2, "seasonNumber": 1, "size": 500, "sizeleft": 0, "status": "completed",
           **EXTRA}]


class FakeArr:
    def __init__(self, items, queue, kind):
        self.items, self.q, self.kind, self.calls = items, queue, kind, 0

    def movies(self):
        self.calls += 1
        return self.items

    series = movies

    def queue(self):
        return self.q


class FakeSeerr:
    def __init__(self, who=()):
        self.who = list(who)

    def requesters(self, media_type, tmdb_id):
        return self.who


class FakeTMDB:
    def tv(self, tmdb_id):
        return {"id": tmdb_id, "external_ids": {"tvdb_id": 504 if tmdb_id == 4 else None}}


@pytest.fixture
def fake_arr(monkeypatch):
    radarr, sonarr = FakeArr(MOVIES, RQUEUE, "radarr"), FakeArr(SERIES, SQUEUE, "sonarr")
    monkeypatch.setattr(arr, "radarr", lambda: radarr)
    monkeypatch.setattr(arr, "sonarr", lambda: sonarr)
    arr.clear_cache()
    yield radarr, sonarr
    arr.clear_cache()


@pytest.fixture
def env(tmp_path, monkeypatch, fake_arr):
    monkeypatch.setenv("TELLY_DATABASE_URL", f"sqlite:///{tmp_path / 'a.db'}")
    monkeypatch.setenv("TELLY_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("TELLY_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TELLY_SHARED_SECRET", "x")
    monkeypatch.setenv("TELLY_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TELLY_LLM_ENABLED", "false")
    monkeypatch.setenv("TELLY_PUBLIC_URL", "https://telly.test")
    monkeypatch.setenv("TELLY_RADARR_API_KEY", "fake")
    monkeypatch.setenv("TELLY_SONARR_API_KEY", "fake")
    monkeypatch.setenv("TELLY_ARR_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELLY_OVERSEERR_API_KEY", "")
    monkeypatch.setenv("TELLY_APNS_KEY_P8", "")
    monkeypatch.setenv("TELLY_APNS_KEY_ID", "")
    get_settings.cache_clear()
    db._engine = db._factory = None
    db.Base.metadata.create_all(db.get_engine())
    with db.session_scope() as s:
        s.add_all([User(plex_id=OWNER, pms_account_id=1, username="owner", is_owner=True, overseerr_id=1),
                   User(plex_id=BOB, pms_account_id=BOB, username="bob", overseerr_id=2),
                   User(plex_id=CARA, pms_account_id=CARA, username="cara")])
        s.flush()
        s.add_all([Title(tmdb_id=1, media_type="tv", name="Alice Show", status="Returning Series"),
                   Title(tmdb_id=3, media_type="tv", name="Pulling", status="Returning Series"),
                   Follow(plex_id=OWNER, tmdb_id=1, source="manual"),
                   Follow(plex_id=BOB, tmdb_id=3, source="inferred"),
                   AlertPref(plex_id=BOB, discord_enabled=True, ntfy_topic="telly-bob"),
                   AlertPref(plex_id=CARA, discord_enabled=True, ntfy_topic="telly-cara"),
                   IdentityLink(surface="discord", external_id="d-bob", plex_id=BOB, source="link")])
    monkeypatch.setattr(arrhook.apns, "_dormant_logged", False)
    from telly import app as app_mod
    monkeypatch.setattr(app_mod, "schedule_delivery", lambda: None)  # no real timers in tests
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


# ── status mapping ───────────────────────────────────────────────────────


@pytest.mark.parametrize("tmdb_id, state, label", [
    (900, "available", "In Plex · Bluray-1080p"),
    (901, "downloading", "Downloading 40% · ~12 min"),
    (902, "missing", "Missing"),
    (903, "unmonitored", "Not being looked for"),
    (904, "missing", "Wanted · not out yet"),
    (999, "not_in_library", "Not in the library"),
])
def test_movie_status(env, tmdb_id, state, label):
    with db.session_scope() as s:
        st = arr.movie_status(s, tmdb_id, now=NOW)
    assert (st["state"], st["label"]) == (state, label)
    assert st["tone"] == {"available": "good", "downloading": "accent", "missing": "bad"}.get(state, "muted")


def test_downloading_carries_progress_and_eta_and_nothing_about_the_release(env):
    with db.session_scope() as s:
        st = arr.movie_status(s, 901, now=NOW)
    assert st["progress"] == 40 and st["eta"].startswith("2026-11-22T20:12")
    blob = json.dumps(st)
    for leak in ("SECRET", "Release.Name", "/volume1", "SABnzbd", "requested"):
        assert leak not in blob


def test_in_plex_without_arr_is_available(env):
    with db.session_scope() as s:
        s.add(LibraryItem(tmdb_id=777, media_type="movie", rating_key="k"))
        s.flush()
        assert arr.movie_status(s, 777)["state"] == "available"


@pytest.mark.parametrize("tmdb_id, state, label", [
    (1, "available", "In Plex"),
    (2, "missing", "3 episodes missing"),
    (3, "downloading", "Importing · 2 files"),
    (99, "not_in_library", "Not in the library"),
])
def test_tv_status(env, tmdb_id, state, label):
    with db.session_scope() as s:
        st = arr.tv_status(s, tmdb_id, now=NOW)
    assert (st["state"], st["label"]) == (state, label)


def test_series_without_a_tmdb_id_is_found_by_tvdb(env):
    with db.session_scope() as s:
        assert arr.tv_status(s, 4)["state"] == "not_in_library"  # no TMDB lookup offered
        assert arr.tv_status(s, 4, arr.tmdb_tvdb(FakeTMDB()))["state"] == "unmonitored"


def test_library_is_cached_for_a_minute(env, fake_arr):
    radarr, _ = fake_arr
    with db.session_scope() as s:
        arr.status(s, "movie", 900)
        arr.status(s, "movie", 901)
    assert radarr.calls == 1


def test_no_key_or_arr_down_means_no_status(env, monkeypatch):
    with db.session_scope() as s:
        monkeypatch.setattr(arr, "radarr", lambda: (_ for _ in ()).throw(httpx.ConnectError("down")))
        assert arr.status(s, "movie", 900) is None
        monkeypatch.setenv("TELLY_SONARR_API_KEY", "")
        get_settings.cache_clear()
        assert arr.status(s, "tv", 1) is None


def test_status_route_needs_a_session(client):
    assert client.get("/api/arr/status/movie/900").status_code == 401
    r = as_user(client, CARA).get("/api/arr/status/movie/901")
    assert r.status_code == 200 and r.json()["status"]["state"] == "downloading"
    assert as_user(client, CARA).get("/api/arr/status/book/1").status_code == 422


def test_following_cards_carry_status(client):
    shows = as_user(client, BOB).get("/api/following").json()["shows"]
    assert [(x["name"], x["download"]["state"]) for x in shows] == [("Pulling", "downloading")]


def test_no_write_route_to_radarr_or_sonarr_exists(client):
    """spec-arr §8.3: grab, delete and monitor live in the admin app, never in Telly."""
    from telly.app import create_app
    for route in create_app().routes:
        path, methods = getattr(route, "path", ""), getattr(route, "methods", set()) or set()
        if "arr" in path and path != "/internal/arr-webhook/{app_name}":
            assert methods <= {"GET", "HEAD"}, path
    for cls in (arr.RadarrClient, arr.SonarrClient):
        for name in dir(cls):
            assert not name.startswith(("post", "put", "delete", "grab", "add", "monitor")), name


# ── webhook auth ─────────────────────────────────────────────────────────


def basic(password, user="telly"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def hook(client, app="radarr", body=None, headers=None):
    return client.post(f"/internal/arr-webhook/{app}", json=body or {"eventType": "Test"},
                       headers=basic(SECRET) if headers is None else headers)


@pytest.mark.parametrize("headers", [{}, basic("wrong"), basic(""), {"Authorization": "Bearer " + SECRET},
                                     {"Authorization": "Basic !!!notbase64"}])
def test_webhook_refuses_a_missing_or_wrong_secret(client, headers):
    assert hook(client, headers=headers).status_code == 401
    with db.session_scope() as s:
        assert s.scalar(select(func.count()).select_from(ArrNotice)) == 0


def test_webhook_with_no_secret_configured_refuses_everyone(client, monkeypatch):
    monkeypatch.setenv("TELLY_ARR_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    assert hook(client, headers=basic("")).status_code == 401


def test_webhook_is_not_reachable_through_cloudflare_or_for_other_apps(client):
    assert hook(client, headers={**basic(SECRET), "cf-connecting-ip": "1.2.3.4"}).status_code == 404
    assert hook(client, app="lidarr").status_code == 404


def test_webhook_test_event_is_logged(client, caplog):
    with caplog.at_level(logging.INFO, logger="telly.arrhook"):
        r = hook(client, app="sonarr")
    assert r.status_code == 200 and r.json()["kind"] == "test"
    assert "test from sonarr received" in caplog.text


# ── event mapping, dedupe, fan-out ───────────────────────────────────────


def radarr_download(tmdb_id=902, movie_id=9, file_id=55, upgrade=False):
    return {"eventType": "Download", "isUpgrade": upgrade, "instanceName": "Radarr",
            "movie": {"id": movie_id, "title": "Wanted", "year": 2026, "tmdbId": tmdb_id, "folderPath": "/movies/x"},
            "movieFile": {"id": file_id, "quality": "WEBDL-2160p", "relativePath": "Wanted.mkv",
                          "sceneName": "Wanted.2026.2160p-GRP"},
            "release": {"releaseTitle": "Wanted.2026.2160p-GRP", "indexer": "SECRET-INDEXER"},
            "downloadClient": "SECRET-CLIENT"}


def sonarr_download(eps, file_id, series_id=22, tmdb_id=3, upgrade=False):
    return {"eventType": "Download", "isUpgrade": upgrade,
            "series": {"id": series_id, "title": "Pulling", "tvdbId": 503, "tmdbId": tmdb_id, "year": 2025},
            "episodes": [{"id": 100 + e, "seasonNumber": 1, "episodeNumber": e, "title": f"Ep {e}"} for e in eps],
            "episodeFile": {"id": file_id, "quality": "WEBDL-1080p", "relativePath": "x.mkv"}}


class Sent:
    def __init__(self):
        self.msgs = []

    def send(self, *args, **kw):
        self.msgs.append(args)


def test_a_requested_movie_download_alerts_the_requester_once(env):
    with db.session_scope() as s:
        out = arrhook.handle(s, "radarr", radarr_download(), FakeSeerr(who=[2]))
        assert out["kind"] == "download" and out["member_event"]
        # Seerr's requester became Bob's own taste signal, the row the nightly sync makes too
        assert s.get(TasteSignal, (BOB, 902, "movie", "overseerr")) is not None
        ntfy = Sent()
        r = deliver(s, None, ntfy)
        assert r.sent == 1 and ntfy.msgs == [("telly-bob", "Telly", "Wanted is in Plex")]
        assert arrhook.handle(s, "radarr", radarr_download(), FakeSeerr(who=[2]))["duplicate"] is True
        assert deliver(s, None, ntfy).sent == 0
        assert s.scalar(select(func.count()).select_from(Event).where(Event.kind == "in_plex")) == 1


def test_a_watchlisted_movie_alerts_that_person_and_nobody_else(env):
    with db.session_scope() as s:
        s.add(WatchlistItem(plex_id=CARA, tmdb_id=902, media_type="movie", title="Wanted"))
        s.flush()
        arrhook.handle(s, "radarr", radarr_download(), FakeSeerr())
        ntfy = Sent()
        deliver(s, None, ntfy)
        assert [m[0] for m in ntfy.msgs] == ["telly-cara"]


def test_a_tv_watchlist_with_the_same_tmdb_id_isnt_a_movie_follower(env):
    with db.session_scope() as s:
        s.add(WatchlistItem(plex_id=CARA, tmdb_id=902, media_type="tv", title="Other"))
        s.flush()
        arrhook.handle(s, "radarr", radarr_download(), FakeSeerr())
        assert deliver(s, None, Sent()).sent == 0


def test_an_upgrade_tells_only_the_owner(env):
    with db.session_scope() as s:
        out = arrhook.handle(s, "radarr", radarr_download(upgrade=True), FakeSeerr(who=[2]))
        assert out["kind"] == "upgrade" and "member_event" not in out
        assert s.scalar(select(func.count()).select_from(Event)) == 0


@pytest.mark.parametrize("app, body, kind", [
    ("radarr", {"eventType": "Grab", "movie": {"id": 9, "title": "Wanted", "tmdbId": 902},
                "release": {"quality": "WEBDL-2160p", "releaseTitle": "Wanted.2026-GRP"}, "downloadId": "abc"}, "grab"),
    ("radarr", {"eventType": "MovieDelete", "movie": {"id": 9, "title": "Wanted", "tmdbId": 902},
                "deletedFiles": True}, "delete"),
    ("sonarr", {"eventType": "SeriesDelete", "series": {"id": 22, "title": "Pulling", "tmdbId": 3}}, "delete"),
    ("sonarr", {"eventType": "Health", "level": "warning", "type": "IndexerStatusCheck",
                "message": "Indexers unavailable: SECRET-INDEXER"}, "health"),
])
def test_owner_events_are_stored_without_member_events(env, app, body, kind):
    with db.session_scope() as s:
        out = arrhook.handle(s, app, body, FakeSeerr(who=[2]))
        assert out["kind"] == kind
        n = s.scalar(select(ArrNotice))
        assert n.kind == kind and "SECRET" not in n.detail and "GRP" not in n.detail
        assert s.scalar(select(func.count()).select_from(Event)) == 0
        assert arrhook.handle(s, app, body)["duplicate"] is True


def test_unknown_events_are_ignored(env):
    with db.session_scope() as s:
        assert arrhook.handle(s, "radarr", {"eventType": "Rename"})["ignored"] == "Rename"


def test_a_season_pack_is_one_alert_for_followers(env):
    with db.session_scope() as s:
        for e in (1, 2, 3):
            arrhook.handle(s, "sonarr", sonarr_download([e], file_id=60 + e), FakeSeerr())
        evs = list(s.scalars(select(Event).where(Event.kind == "in_plex")))
        assert len(evs) == 1 and evs[0].payload["episodes"] == [[1, 1], [1, 2], [1, 3]]
        ntfy = Sent()
        deliver(s, None, ntfy)
        assert ntfy.msgs == [("telly-bob", "Telly", "Pulling S1E1–E3 are in Plex")]
        # after it went out, the next episode is news of its own
        arrhook.handle(s, "sonarr", sonarr_download([4], file_id=64), FakeSeerr())
        deliver(s, None, ntfy)
        assert ntfy.msgs[-1] == ("telly-bob", "Telly", "Pulling S1E4 is in Plex")


def test_an_unfollowed_show_stays_quiet_even_if_requested(env):
    with db.session_scope() as s:
        s.get(Follow, (BOB, 3)).state = "unfollowed"
        s.flush()
        arrhook.handle(s, "sonarr", sonarr_download([1], file_id=61), FakeSeerr(who=[2]))
        assert deliver(s, None, Sent()).sent == 0


def test_in_plex_shows_up_in_whats_new_for_followers(client):
    with db.session_scope() as s:
        arrhook.handle(s, "sonarr", sonarr_download([5], file_id=65), FakeSeerr())
    news = as_user(client, BOB).get("/api/whats-new").json()["events"]
    assert [n["summary"] for n in news] == ["Pulling S1E5 is in Plex"]
    assert as_user(client, CARA).get("/api/whats-new").json()["events"] == []


def test_a_movie_event_never_lands_on_a_show_with_the_same_tmdb_id(client):
    with db.session_scope() as s:
        arrhook.handle(s, "radarr", radarr_download(tmdb_id=1, movie_id=99), FakeSeerr())
    assert as_user(client, OWNER).get("/api/whats-new").json()["events"] == []


def test_describe_in_plex():
    assert describe("in_plex", {"title": "Dune"}, "Dune") == "Dune is in Plex"
    assert describe("in_plex", {"episodes": [[2, 3]]}, "X") == "X S2E3 is in Plex"
    assert describe("in_plex", {"episodes": [[1, 9], [2, 1]]}, "X") == "X: 2 new episodes are in Plex"


def test_webhook_through_the_app(client):
    r = hook(client, body=radarr_download())
    assert r.status_code == 200 and r.json()["member_event"]


# ── owner-only APNs registration ─────────────────────────────────────────


def test_apns_register_is_owner_only(client):
    assert client.post("/api/apns/register", json={"token": TOKEN}).status_code == 401
    assert as_user(client, BOB).post("/api/apns/register", json={"token": TOKEN}).status_code == 403
    r = as_user(client, OWNER).post("/api/apns/register", json={"token": TOKEN.upper(), "environment": "development"})
    assert r.status_code == 200 and r.json() == {"on": True, "configured": False}
    as_user(client, OWNER).post("/api/apns/register", json={"token": TOKEN, "environment": "production"})
    with db.session_scope() as s:
        rows = list(s.scalars(select(ApnsDevice)))
        assert [(d.token, d.environment, d.plex_id) for d in rows] == [(TOKEN, "production", OWNER)]
    assert as_user(client, OWNER).post("/api/apns/register", json={"token": "zz" * 32}).status_code == 422
    assert as_user(client, OWNER).post("/api/apns/register", json={"token": TOKEN, "environment": "x"}).status_code == 422
    assert as_user(client, OWNER).post("/api/apns/register", content=f'{{"token":"{TOKEN}"}}',
                                       headers={"content-type": "text/plain"}).status_code == 415
    assert as_user(client, BOB).post("/api/apns/unregister", json={"token": TOKEN}).status_code == 403
    assert as_user(client, OWNER).post("/api/apns/unregister", json={"token": TOKEN}).json() == {"on": False}
    with db.session_scope() as s:
        assert s.scalar(select(func.count()).select_from(ApnsDevice)) == 0


# ── APNs sending ─────────────────────────────────────────────────────────


@pytest.fixture
def key(env, monkeypatch):
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption())
    monkeypatch.setenv("TELLY_APNS_KEY_P8", base64.b64encode(pem).decode())
    monkeypatch.setenv("TELLY_APNS_KEY_ID", "ABC123DEFG")
    get_settings.cache_clear()
    monkeypatch.setattr(apns, "_cached", None)
    return k


def _unb64u(t):
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


def _devices(s, *rows):
    for pid, env_name, token in rows:
        s.add(ApnsDevice(token=token, plex_id=pid, environment=env_name))
    s.flush()


def test_apns_is_dormant_without_a_key(env, monkeypatch, caplog):
    monkeypatch.setattr(apns, "client", lambda: pytest.fail("no key, no request"))
    with db.session_scope() as s:
        _devices(s, (OWNER, "production", TOKEN))
        with caplog.at_level(logging.INFO, logger="telly.apns"):
            assert arrhook.handle(s, "radarr", radarr_download(), FakeSeerr())["pushed"] == 0
            arrhook.handle(s, "radarr", {"eventType": "Grab", "movie": {"id": 1, "title": "t"}, "downloadId": "z"})
    assert caplog.text.count("dormant") == 1


def test_provider_token_is_an_es256_jwt_apple_can_verify(key):
    head, claims, sig = apns.provider_token().split(".")
    raw = _unb64u(sig)
    key.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                            f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert json.loads(_unb64u(head)) == {"alg": "ES256", "kid": "ABC123DEFG"}
    assert json.loads(_unb64u(claims))["iss"] == "CCYV5HQZCM"
    assert apns.provider_token() == apns.provider_token()  # reused inside its life


def test_owner_alert_goes_to_his_devices_only_with_the_deeplink(key, monkeypatch):
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers, json.loads(req.content)))
        return httpx.Response(200)

    monkeypatch.setattr(apns, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    with db.session_scope() as s:
        _devices(s, (OWNER, "development", TOKEN), (BOB, "production", "cd" * 32))
        out = arrhook.handle(s, "radarr", radarr_download(), FakeSeerr())
        assert out["pushed"] == 1
        assert s.scalar(select(ArrNotice)).pushed == 1
    (url, headers, body), = seen
    assert url == f"https://api.sandbox.push.apple.com/3/device/{TOKEN}"
    assert headers["apns-topic"] == "app.telly.admin" and headers["authorization"].startswith("bearer ")
    assert body["aps"]["alert"] == {"title": "Downloaded · Wanted · WEBDL-2160p", "body": "Imported: it's in Plex."}
    assert body["aps"]["sound"] == "default"
    assert body["deeplink"] == "tellyadmin://movies/open/9"


def test_series_deeplink_and_episode_label():
    ev = arrhook.parse("sonarr", sonarr_download([1, 2], file_id=1))
    msg = arrhook.owner_message("sonarr", ev)
    assert msg["deeplink"] == "tellyadmin://series/open/22"
    assert msg["title"] == "Downloaded · Pulling · S01E01–E02 · WEBDL-1080p"


@pytest.mark.parametrize("status, reason", [(410, "Unregistered"), (400, "BadDeviceToken")])
def test_a_token_apple_calls_gone_is_forgotten(key, monkeypatch, status, reason):
    monkeypatch.setattr(apns, "client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(status, json={"reason": reason}))))
    with db.session_scope() as s:
        _devices(s, (OWNER, "production", TOKEN))
        assert apns.notify_owner(s, {"title": "t"}) == 0
        assert s.get(ApnsDevice, TOKEN) is None


def test_other_refusals_keep_the_device(key, monkeypatch):
    monkeypatch.setattr(apns, "client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(429, json={"reason": "TooManyRequests"}))))
    with db.session_scope() as s:
        _devices(s, (OWNER, "production", TOKEN))
        assert apns.notify_owner(s, {"title": "t"}) == 0
        assert s.get(ApnsDevice, TOKEN) is not None


# ── plexbot's MCP tool ───────────────────────────────────────────────────


class SearchTMDB(FakeTMDB):
    def search_multi(self, q):
        return [{"id": 901, "media_type": "movie", "title": "Coming", "release_date": "2026-01-01"}]


def _rpc(client, token, method, params=None):
    return client.post("/mcp", headers={"Accept": "application/json, text/event-stream",
                                        "Authorization": f"Bearer {token}"},
                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_download_status_tool_is_caller_scoped_and_read_only(client, monkeypatch):
    from telly import mcp_server
    monkeypatch.setattr(mcp_server, "TMDBClient", SearchTMDB)
    tok = requester.mint("discord", "d-bob", secret="x")
    tools = {t["name"]: t for t in _rpc(client, tok, "tools/list").json()["result"]["tools"]}
    props = set(tools["download_status"]["inputSchema"].get("properties", {}))
    assert props == {"title", "year", "media_type"}  # nothing that could name a person
    r = _rpc(client, tok, "tools/call", {"name": "download_status", "arguments": {"title": "Coming"}}).json()
    out = json.loads(r["result"]["content"][0]["text"])
    assert out["results"][0]["status"]["state"] == "downloading" and out["ambiguous"] is False
    stranger = requester.mint("discord", "d-stranger", secret="x")
    r = _rpc(client, stranger, "tools/call", {"name": "download_status", "arguments": {"title": "Coming"}}).json()
    assert json.loads(r["result"]["content"][0]["text"])["linked"] is False
