"""Dismiss / Mark watched on What's new, per person (news_dismissals).

- Only the caller's own rows, read and written through their plex_id. No function takes
  anyone else's, and an event they can't see (a show they don't follow, a movie, quiet old
  news) reads as missing, the same as a bad id.
- Dismissing hides that one event from their What's new (shows.whats_new) and nobody else's.
- "Watched" is a dismissal that also tells History where they are with the show
  (history.note_watched). Only a release can be watched: new episodes out, or in Plex.
- Restore (Undo) deletes the row and takes back what "watched" wrote to History, unless
  they've changed it since.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import history
from .clients.tmdb import TMDBClient
from .models import Event, Follow, NewsDismissal, Title, utcnow

WATCHABLE = ("episodes_aired", "in_plex")


class NotFound(LookupError):
    pass


class NotWatchable(ValueError):
    pass


def visible(s: Session, plex_id: int, event_id: int) -> Event | None:
    """The event, if it's one What's new can show them: a show they follow, not quiet news."""
    ev = s.get(Event, event_id)
    if ev is None or ev.media_type != "tv" or (ev.payload or {}).get("quiet"):
        return None
    if s.get(Title, (ev.tmdb_id, "tv")) is None:
        return None
    f = s.get(Follow, (plex_id, ev.tmdb_id))
    return ev if f is not None and f.state == "following" else None


def dismissed_ids(s: Session, plex_id: int) -> set[int]:
    return set(s.scalars(select(NewsDismissal.event_id).where(NewsDismissal.plex_id == plex_id)))


def _row(s: Session, plex_id: int, event_id: int) -> NewsDismissal | None:
    return s.scalar(select(NewsDismissal).where(NewsDismissal.plex_id == plex_id,
                                                NewsDismissal.event_id == event_id))


def is_last_episode(ev: Event, t: Title) -> bool:
    """This release reaches the final episode of a show that has ended."""
    if t.status not in history.ENDED:
        return False
    p, last = ev.payload or {}, t.last_episode or {}
    if ev.kind == "episodes_aired":
        reached = (p.get("season"), p.get("to_episode"))
    else:
        eps = p.get("episodes") or []
        reached = tuple(eps[-1]) if eps else (None, None)
    return bool(last.get("season")) and reached == (last.get("season"), last.get("episode"))


def dismiss(s: Session, plex_id: int, event_id: int) -> dict:
    """Hide it from their What's new. Already hidden (either way) stays as it is."""
    if visible(s, plex_id, event_id) is None and _row(s, plex_id, event_id) is None:
        raise NotFound(event_id)
    row = _row(s, plex_id, event_id)
    if row is None:
        row = NewsDismissal(plex_id=plex_id, event_id=event_id, reason="dismissed", dismissed_at=utcnow())
        s.add(row)
        s.flush()
    return {"ok": True, "event_id": event_id, "reason": row.reason}


def watched(s: Session, tmdb: TMDBClient | None, plex_id: int, event_id: int) -> dict:
    """Hide it and tell History they've watched it. A dismissed event can still be marked
    watched; marking it again changes nothing."""
    row = _row(s, plex_id, event_id)
    ev = visible(s, plex_id, event_id)
    if ev is None and row is None:
        raise NotFound(event_id)
    ev = ev or s.get(Event, event_id)
    if ev.kind not in WATCHABLE:
        raise NotWatchable(ev.kind)
    t = s.get(Title, (ev.tmdb_id, "tv"))
    if row is not None and row.reason == "watched":
        e = s.get(history.WatchEntry, (plex_id, ev.tmdb_id, "tv"))
        status = (e.status if e else None) or history.plex_status_for(s, plex_id, ev.tmdb_id, "tv")
        return {"ok": True, "event_id": event_id, "reason": "watched",
                "history": {"title": t.name if t else "", "tmdb_id": ev.tmdb_id, "media_type": "tv",
                            "status": status, "changed": False}}
    note = history.note_watched(s, tmdb, plex_id, ev.tmdb_id, "tv",
                                last_episode=bool(t and is_last_episode(ev, t)))
    if row is None:
        row = NewsDismissal(plex_id=plex_id, event_id=event_id, dismissed_at=utcnow())
        s.add(row)
    row.reason, row.dismissed_at = "watched", utcnow()
    row.history_status, row.history_created = note["set"], note["created"]
    s.flush()
    return {"ok": True, "event_id": event_id, "reason": "watched",
            "history": {"title": note["title"], "tmdb_id": ev.tmdb_id, "media_type": "tv",
                        "status": note["status"], "changed": note["set"] is not None}}


def restore(s: Session, plex_id: int, event_id: int) -> dict:
    """Undo: back in their What's new, and History as it was if "watched" changed it."""
    row = _row(s, plex_id, event_id)
    if row is None:
        if visible(s, plex_id, event_id) is None:
            raise NotFound(event_id)
        return {"ok": True, "event_id": event_id, "restored": False, "history_reverted": False}
    reverted = False
    if row.reason == "watched":
        ev = s.get(Event, event_id)
        reverted = history.undo_watched(s, plex_id, ev.tmdb_id, "tv", row.history_status,
                                        row.history_created)
    s.delete(row)
    s.flush()
    return {"ok": True, "event_id": event_id, "restored": True, "history_reverted": reverted}
