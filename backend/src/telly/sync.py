"""Plex → Telly: members, play history, and inferred follows."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .clients.plex import PlexClient, tmdb_id_from_guids
from .models import Follow, Play, PlexItem, Title, User, utcnow

log = logging.getLogger(__name__)

# Inference (spec §6): a show counts as followed once someone has watched at least this many
# distinct episodes AND either half of what has aired or anything from the latest aired season.
# Ended/canceled shows are included on purpose: a revival is exactly the alert people want.
MIN_EPISODES = 3
MIN_FRACTION = 0.5


@dataclass
class SyncReport:
    users: int = 0
    removed: list[int] = field(default_factory=list)
    new_plays: int = 0
    skipped_unknown_account: int = 0
    unmatched_items: int = 0
    new_follows: int = 0


def sync_users(s: Session, plex: PlexClient, report: SyncReport) -> None:
    """Upsert members from PMS /accounts. Owner: PMS id 1 → their plex.tv id (spec §2)."""
    owner_id = plex.owner_plex_id()
    present: set[int] = set()
    for a in plex.accounts():
        pms_id = int(a["id"])
        plex_id = owner_id if pms_id == 1 else pms_id
        present.add(plex_id)
        u = s.get(User, plex_id)
        if u is None:
            u = User(plex_id=plex_id, pms_account_id=pms_id)
            s.add(u)
        u.username = a["name"]
        u.is_owner = pms_id == 1
        u.removed_at = None
    for u in s.scalars(select(User).where(User.removed_at.is_(None))):
        if u.plex_id not in present:  # removed from the server (spec §8)
            u.removed_at = utcnow()
            report.removed.append(u.plex_id)
    report.users = len(present)
    s.flush()


def _resolve_item(s: Session, plex: PlexClient, rating_key: str, media_type: str,
                  title: str, report: SyncReport, create_title: bool = True) -> int | None:
    cached = s.get(PlexItem, rating_key)
    if cached is not None:
        return cached.tmdb_id
    item = plex.item(rating_key)
    tmdb_id = tmdb_id_from_guids(item) if item else None
    status = "matched" if tmdb_id else ("missing" if item is None else "no_tmdb_guid")
    if tmdb_id is None:
        report.unmatched_items += 1
    s.add(PlexItem(rating_key=rating_key, media_type=media_type, title=title,
                   tmdb_id=tmdb_id, match_status=status))
    if create_title and tmdb_id is not None and s.get(Title, (tmdb_id, media_type)) is None:
        s.add(Title(tmdb_id=tmdb_id, media_type=media_type, name=title,
                    year=(item or {}).get("year")))
    s.flush()
    return tmdb_id


def sync_history(s: Session, plex: PlexClient, report: SyncReport, page_size: int = 200) -> None:
    """Pull plays newest-first; stop at the first page that is entirely already known."""
    accounts = {u.pms_account_id: u.plex_id for u in s.scalars(select(User))}
    start = 0
    while True:
        total, items = plex.history_page(start, page_size)
        if not items:
            break
        known = set(s.scalars(select(Play.history_key).where(
            Play.history_key.in_([str(i.get("historyKey")) for i in items]))))
        fresh = 0
        for i in items:
            hk = str(i.get("historyKey"))
            if hk in known:
                continue
            plex_id = accounts.get(int(i.get("accountID") or 0))
            if plex_id is None:
                report.skipped_unknown_account += 1
                continue
            kind = i.get("type")
            if kind == "episode":
                rk = (i.get("grandparentKey") or "").rsplit("/", 1)[-1]
                media_type, name = "tv", i.get("grandparentTitle") or ""
            elif kind == "movie":
                rk, media_type, name = str(i.get("ratingKey")), "movie", i.get("title") or ""
            else:
                continue
            if not rk:
                continue
            tmdb_id = _resolve_item(s, plex, rk, media_type, name, report)
            s.add(Play(
                history_key=hk, plex_id=plex_id, media_type=media_type, tmdb_id=tmdb_id,
                rating_key=rk,
                season=i.get("parentIndex") if kind == "episode" else None,
                episode=i.get("index") if kind == "episode" else None,
                viewed_at=datetime.fromtimestamp(int(i["viewedAt"]), tz=timezone.utc),
            ))
            fresh += 1
        report.new_plays += fresh
        s.flush()
        start += len(items)
        if (fresh == 0 and known) or start >= total:  # reached already-synced territory
            break


def aired_episode_count(title: Title) -> int:
    """Episodes aired so far, from the TMDB snapshot (specials excluded)."""
    last = title.last_episode or {}
    ls, le = last.get("season") or 0, last.get("episode") or 0
    return sum(x.get("episode_count") or 0 for x in title.seasons
               if 0 < (x.get("season") or 0) < ls) + le


def qualifies(watched: set[tuple[int, int]], title: Title) -> bool:
    if len(watched) < MIN_EPISODES:
        return False
    aired = aired_episode_count(title)
    latest = (title.last_episode or {}).get("season")
    if latest and any(season == latest for season, _ in watched):
        return True
    return aired > 0 and len(watched) / aired >= MIN_FRACTION


def infer_follows(s: Session, report: SyncReport) -> None:
    """Add inferred follows. Never touches an existing row, so an unfollow stays unfollowed."""
    existing = {(f.plex_id, f.tmdb_id) for f in s.scalars(select(Follow))}
    watched: dict[tuple[int, int], set[tuple[int, int]]] = {}
    rows = s.execute(select(Play.plex_id, Play.tmdb_id, Play.season, Play.episode).where(
        Play.media_type == "tv", Play.tmdb_id.is_not(None), Play.season.is_not(None)))
    for plex_id, tmdb_id, season, episode in rows:
        if season and season > 0:
            watched.setdefault((plex_id, tmdb_id), set()).add((season, episode))
    active = {u.plex_id for u in s.scalars(select(User).where(User.removed_at.is_(None)))}
    for (plex_id, tmdb_id), eps in watched.items():
        if (plex_id, tmdb_id) in existing or plex_id not in active:
            continue
        title = s.get(Title, (tmdb_id, "tv"))
        if title is None or title.last_polled_at is None:
            continue  # not polled yet; next run decides
        if qualifies(eps, title):
            s.add(Follow(plex_id=plex_id, tmdb_id=tmdb_id, source="inferred"))
            report.new_follows += 1
    s.flush()
