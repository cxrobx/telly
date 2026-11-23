"""Radarr/Sonarr webhooks → alerts (docs/spec-arr.md phase 2).

Two audiences, kept apart:
- **Members**: a Download/Import (not an upgrade) of a title they follow, have on their
  watchlist, or requested in Seerr becomes an `in_plex` Event, so it goes out through the
  existing fan-out (alerts.py: Discord DM / ntfy, at most once per person per channel).
- **The owner**: grab, download, upgrade, delete and health become an ArrNotice and an APNs
  alert to his admin app, opening the title there (tellyadmin://movies/open/{id}).

Only ids, the title, the event and quality are stored. Release names, paths, indexers,
download clients and health messages are never written down.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from . import apns
from .models import ArrNotice, Delivery, Event, TasteSignal, User, aware, utcnow

log = logging.getLogger(__name__)

APPS = ("radarr", "sonarr")
MERGE_WINDOW = timedelta(hours=2)  # a season pack's episodes, one webhook each, become one alert
VERB = {"grab": "Grabbed", "download": "Downloaded", "upgrade": "Upgraded", "delete": "Deleted",
        "health": "Health", "test": "Test"}
LINE = {"grab": "Downloading now.", "download": "Imported: it's in Plex.",
        "upgrade": "Replaced with a better copy.", "delete": "Removed from the library.",
        "test": "The webhook from {app} reached Telly."}


def _quality(q) -> str:
    if isinstance(q, dict):
        q = (q.get("quality") or {}).get("name") if isinstance(q.get("quality"), dict) else q.get("name")
    return str(q or "")


def _eps(episodes: list[dict]) -> list[tuple[int, int]]:
    return sorted({(int(e.get("seasonNumber") or 0), int(e.get("episodeNumber") or 0))
                   for e in episodes or [] if e.get("episodeNumber") is not None})


def ep_label(eps: list[tuple[int, int]]) -> str:
    if not eps:
        return ""
    (s1, e1), (s2, e2) = eps[0], eps[-1]
    if len(eps) == 1:
        return f"S{s1:02d}E{e1:02d}"
    if s1 == s2:
        return f"S{s1:02d}E{e1:02d}–E{e2:02d}"
    return f"S{s1:02d}E{e1:02d}–S{s2:02d}E{e2:02d}"


def _short(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def parse(app: str, body: dict) -> dict | None:
    """The webhook as {kind, arr_id, tmdb_id, tvdb_id, title, year, quality, episodes, key}, or
    None for an event Telly doesn't use. `key` is stable across *arr's retries."""
    et = (body.get("eventType") or "").strip()
    item = body.get("movie" if app == "radarr" else "series") or {}
    base = {"arr_id": item.get("id"), "tmdb_id": item.get("tmdbId") or None,
            "tvdb_id": item.get("tvdbId") or None, "title": (item.get("title") or "")[:200],
            "year": item.get("year"), "quality": "", "episodes": []}
    if app == "sonarr":
        base["episodes"] = _eps(body.get("episodes") or [])
    if et == "Test":
        return {**base, "kind": "test", "key": None}
    if et == "Grab":
        rel = body.get("release") or {}
        dl = body.get("downloadId") or _short(f"{rel.get('releaseTitle')}{base['episodes']}")
        return {**base, "kind": "grab", "quality": _quality(rel.get("quality")),
                "key": f"grab:{app}:{base['arr_id']}:{dl}"}
    if et == "Download":
        files = [body["movieFile" if app == "radarr" else "episodeFile"]] if body.get(
            "movieFile" if app == "radarr" else "episodeFile") else list(body.get("episodeFiles") or [])
        f = files[0] if files else {}
        kind = "upgrade" if body.get("isUpgrade") else "download"
        fid = f.get("id") or _short(str(base["episodes"]) + str(body.get("downloadId")))
        return {**base, "kind": kind, "quality": _quality(f.get("quality")),
                "key": f"{kind}:{app}:{base['arr_id']}:{fid}"}
    if et in ("MovieDelete", "SeriesDelete"):
        return {**base, "kind": "delete", "key": f"delete:{app}:{base['arr_id']}:{utcnow().date()}"}
    if et == "Health":
        level, typ = str(body.get("level") or "")[:20], str(body.get("type") or "")[:80]
        return {**base, "kind": "health", "title": f"{app.title()} {level.lower() or 'health'}",
                "quality": f"{level}: {typ}".strip(": "), "health_message": str(body.get("message") or "")[:180],
                "key": f"health:{app}:{typ}:{_short(str(body.get('message')))}:{utcnow().date()}"}
    return None


def deeplink(app: str, arr_id: int | None) -> str:
    """QuickActions.swift routes. One instance each, so no ?instance=."""
    if not arr_id:
        return "tellyadmin://open"
    return f"tellyadmin://{'movies' if app == 'radarr' else 'series'}/open/{int(arr_id)}"


def owner_message(app: str, ev: dict) -> dict:
    kind = ev["kind"]
    if kind == "health":
        return {"title": f"{app.title()} health · {ev['quality']}"[:120],
                "body": ev.get("health_message") or "Check the app.", "deeplink": "tellyadmin://activity",
                "collapse": f"health-{app}", "thread": f"{app}-health"}
    parts = [VERB[kind], ev["title"] or "Untitled"]
    if app == "sonarr" and (lab := ep_label(ev["episodes"])):
        parts.append(lab)
    if ev["quality"]:
        parts.append(ev["quality"])
    return {"title": " · ".join(parts)[:160], "body": LINE[kind].format(app=app.title()),
            "deeplink": deeplink(app, ev["arr_id"]),
            # a season pack's ten imports replace each other on the lock screen
            "collapse": f"{app}-{ev['arr_id']}-{kind}", "thread": app}


def _notice(s: Session, app: str, ev: dict) -> bool:
    """Store the owner's notice once. False when this exact event was already handled."""
    if ev["key"] is None:  # a test: always logged, never deduped
        ev = {**ev, "key": f"test:{app}:{utcnow().isoformat()}"}
    detail = ev["quality"] if ev["kind"] == "health" else " · ".join(
        x for x in (ep_label(ev["episodes"]), ev["quality"]) if x)
    res = s.execute(sqlite_insert(ArrNotice).values(
        app=app, kind=ev["kind"], arr_id=ev["arr_id"], tmdb_id=ev["tmdb_id"], title=ev["title"],
        detail=detail[:200], dedupe_key=ev["key"], received_at=utcnow(), pushed=0,
    ).on_conflict_do_nothing(index_elements=["dedupe_key"]))
    s.flush()
    return bool(res.rowcount)


def record_requesters(s: Session, media_type: str, tmdb_id: int, title: str, overseerr) -> int:
    """Who requested it in Seerr, right now (the nightly sync would add the same rows later).
    Each becomes that person's own `overseerr` taste signal: the same row sync makes, so the
    fan-out finds requesters in one place."""
    try:
        oids = set(overseerr.requesters(media_type, tmdb_id))
    except Exception as e:  # noqa: BLE001 — Seerr down: the nightly sync's rows still count
        log.warning("seerr requesters lookup failed: %s", type(e).__name__)
        return 0
    n = 0
    for u in s.scalars(select(User).where(User.overseerr_id.in_(oids), User.removed_at.is_(None))):
        if s.get(TasteSignal, (u.plex_id, tmdb_id, media_type, "overseerr")) is None:
            s.add(TasteSignal(plex_id=u.plex_id, tmdb_id=tmdb_id, media_type=media_type,
                              source="overseerr", title=title, noted_at=utcnow()))
            n += 1
    s.flush()
    return n


def _in_plex(s: Session, media_type: str, ev: dict) -> Event | None:
    """The member-facing event. Movies: once per movie, ever. Shows: one per batch of a season's
    episodes, so a pack is one alert; a later batch is a new event."""
    tmdb_id, now = ev["tmdb_id"], utcnow()
    if media_type == "movie":
        payload = {"title": ev["title"], "year": ev["year"], "quality": ev["quality"]}
        key = f"in_plex:movie:{tmdb_id}"
    else:
        if not ev["episodes"]:
            return None
        season = ev["episodes"][0][0]
        new = [list(e) for e in ev["episodes"]]
        for open_ev in s.scalars(select(Event).where(
                Event.kind == "in_plex", Event.media_type == "tv", Event.tmdb_id == tmdb_id)
                .order_by(Event.id.desc()).limit(5)):
            p = open_ev.payload or {}
            if (p.get("season") == season and now - aware(open_ev.detected_at) < MERGE_WINDOW
                    and s.scalar(select(Delivery.id).where(Delivery.event_id == open_ev.id)) is None):
                eps = sorted({tuple(e) for e in p.get("episodes", [])} | {tuple(e) for e in new})
                open_ev.payload = {**p, "episodes": [list(e) for e in eps], "quality": ev["quality"] or p.get("quality")}
                s.flush()
                return open_ev
        payload = {"title": ev["title"], "season": season, "episodes": new, "quality": ev["quality"]}
        key = f"in_plex:tv:{tmdb_id}:s{season}e{new[0][1]}"
    res = s.execute(sqlite_insert(Event).values(
        tmdb_id=tmdb_id, media_type=media_type, kind="in_plex", payload=payload, dedupe_key=key,
        detected_at=now).on_conflict_do_nothing(index_elements=["dedupe_key"]))
    s.flush()
    if not res.rowcount:
        return None  # already told people about this one (a re-download, an *arr retry)
    return s.scalar(select(Event).where(Event.dedupe_key == key))


def _tmdb_for_series(ev: dict) -> int | None:
    if ev["tmdb_id"]:
        return ev["tmdb_id"]
    from . import arr  # Sonarr 4 sends tmdbId; older payloads only tvdbId
    try:
        x = arr._series()[1].get(ev["tvdb_id"]) if ev["tvdb_id"] else None
    except Exception:  # noqa: BLE001
        return None
    return (x or {}).get("tmdbId") or None


def handle(s: Session, app: str, body: dict, overseerr=None) -> dict:
    """One webhook. Returns what happened (for the log and the response); never raises on a
    payload shape it doesn't know."""
    ev = parse(app, body)
    if ev is None:
        return {"ok": True, "ignored": body.get("eventType")}
    if ev["kind"] == "test":
        log.info("arr webhook: test from %s received", app)
    fresh = _notice(s, app, ev)
    out: dict = {"ok": True, "kind": ev["kind"], "duplicate": not fresh}
    if not fresh:
        return out
    media_type = "movie" if app == "radarr" else "tv"
    if ev["kind"] == "download":
        if media_type == "tv":
            ev["tmdb_id"] = _tmdb_for_series(ev)
        if ev["tmdb_id"]:
            if overseerr is not None:
                record_requesters(s, media_type, ev["tmdb_id"], ev["title"], overseerr)
            e = _in_plex(s, media_type, ev)
            out["member_event"] = e.id if e else None
    s.commit()  # the notice and the event exist before any push goes out
    pushed = apns.notify_owner(s, owner_message(app, ev))
    if pushed:
        n = s.scalar(select(ArrNotice).where(ArrNotice.dedupe_key == ev["key"])) if ev["key"] else None
        if n is not None:
            n.pushed = pushed
    s.commit()
    out["pushed"] = pushed
    log.info("arr webhook %s/%s: member_event=%s pushed=%s", app, ev["kind"], out.get("member_event"), pushed)
    return out
