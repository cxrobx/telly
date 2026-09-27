"""TMDB poll: refresh each show's snapshot, run detection, store events."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .clients.tmdb import TMDBClient
from .config import get_settings
from .detect import FactState, detect
from .models import Event, ShowFact, Title, utcnow

log = logging.getLogger(__name__)


@dataclass
class PollReport:
    polled: int = 0
    baselined: int = 0
    errors: dict[int, str] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)


def _snapshot(title: Title, tv: dict) -> None:
    def ep(e: dict | None) -> dict | None:
        if not e:
            return None
        return {"season": e.get("season_number"), "episode": e.get("episode_number"),
                "air_date": e.get("air_date"), "name": e.get("name")}

    title.name = tv.get("name") or title.name
    first = tv.get("first_air_date") or ""
    title.year = int(first[:4]) if first[:4].isdigit() else title.year
    title.poster_path = tv.get("poster_path")
    title.backdrop_path = tv.get("backdrop_path")
    title.genres = [g["name"] for g in tv.get("genres") or []]
    title.networks = [n["name"] for n in tv.get("networks") or []]
    title.status = tv.get("status")
    title.seasons = [{"season": x.get("season_number"), "air_date": x.get("air_date"),
                      "episode_count": x.get("episode_count"), "name": x.get("name")}
                     for x in tv.get("seasons") or []]
    title.last_episode = ep(tv.get("last_episode_to_air"))
    title.imdb_id = (tv.get("external_ids") or {}).get("imdb_id") or title.imdb_id
    title.next_episode = ep(tv.get("next_episode_to_air"))


def poll_title(s: Session, tmdb: TMDBClient, title: Title, report: PollReport) -> None:
    tv = tmdb.tv(title.tmdb_id)
    title.last_polled_at = utcnow()
    if tv is None:
        title.poll_error = "not_found"
        report.errors[title.tmdb_id] = "not_found"
        return
    title.poll_error = None
    _snapshot(title, tv)

    rows = {f.key: f for f in s.scalars(select(ShowFact).where(ShowFact.tmdb_id == title.tmdb_id))}
    facts = {k: FactState(r.confirmed, r.pending, r.pending_seen) for k, r in rows.items()}
    first_seen = not rows
    events = detect(title.tmdb_id, tv, facts, first_seen=first_seen,
                    confirm=get_settings().confirm_polls)

    for key, st in facts.items():
        row = rows.get(key)
        if row is None:
            row = ShowFact(tmdb_id=title.tmdb_id, key=key)
            s.add(row)
        row.confirmed, row.pending, row.pending_seen = st.confirmed, st.pending, st.pending_seen

    for ev in events:
        res = s.execute(sqlite_insert(Event).values(
            tmdb_id=title.tmdb_id, kind=ev.kind, payload=ev.payload, dedupe_key=ev.dedupe_key,
            detected_at=utcnow(),
        ).on_conflict_do_nothing(index_elements=["dedupe_key"]))
        if res.rowcount:
            report.events.append(f"{title.name}: {ev.kind} {ev.payload}")
    report.polled += 1
    report.baselined += int(first_seen)


def titles_to_poll(s: Session, hot_only: bool, today: date | None = None) -> list[Title]:
    """All shows (daily), or just the ones airing around now (hourly), so an episode alert
    lands within the hour instead of up to a day late."""
    shows = list(s.scalars(select(Title).where(Title.media_type == "tv")))
    if not hot_only:
        return shows
    today = today or date.today()
    window = {(today + timedelta(days=d)).isoformat() for d in (-1, 0, 1)}
    return [t for t in shows
            if t.last_polled_at is None or (t.next_episode or {}).get("air_date") in window]


def poll(s: Session, tmdb: TMDBClient, hot_only: bool = False) -> PollReport:
    report = PollReport()
    for title in titles_to_poll(s, hot_only):
        try:
            poll_title(s, tmdb, title, report)
            s.commit()  # one show at a time: a TMDB failure mid-run keeps what was done
        except Exception as e:  # noqa: BLE001 — one bad show must not stop the run
            s.rollback()
            report.errors[title.tmdb_id] = f"{type(e).__name__}: {e}"
            log.warning("poll failed for %s (%s): %s", title.tmdb_id, title.name, e)
    return report
