"""Nightly mirrors of the other Plex-side data: the library, each person's watchlist, and
which Overseerr account each member has (for Request)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .clients.overseerr import OverseerrClient
from .clients.plex import PlexClient
from .clients.plextv import PlexTV
from .crypto import decrypt_token
from .models import Follow, LibraryItem, Title, User, WatchlistItem, utcnow
from .sync import SyncReport, _resolve_item

log = logging.getLogger(__name__)


@dataclass
class PlexDataReport:
    library: int = 0
    watchlists: int = 0
    watchlist_follows_added: int = 0
    watchlist_follows_removed: int = 0
    dead_tokens: list[int] = field(default_factory=list)
    overseerr_mapped: int = 0


def sync_library(s: Session, plex: PlexClient, report: PlexDataReport) -> None:
    """Every movie/show on the server by TMDB id. Guids come from the PlexItem cache, so only
    items new to the library cost a metadata call."""
    seen: set[tuple[int, str]] = set()
    now = utcnow()
    for sec in plex.sections():
        if sec["type"] not in ("movie", "show"):
            continue
        media_type = "tv" if sec["type"] == "show" else "movie"
        for item in plex.section_items(sec["key"]):
            tmdb_id = _resolve_item(s, plex, item["rating_key"], media_type, item["title"],
                                    SyncReport(), create_title=False)
            if tmdb_id is None or (tmdb_id, media_type) in seen:
                continue
            seen.add((tmdb_id, media_type))
            row = s.get(LibraryItem, (tmdb_id, media_type))
            if row is None:
                s.add(LibraryItem(tmdb_id=tmdb_id, media_type=media_type,
                                  rating_key=item["rating_key"], seen_at=now))
            else:
                row.rating_key, row.seen_at = item["rating_key"], now
        s.flush()
    s.execute(delete(LibraryItem).where(LibraryItem.seen_at < now))  # gone from the server
    report.library = len(seen)


def sync_watchlists(s: Session, plextv: PlexTV, report: PlexDataReport) -> None:
    """Mirror each signed-in person's watchlist; shows on it are followed (source=watchlist).
    A watchlist follow disappears when the show leaves the watchlist; an unfollow in Telly
    stays sticky either way."""
    users = s.scalars(select(User).where(User.plex_token_enc.is_not(None), User.removed_at.is_(None)))
    for u in list(users):
        token = decrypt_token(u.plex_token_enc or "")
        if token is None:
            u.plex_token_enc = None
            report.dead_tokens.append(u.plex_id)
            continue
        try:
            items = plextv.watchlist(token)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):  # revoked: keep the last copy, stop trying
                u.plex_token_enc = None
                report.dead_tokens.append(u.plex_id)
            else:
                log.warning("watchlist fetch failed for %s: %s", u.plex_id, e)
            continue
        s.execute(delete(WatchlistItem).where(WatchlistItem.plex_id == u.plex_id))
        shows = set()
        for it in {(i["tmdb_id"], i["media_type"]): i for i in items}.values():
            added = (datetime.fromtimestamp(it["added_at"], tz=timezone.utc)
                     if it.get("added_at") else None)
            s.add(WatchlistItem(plex_id=u.plex_id, tmdb_id=it["tmdb_id"],
                                media_type=it["media_type"], title=it["title"], added_at=added))
            if it["media_type"] == "tv":
                shows.add(it["tmdb_id"])
                if s.get(Title, (it["tmdb_id"], "tv")) is None:
                    s.add(Title(tmdb_id=it["tmdb_id"], media_type="tv", name=it["title"]))
        s.flush()
        for tmdb_id in shows:
            if s.get(Follow, (u.plex_id, tmdb_id)) is None:
                s.add(Follow(plex_id=u.plex_id, tmdb_id=tmdb_id, source="watchlist"))
                report.watchlist_follows_added += 1
        for f in s.scalars(select(Follow).where(Follow.plex_id == u.plex_id,
                                                Follow.source == "watchlist",
                                                Follow.state == "following")):
            if f.tmdb_id not in shows:
                s.delete(f)
                report.watchlist_follows_removed += 1
        report.watchlists += 1
        s.flush()


def sync_overseerr_ids(s: Session, overseerr: OverseerrClient, report: PlexDataReport) -> None:
    by_plex = {int(u["plex_id"]): u["id"] for u in overseerr.users() if u.get("plex_id")}
    for u in s.scalars(select(User)):
        u.overseerr_id = by_plex.get(u.plex_id)
        report.overseerr_mapped += int(u.overseerr_id is not None)
    s.flush()
