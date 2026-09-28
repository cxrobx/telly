"""Watch history, per person: everything they watched on Plex plus what they add by hand, each
with a status and, once they give one, a rating. Ratings are the strongest taste signal Telly
has; recs.py reads them through `watched()`.

The scale is Not for me / Liked it / Loved it (-1 / 1 / 2), unrated by default (chosen
2026-09-28 from the research in docs/spec-history.md). Coarse scales get far more ratings
for the same effort, the middle of a 5- or 10-point scale is mostly noise when people
re-rate, and liked vs. loved is the one split worth keeping: a loved show is the best seed.

Status comes from Plex unless they set one:
- caught_up: they've seen the latest aired episode of a show that's still running
- finished: the same for a show that has ended or been canceled; any movie they played
- watching: played in the last STALL_DAYS, with aired episodes left
- stalled: nothing for STALL_DAYS with aired episodes left. Never taken as dropped: on a
  shared server people often finish a show somewhere else, so the page asks instead.
By hand: watching | finished | dropped. Something added by hand and never played on Plex is
finished unless they say otherwise.

Plex plays are never copied into watch_entries: a row exists only for what the person said.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .clients.tmdb import TMDBClient
from .models import Play, Title, WatchEntry, aware, utcnow
from .ratings import lookup
from .sync import aired_episode_count

RATINGS = {-1: "not_for_me", 1: "liked", 2: "loved"}
STATUSES = ("watching", "finished", "dropped")
ENDED = {"Ended", "Canceled"}
STALL_DAYS = 60
# Dropped this early, it's the premise or the tone they didn't like: that counts against.
# Dropped later (a season or more in) is just losing interest, and counts for nothing. Two
# episodes is always early; up to five only if that's under 15% of what's aired; past five,
# never (26 episodes of a 1,000-episode anime is under 3% of it, and still a real try).
EARLY_DROP_EPISODES = 2
EARLY_DROP_FRACTION = 0.15
EARLY_DROP_MAX = 5


class HistoryError(ValueError):
    pass


@dataclass
class Watched:
    tmdb_id: int
    media_type: str
    name: str
    year: int | None
    poster_path: str | None
    backdrop_path: str | None
    status: str                  # caught_up | finished | watching | stalled | dropped
    plex_status: str | None      # what Plex alone says; None when it was never played there
    status_set: bool             # they chose the status
    rating: int | None
    source: str                  # plex | manual
    episodes: int                # distinct episodes played on Plex (a movie: 1 if played)
    aired: int | None            # episodes aired so far (shows only, once polled)
    last_watched: datetime | None
    added_at: datetime | None

    @property
    def dropped_early(self) -> bool:
        if self.status != "dropped":
            return False
        if self.episodes <= EARLY_DROP_EPISODES:
            return True  # includes a show they added by hand as dropped
        return (self.episodes <= EARLY_DROP_MAX and bool(self.aired)
                and self.episodes / self.aired < EARLY_DROP_FRACTION)


def plex_status(title: Title | None, media_type: str, eps: set[tuple[int, int]],
                last: datetime | None, now: datetime) -> str:
    if media_type == "movie":
        return "finished"
    latest = (title.last_episode or {}) if title else {}
    key = (latest.get("season"), latest.get("episode"))
    aired = aired_episode_count(title) if title and title.last_polled_at else 0
    if (key[0] and key in eps) or (aired and len(eps) >= aired):
        return "finished" if title and title.status in ENDED else "caught_up"
    if last is not None and (now - aware(last)).days < STALL_DAYS:
        return "watching"
    return "stalled"


def watched(s: Session, plex_id: int, now: datetime | None = None) -> list[Watched]:
    """Everything in the caller's history, most recent first."""
    now = now or utcnow()
    plays: dict[tuple[int, str], dict] = {}
    for tmdb_id, media_type, season, episode, at in s.execute(
            select(Play.tmdb_id, Play.media_type, Play.season, Play.episode, Play.viewed_at)
            .where(Play.plex_id == plex_id, Play.tmdb_id.is_not(None))):
        p = plays.setdefault((tmdb_id, media_type), {"eps": set(), "last": None})
        if media_type == "tv" and season and season > 0:
            p["eps"].add((season, episode or 0))
        at = aware(at)
        p["last"] = at if p["last"] is None or at > p["last"] else p["last"]
    mine = {(e.tmdb_id, e.media_type): e for e in s.scalars(
        select(WatchEntry).where(WatchEntry.plex_id == plex_id))}
    out = []
    for key in plays.keys() | mine.keys():
        tmdb_id, media_type = key
        t = s.get(Title, key)
        p, e = plays.get(key), mine.get(key)
        derived = plex_status(t, media_type, p["eps"], p["last"], now) if p else None
        status = (e.status if e and e.status else None) or derived or "finished"
        episodes = (len(p["eps"]) if media_type == "tv" else 1) if p else 0
        out.append(Watched(
            tmdb_id=tmdb_id, media_type=media_type,
            name=(t.name if t and t.name else None) or (e.title if e else ""),
            year=(t.year if t else None) or (e.year if e else None),
            poster_path=(t.poster_path if t else None) or (e.poster_path if e else None),
            backdrop_path=(t.backdrop_path if t else None) or (e.backdrop_path if e else None),
            status=status, plex_status=derived, status_set=bool(e and e.status),
            rating=e.rating if e else None, source="plex" if p else "manual",
            episodes=episodes,
            aired=aired_episode_count(t) if media_type == "tv" and t and t.last_polled_at else None,
            last_watched=p["last"] if p else None,
            added_at=aware(e.added_at) if e and e.added_at else None))
    out.sort(key=lambda w: w.last_watched or w.added_at or now, reverse=True)
    return out


def listing(s: Session, plex_id: int) -> list[dict]:
    rows = watched(s, plex_id)
    scores = lookup(s, [(w.tmdb_id, w.media_type) for w in rows])
    return [{
        "tmdb_id": w.tmdb_id, "media_type": w.media_type, "name": w.name, "year": w.year,
        "poster_path": w.poster_path, "backdrop_path": w.backdrop_path, "status": w.status,
        "plex_status": w.plex_status, "status_set": w.status_set,
        "rating": RATINGS.get(w.rating) if w.rating is not None else None,
        "source": w.source, "episodes": w.episodes, "aired": w.aired,
        "last_watched": w.last_watched.date().isoformat() if w.last_watched else None,
        "added_at": w.added_at.date().isoformat() if w.added_at else None,
        "ratings": scores.get((w.tmdb_id, w.media_type)),
    } for w in rows]


def _played(s: Session, plex_id: int, tmdb_id: int, media_type: str) -> bool:
    return s.scalar(select(Play.history_key).where(
        Play.plex_id == plex_id, Play.tmdb_id == tmdb_id, Play.media_type == media_type).limit(1)) is not None


def _entry(s: Session, tmdb: TMDBClient | None, plex_id: int, tmdb_id: int, media_type: str) -> WatchEntry:
    """Their row for this title, made (with its name and art) if it doesn't exist yet."""
    if media_type not in ("tv", "movie"):
        raise HistoryError("media_type must be tv or movie")
    e = s.get(WatchEntry, (plex_id, tmdb_id, media_type))
    if e is not None:
        return e
    t = s.get(Title, (tmdb_id, media_type))
    name, year, poster, backdrop = (t.name, t.year, t.poster_path, t.backdrop_path) if t else ("", None, None, None)
    if not (name and poster):
        d = (tmdb or TMDBClient())
        d = (d.tv(tmdb_id) if media_type == "tv" else d.movie(tmdb_id)) or {}
        if not d and not name:
            raise HistoryError(f"TMDB has no {media_type} with id {tmdb_id}")
        name = name or d.get("name") or d.get("title") or ""
        date_ = d.get("first_air_date") or d.get("release_date") or ""
        year = year or (int(date_[:4]) if date_[:4].isdigit() else None)
        poster, backdrop = poster or d.get("poster_path"), backdrop or d.get("backdrop_path")
    e = WatchEntry(plex_id=plex_id, tmdb_id=tmdb_id, media_type=media_type, title=name, year=year,
                   poster_path=poster, backdrop_path=backdrop,
                   source="plex" if _played(s, plex_id, tmdb_id, media_type) else "manual")
    s.add(e)
    return e


def _tidy(s: Session, e: WatchEntry) -> None:
    """A row for a Plex title that no longer says anything is just noise: drop it."""
    if e.source == "plex" and e.rating is None and e.status is None:
        s.delete(e)


def rate(s: Session, tmdb: TMDBClient | None, plex_id: int, tmdb_id: int, media_type: str,
         rating: int | None) -> dict:
    """rating: -1 not for me, 1 liked it, 2 loved it, None to clear."""
    if rating is not None and rating not in RATINGS:
        raise HistoryError("rating must be -1, 1 or 2")
    e = _entry(s, tmdb, plex_id, tmdb_id, media_type)
    e.rating, e.updated_at = rating, utcnow()
    _tidy(s, e)
    s.flush()
    return {"ok": True, "title": e.title, "rating": RATINGS.get(rating) if rating is not None else None}


def set_status(s: Session, tmdb: TMDBClient | None, plex_id: int, tmdb_id: int, media_type: str,
               status: str | None) -> dict:
    """status: watching | finished | dropped, or None to go back to what Plex says."""
    if status is not None and status not in STATUSES:
        raise HistoryError(f"status must be one of {', '.join(STATUSES)}")
    e = _entry(s, tmdb, plex_id, tmdb_id, media_type)
    e.status, e.updated_at = status, utcnow()
    _tidy(s, e)
    s.flush()
    return {"ok": True, "title": e.title, "status": status}


def add(s: Session, tmdb: TMDBClient | None, plex_id: int, tmdb_id: int, media_type: str,
        status: str | None = None, rating: int | None = None) -> dict:
    """Something they watched anywhere. Adding it again only changes what they pass."""
    e = _entry(s, tmdb, plex_id, tmdb_id, media_type)
    if status is not None:
        set_status(s, tmdb, plex_id, tmdb_id, media_type, status)
    if rating is not None:
        rate(s, tmdb, plex_id, tmdb_id, media_type, rating)
    if (row := s.get(WatchEntry, (plex_id, tmdb_id, media_type))) is not None:
        _tidy(s, row)
    s.flush()
    return {"ok": True, "title": e.title, "tmdb_id": tmdb_id, "media_type": media_type}


def remove(s: Session, plex_id: int, tmdb_id: int, media_type: str) -> dict:
    """Forget what they said about it. Plex plays stay, so a Plex title stays in history."""
    e = s.get(WatchEntry, (plex_id, tmdb_id, media_type))
    if e is not None:
        s.delete(e)
        s.flush()
    return {"ok": True, "still_in_history": _played(s, plex_id, tmdb_id, media_type)}


def record(s: Session, tmdb: TMDBClient | None, plex_id: int, tmdb_id: int, media_type: str,
           liked: bool, loved: bool = False, status: str | None = None) -> dict:
    """What someone said about a title in chat or on Your taste. A plain "liked it" never
    downgrades a title they already marked as loved."""
    e = _entry(s, tmdb, plex_id, tmdb_id, media_type)
    rating = 2 if loved else 1 if liked else -1
    if rating == 1 and e.rating == 2:
        rating = 2
    add(s, tmdb, plex_id, tmdb_id, media_type, status=status, rating=rating)
    return {"ok": True, "title": e.title, "tmdb_id": tmdb_id, "media_type": media_type,
            "liked": liked or loved, "rating": RATINGS[rating]}
