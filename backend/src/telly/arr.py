"""Download status for a title, safe to show any member (docs/spec-arr.md §2, §8.2).

TMDB id → Radarr movie (by tmdbId) / Sonarr series (by tmdbId, which Sonarr 4 carries, else
by tvdbId from TMDB's external_ids). The libraries and queues are small (57 movies, 84 series
on 2026-11-22), so each app's whole library and queue is fetched once and cached for 60 s.

What a member sees is about the title, never a person: one of
  downloading (with % and ETA) | missing (monitored, no file) | available (has a file, or in
  Plex) | unmonitored | not_in_library
plus quality. No requester, release name, indexer or download client ever leaves this file.
Read-only: nothing here writes to Radarr or Sonarr (spec §8.3).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .clients.radarr import RadarrClient
from .clients.sonarr import SonarrClient
from .config import get_settings
from .models import LibraryItem

log = logging.getLogger(__name__)

TTL = 60.0
STATES = ("downloading", "missing", "available", "unmonitored", "not_in_library")
TONE = {"available": "good", "downloading": "accent", "missing": "bad"}  # the web chip's data-tone

_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}
_tvdb: dict[int, int | None] = {}  # TMDB tv id → tvdb id, for series Sonarr has no tmdbId on


def configured(media_type: str) -> bool:
    s = get_settings()
    return bool(s.radarr_api_key if media_type == "movie" else s.sonarr_api_key)


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _tvdb.clear()


def _cached(key: str, fetch: Callable[[], object]):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    value = fetch()
    with _lock:
        _cache[key] = (now, value)
    return value


# Tests swap these for fakes (the fake *arr in tests/test_arr.py).
def radarr() -> RadarrClient:
    return RadarrClient()


def sonarr() -> SonarrClient:
    return SonarrClient()


def _movies() -> dict[int, dict]:
    return _cached("radarr:movies", lambda: {m["tmdbId"]: m for m in radarr().movies() if m.get("tmdbId")})


def _series() -> tuple[dict[int, dict], dict[int, dict]]:
    def fetch():
        rows = sonarr().series()
        return ({x["tmdbId"]: x for x in rows if x.get("tmdbId")},
                {x["tvdbId"]: x for x in rows if x.get("tvdbId")})
    return _cached("sonarr:series", fetch)


def _queue(app: str) -> list[dict]:
    return _cached(f"{app}:queue", lambda: (radarr() if app == "radarr" else sonarr()).queue())


def _quality(q) -> str | None:
    """A quality as *arr sends it: {"quality": {"name": …}} in the API, a plain name in webhooks."""
    if isinstance(q, str):
        return q or None
    if isinstance(q, dict):
        inner = q.get("quality")
        return _quality(inner) if inner is not None else q.get("name")
    return None


def _eta_text(eta: datetime | None, now: datetime) -> str | None:
    if eta is None:
        return None
    mins = int((eta - now).total_seconds() // 60)
    if mins < 1:
        return None
    if mins < 60:
        return f"~{mins} min"
    hours = mins / 60
    return f"~{hours:.0f} h" if hours < 48 else f"~{hours / 24:.0f} days"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _downloading(items: list[dict], now: datetime) -> dict:
    # Sonarr/Radarr report size 1 until the client knows the real size (seen live 2026-11-22)
    sized = [i for i in items if (i.get("size") or 0) > 1]
    size = sum(i["size"] for i in sized)
    left = sum(max(i.get("sizeleft") or 0, 0) for i in sized)
    pct = round(100 * (size - left) / size) if size else None
    finishing = all((i.get("status") or "").lower() == "completed" for i in items)
    etas = [e for e in (_parse(i.get("estimatedCompletionTime")) for i in items) if e and e > now]
    eta = max(etas) if etas and not finishing else None
    if finishing:
        label = "Importing"
    else:
        label = f"Downloading {pct}%" if pct is not None else "Queued"
        if (t := _eta_text(eta, now)):
            label += f" · {t}"
    if len(items) > 1:
        label += f" · {len(items)} files"
    return {"state": "downloading", "label": label, "progress": pct,
            "eta": eta.isoformat() if eta else None}


def _out(state: str, label: str, **extra) -> dict:
    return {"state": state, "label": label, "tone": TONE.get(state, "muted"), "progress": None,
            "eta": None, "quality": None, **extra}


def movie_status(s: Session, tmdb_id: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    in_plex = s.get(LibraryItem, (tmdb_id, "movie")) is not None
    m = _movies().get(tmdb_id)
    if m is None:
        return _out("available", "In Plex") if in_plex else _out("not_in_library", "Not in the library")
    q = [i for i in _queue("radarr") if i.get("movieId") == m["id"]]
    if q:
        return {**_out("downloading", ""), **_downloading(q, now)}
    quality = _quality((m.get("movieFile") or {}).get("quality"))
    if m.get("hasFile") or in_plex:
        return _out("available", "In Plex" + (f" · {quality}" if quality else ""), quality=quality)
    if not m.get("monitored"):
        return _out("unmonitored", "Not being looked for")
    return _out("missing", "Wanted · not out yet" if m.get("isAvailable") is False else "Missing")


def _series_for(tmdb_id: int, tvdb_of: Callable[[int], int | None] | None) -> dict | None:
    by_tmdb, by_tvdb = _series()
    if tmdb_id in by_tmdb:
        return by_tmdb[tmdb_id]
    if tvdb_of is None:
        return None
    if tmdb_id not in _tvdb:
        try:
            _tvdb[tmdb_id] = tvdb_of(tmdb_id)
        except Exception as e:  # noqa: BLE001 — TMDB down: just say not found this time
            log.warning("tvdb lookup for %s failed: %s", tmdb_id, e)
            return None
    tvdb = _tvdb[tmdb_id]
    return by_tvdb.get(tvdb) if tvdb else None


def tv_status(s: Session, tmdb_id: int, tvdb_of: Callable[[int], int | None] | None = None,
              now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    in_plex = s.get(LibraryItem, (tmdb_id, "tv")) is not None
    x = _series_for(tmdb_id, tvdb_of)
    if x is None:
        return _out("available", "In Plex") if in_plex else _out("not_in_library", "Not in the library")
    q = [i for i in _queue("sonarr") if i.get("seriesId") == x["id"]]
    if q:
        return {**_out("downloading", ""), **_downloading(q, now)}
    st = x.get("statistics") or {}
    files, wanted = st.get("episodeFileCount") or 0, st.get("episodeCount") or 0
    if x.get("monitored") and files < wanted:
        n = wanted - files
        return _out("missing", f"{n} episode{'s' if n != 1 else ''} missing", missing=n)
    if files or in_plex:
        return _out("available", "In Plex")
    if not x.get("monitored"):
        return _out("unmonitored", "Not being looked for")
    return _out("missing", "Wanted · nothing aired yet" if not wanted else "Missing")


def tmdb_tvdb(tmdb) -> Callable[[int], int | None]:
    """tvdb id from TMDB's external_ids (TMDBClient.tv appends them)."""
    return lambda tmdb_id: ((tmdb.tv(tmdb_id) or {}).get("external_ids") or {}).get("tvdb_id")


def status(s: Session, media_type: str, tmdb_id: int, tmdb=None) -> dict | None:
    """The member-safe status, or None when that app isn't set up or can't be reached (the UI
    then shows no chip; it never guesses)."""
    if media_type not in ("tv", "movie") or not configured(media_type):
        return None
    try:
        if media_type == "movie":
            return movie_status(s, tmdb_id)
        return tv_status(s, tmdb_id, tmdb_tvdb(tmdb) if tmdb is not None else None)
    except Exception as e:  # noqa: BLE001 — *arr down must never break a page
        log.warning("arr status %s/%s failed: %s", media_type, tmdb_id, type(e).__name__)
        return None


def statuses(s: Session, media_type: str, tmdb_ids: list[int]) -> dict[int, dict]:
    """Many at once (Following). Uses only the cached library, no per-title TMDB lookups."""
    if not configured(media_type):
        return {}
    out = {}
    for t in tmdb_ids:
        st = status(s, media_type, t)
        if st is not None:
            out[t] = st
    return out
