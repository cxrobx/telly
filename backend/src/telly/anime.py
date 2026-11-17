"""A durable public anime catalog, independent of per-person recommendation seeds."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .clients.tmdb import TMDBClient
from .models import AnimeCatalog, LibraryItem, Play, WatchEntry, aware, utcnow
from .ratings import lookup
from .recs import _candidate, is_anime

MIN_VOTES = 200
MIN_SCORE = 7.5
PAGES = 5  # up to 200 shows and movies; ten public queries, once a day


def acclaim(rating: float, votes: int) -> float:
    """Shrink small samples toward 7/10 with a 500-vote prior."""
    return (votes * rating + 500 * 7) / (votes + 500)


def refresh(s: Session, tmdb: TMDBClient, force: bool = False) -> int:
    before = s.get(AnimeCatalog, 1)
    if before and not force and aware(before.fetched_at) > utcnow() - timedelta(days=1):
        return len(before.picks)
    picks = {}
    # Build completely before replacing the snapshot: an outage preserves the last catalog.
    for kind in ("tv", "movie"):
        for page in range(1, PAGES + 1):
            for raw in tmdb.acclaimed_anime(kind, page):
                votes, score = int(raw.get("vote_count") or 0), float(raw.get("vote_average") or 0)
                if not raw.get("id") or raw.get("adult") or not is_anime(raw) or votes < MIN_VOTES or score < MIN_SCORE:
                    continue
                c = _candidate(raw, kind)
                picks[(kind, c.tmdb_id)] = {
                    "tmdb_id": c.tmdb_id, "media_type": kind, "title": c.title, "year": c.year,
                    "poster_path": c.poster_path, "backdrop_path": c.backdrop_path,
                    "overview": c.overview, "reason": c.overview, "because": [], "trending": False,
                    "anime": True, "tmdb_rating": score, "tmdb_votes": votes,
                    "acclaim": acclaim(score, votes)}
    if not picks:
        raise ValueError("Anime discovery returned an empty catalog; kept the previous snapshot")
    ranked = sorted(picks.values(), key=lambda p: (-p["acclaim"], -p["tmdb_votes"], p["title"]))
    s.merge(AnimeCatalog(id=1, picks=ranked, fetched_at=utcnow()))
    s.flush()
    return len(ranked)


def for_user(s: Session, plex_id: int) -> dict:
    snapshot = s.get(AnimeCatalog, 1)
    if snapshot is None:
        return {"picks": [], "updated_at": None}
    watched = set(s.execute(select(Play.tmdb_id, Play.media_type).where(Play.plex_id == plex_id)).all())
    watched |= set(s.execute(select(WatchEntry.tmdb_id, WatchEntry.media_type).where(
        WatchEntry.plex_id == plex_id)).all())
    library = set(s.execute(select(LibraryItem.tmdb_id, LibraryItem.media_type)).all())
    scores = lookup(s, [(p["tmdb_id"], p["media_type"]) for p in snapshot.picks])
    return {"updated_at": aware(snapshot.fetched_at).isoformat(), "picks": [
        {**p, "watched": (p["tmdb_id"], p["media_type"]) in watched,
         "in_library": (p["tmdb_id"], p["media_type"]) in library,
         "ratings": scores.get((p["tmdb_id"], p["media_type"]))} for p in snapshot.picks]}
