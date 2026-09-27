"""Per-person show logic, shared by the MCP tools and (later) the web API.

Every function takes the caller's plex_id and reads only that person's rows.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .clients.tmdb import TMDBClient
from .models import Event, Follow, LibraryItem, Play, Title, aware, utcnow
from .poll import PollReport, poll_title
from .ratings import lookup


def _fmt(d: str | None) -> str:
    if not d:
        return "date TBA"
    try:
        return date.fromisoformat(d).strftime("%b %-d, %Y")
    except ValueError:
        return d


def describe(kind: str, payload: dict, name: str) -> str:
    """One line a person reads in a DM or on a lock screen."""
    p = payload
    if kind == "renewed":
        src = ""
        if p.get("via") == "news" and p.get("source_name"):
            when = _fmt(p.get("announced")) if p.get("announced") else ""
            src = f" ({p['source_name']}, {when})" if p.get("quiet") and when else f" ({p['source_name']})"
        return f"{name} was renewed for season {p['season']}{src}"
    if kind == "canceled":
        return f"{name} was canceled after season {p['after_season']}"
    if kind == "ended":
        return f"{name} has ended; season {p['after_season']} was the last"
    if kind == "season_dated":
        moved = f" (moved from {_fmt(p['previous'])})" if p.get("previous") else ""
        return f"{name} season {p['season']} premieres {_fmt(p['air_date'])}{moved}"
    if kind == "episodes_aired":
        s, a, b = p["season"], p["from_episode"], p["to_episode"]
        if p.get("premiere") and b > a:
            return f"{name} season {s} is out: all {b} episodes"
        if p.get("premiere"):
            return f"{name} season {s} premiered: S{s}E{b} is out"
        if b > a:
            return f"{name} S{s}E{a}–E{b} are out"
        return f"{name} S{s}E{b} is out"
    return f"{name}: {kind}"


def _art(t: Title) -> dict:
    return {"poster_path": t.poster_path, "backdrop_path": t.backdrop_path, "networks": t.networks}


def _following(s: Session, plex_id: int) -> dict[int, Follow]:
    return {f.tmdb_id: f for f in s.scalars(select(Follow).where(
        Follow.plex_id == plex_id, Follow.state == "following"))}


def next_season(s: Session, t: Title) -> dict | None:
    """The next season we know of: a TMDB season row not yet aired, else a news renewal."""
    last = (t.last_episode or {}).get("season") or 0
    for x in sorted(t.seasons or [], key=lambda x: x.get("season") or 0):
        if (x.get("season") or 0) > last:
            return {"season": x["season"], "air_date": x.get("air_date"), "source": "TMDB"}
    for e in s.scalars(select(Event).where(Event.tmdb_id == t.tmdb_id, Event.kind == "renewed")):
        if e.payload.get("via") == "news" and (e.payload.get("season") or 0) > last:
            return {"season": e.payload["season"], "air_date": None, "source": e.payload.get("source_name")}
    return None


def followed_shows(s: Session, plex_id: int) -> list[dict]:
    follows = _following(s, plex_id)
    scores = lookup(s, [(t, "tv") for t in follows])
    out = []
    for t in s.scalars(select(Title).where(Title.media_type == "tv",
                                           Title.tmdb_id.in_(follows))).all():
        out.append({"tmdb_id": t.tmdb_id, "name": t.name, "year": t.year, "status": t.status,
                    "followed_because": follows[t.tmdb_id].source,
                    "next_episode": t.next_episode, "last_episode": t.last_episode,
                    "next_season": next_season(s, t), "ratings": scores.get((t.tmdb_id, "tv")),
                    **_art(t)})
    return sorted(out, key=lambda x: x["name"].lower())


def upcoming(s: Session, plex_id: int, days: int = 120, today: date | None = None) -> dict:
    """The caller's timeline: dated episodes/premieres in the window, plus renewals with no date."""
    today = today or date.today()
    end = today + timedelta(days=days)
    dated, undated = [], []
    for t in s.scalars(select(Title).where(Title.media_type == "tv",
                                           Title.tmdb_id.in_(_following(s, plex_id)))):
        last_season = (t.last_episode or {}).get("season") or 0
        nxt = t.next_episode or {}
        seen_premiere: set[int] = set()
        if nxt.get("air_date") and today <= date.fromisoformat(nxt["air_date"]) <= end:
            premiere = nxt.get("episode") == 1
            dated.append({"date": nxt["air_date"], "show": t.name, "tmdb_id": t.tmdb_id,
                          "season": nxt.get("season"), "episode": nxt.get("episode"),
                          "kind": "season_premiere" if premiere else "episode",
                          "title": nxt.get("name"), **_art(t)})
            if premiere:
                seen_premiere.add(nxt.get("season"))
        for season in t.seasons or []:
            n = season.get("season") or 0
            if n <= last_season or n == 0 or n in seen_premiere:
                continue
            d = season.get("air_date")
            if d and today <= date.fromisoformat(d) <= end:
                dated.append({"date": d, "show": t.name, "tmdb_id": t.tmdb_id, "season": n,
                              "episode": 1, "kind": "season_premiere", "title": season.get("name"),
                              **_art(t)})
            elif not d:
                undated.append({"show": t.name, "tmdb_id": t.tmdb_id, "season": n,
                                "note": "renewed, no premiere date yet", "source": "TMDB", **_art(t)})
        listed = {x.get("season") for x in t.seasons or []}
        for ev in s.scalars(select(Event).where(Event.tmdb_id == t.tmdb_id, Event.kind == "renewed")):
            n = (ev.payload or {}).get("season") or 0
            if ev.payload.get("via") == "news" and n > last_season and n not in listed:
                undated.append({"show": t.name, "tmdb_id": t.tmdb_id, "season": n,
                                "note": "renewed, no premiere date yet",
                                "source": ev.payload.get("source_name"),
                                "source_url": ev.payload.get("source_url"), **_art(t)})
    dated.sort(key=lambda x: (x["date"], x["show"]))
    return {"from": today.isoformat(), "to": end.isoformat(), "dated": dated,
            "renewed_no_date": sorted(undated, key=lambda x: x["show"])}


def whats_new(s: Session, plex_id: int, days: int = 14) -> list[dict]:
    since = utcnow() - timedelta(days=days)
    follows = _following(s, plex_id)
    rows = s.execute(
        select(Event, Title)
        .join(Title, (Title.tmdb_id == Event.tmdb_id) & (Title.media_type == "tv"))
        .where(Event.tmdb_id.in_(follows), Event.detected_at >= since)
        .order_by(Event.detected_at.desc()))
    return [{"when": aware(ev.detected_at).date().isoformat(), "show": t.name, "kind": ev.kind,
             "tmdb_id": t.tmdb_id, "payload": ev.payload,
             "summary": describe(ev.kind, ev.payload, t.name), **_art(t)}
            for ev, t in rows if not (ev.payload or {}).get("quiet")]  # old news isn't news


def recently_watched(s: Session, plex_id: int, limit: int = 10) -> list[dict]:
    rows = s.execute(
        select(Title.name, Play.media_type, func.count(), func.max(Play.viewed_at))
        .join(Title, (Title.tmdb_id == Play.tmdb_id) & (Title.media_type == Play.media_type))
        .where(Play.plex_id == plex_id)
        .group_by(Title.tmdb_id, Title.media_type)
        .order_by(func.max(Play.viewed_at).desc()).limit(limit))
    return [{"title": n, "type": mt, "plays": c,
             "last_watched": aware(last).date().isoformat() if isinstance(last, datetime) else str(last)[:10]}
            for n, mt, c, last in rows]


def search(tmdb: TMDBClient, query: str, limit: int = 6) -> list[dict]:
    return [{"tmdb_id": r["id"], "name": r.get("name"), "first_aired": r.get("first_air_date"),
             "overview": (r.get("overview") or "")[:160]}
            for r in tmdb.search_tv(query)[:limit]]


def ensure_title(s: Session, tmdb: TMDBClient, tmdb_id: int) -> Title | None:
    """Make sure a show exists and has been polled once (its baseline), so it can render and
    shows on timelines at once. None if TMDB has no such show."""
    title = s.get(Title, (tmdb_id, "tv"))
    if title is None:
        title = Title(tmdb_id=tmdb_id, media_type="tv")
        s.add(title)
        s.flush()
    if title.last_polled_at is None:
        poll_title(s, tmdb, title, PollReport())
        if title.poll_error:
            s.delete(title)
            s.flush()
            return None
    return title


def follow(s: Session, tmdb: TMDBClient, plex_id: int, tmdb_id: int) -> dict:
    title = ensure_title(s, tmdb, tmdb_id)
    if title is None:
        return {"ok": False, "error": f"TMDB has no show with id {tmdb_id}"}
    f = s.get(Follow, (plex_id, tmdb_id))
    if f is None:
        s.add(Follow(plex_id=plex_id, tmdb_id=tmdb_id, source="manual", state="following"))
    else:
        f.state, f.source = "following", "manual"
    s.flush()
    return {"ok": True, "following": title.name, "status": title.status}


def unfollow(s: Session, plex_id: int, tmdb_id: int) -> dict:
    f = s.get(Follow, (plex_id, tmdb_id))
    if f is None:
        s.add(Follow(plex_id=plex_id, tmdb_id=tmdb_id, source="manual", state="unfollowed"))
    else:
        f.state = "unfollowed"
    s.flush()
    t = s.get(Title, (tmdb_id, "tv"))
    return {"ok": True, "unfollowed": t.name if t else tmdb_id}


def show_detail(s: Session, plex_id: int, tmdb_id: int) -> dict | None:
    t = s.get(Title, (tmdb_id, "tv"))
    if t is None:
        return None
    f = s.get(Follow, (plex_id, tmdb_id))
    plays = s.execute(select(func.count(), func.max(Play.viewed_at)).where(
        Play.plex_id == plex_id, Play.tmdb_id == tmdb_id, Play.media_type == "tv")).one()
    events = s.scalars(select(Event).where(Event.tmdb_id == tmdb_id)
                       .order_by(Event.detected_at.desc()).limit(20))
    in_lib = s.get(LibraryItem, (tmdb_id, "tv")) is not None
    last_season = (t.last_episode or {}).get("season") or 0
    listed = {x.get("season") for x in t.seasons or []}
    renewed_next = next((
        {"season": e.payload["season"], "source": e.payload.get("source_name"),
         "source_url": e.payload.get("source_url")}
        for e in s.scalars(select(Event).where(Event.tmdb_id == tmdb_id, Event.kind == "renewed"))
        if e.payload.get("via") == "news" and e.payload.get("season", 0) > last_season
        and e.payload.get("season") not in listed), None)  # a renewal TMDB hasn't added yet
    return {
        "tmdb_id": t.tmdb_id, "name": t.name, "year": t.year, "status": t.status,
        "genres": t.genres, "seasons": [x for x in t.seasons or [] if (x.get("season") or 0) > 0],
        "last_episode": t.last_episode, "next_episode": t.next_episode, **_art(t),
        "following": bool(f and f.state == "following"),
        "followed_because": f.source if f and f.state == "following" else None,
        "my_plays": plays[0],
        "last_watched": aware(plays[1]).date().isoformat() if isinstance(plays[1], datetime) else None,
        "in_library": in_lib,
        "renewed_next": renewed_next,
        "ratings": lookup(s, [(tmdb_id, "tv")]).get((tmdb_id, "tv")),
        "events": [{"when": aware(e.detected_at).date().isoformat(), "kind": e.kind,
                    "summary": describe(e.kind, e.payload, t.name),
                    "source_url": (e.payload or {}).get("source_url")} for e in events],
    }

