"""IMDb and Rotten Tomatoes scores for what people see: followed shows and their picks.

Sources (checked 2026-09-27 on 12 series from Chris's picks and follows):
- OMDb (by IMDb id): IMDb score and votes for 12 of 12 series and movies. Rotten Tomatoes
  for movies only: 0 of 12 series had one.
- MDBList (by TMDB id): Rotten Tomatoes critics + audience for series and movies. Shape read
  from the live API on 2026-09-27 (Fallout 94/96, Succession 95/88): `ratings[]` items with
  `source` + `value`; critics are "tomatoes", and the audience is "popcorn" on api.mdblist.com
  but "tomatoesaudience" on the legacy mdblist.com/api host, so both names are accepted.

Scores move slowly, so each title is refreshed weekly. OMDb's free key allows 1,000 calls a
day and a nightly run needs ~150.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .clients.tmdb import TMDBClient
from .config import get_settings
from .models import Follow, Rating, Recommendation, Title, TrendingPick, User, aware, utcnow

log = logging.getLogger(__name__)

MAX_AGE = timedelta(days=7)
MAX_PER_RUN = 400


class OMDb:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None) -> None:
        self.key = api_key or get_settings().omdb_api_key
        self.http = client or httpx.Client(timeout=15.0)

    def get(self, imdb_id: str) -> dict | None:
        r = self.http.get("https://www.omdbapi.com/", params={"i": imdb_id, "apikey": self.key})
        r.raise_for_status()
        d = r.json()
        return None if d.get("Response") == "False" else parse_omdb(d)


def _num(v: str | None, cast=float):
    if not v or v == "N/A":
        return None
    try:
        return cast(v.replace(",", "").split("/")[0].rstrip("%"))
    except ValueError:
        return None


def parse_omdb(d: dict) -> dict:
    out = {"imdb": _num(d.get("imdbRating")), "imdb_votes": _num(d.get("imdbVotes"), int),
           "metacritic": _num(d.get("Metascore"), int), "rt_critic": None}
    for r in d.get("Ratings") or []:
        if r.get("Source") == "Rotten Tomatoes":
            out["rt_critic"] = _num(r.get("Value"), int)
    return out


AUDIENCE_SOURCES = {"popcorn", "tomatoesaudience"}


def parse_mdblist(d: dict) -> tuple[int | None, int | None] | None:
    """(critics, audience) from an MDBList title, or None if it has neither."""
    critic = audience = None
    for r in d.get("ratings") or []:
        value = r.get("value")
        if value is None:
            continue
        if r.get("source") == "tomatoes":
            critic = int(round(float(value)))
        elif r.get("source") in AUDIENCE_SOURCES:
            audience = int(round(float(value)))
    return (critic, audience) if critic is not None or audience is not None else None


class MDBList:
    BASE = "https://api.mdblist.com"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None) -> None:
        self.key = api_key or get_settings().mdblist_api_key
        self.http = client or httpx.Client(timeout=20.0)

    def rt(self, media_type: str, tmdb_id: int) -> tuple[int | None, int | None] | None:
        kind = "show" if media_type == "tv" else "movie"
        r = self.http.get(f"{self.BASE}/tmdb/{kind}/{tmdb_id}", params={"apikey": self.key})
        if r.status_code == 404:
            return None
        r.raise_for_status()  # 429 (daily quota) raises: the run stops asking, keeps the rest
        return parse_mdblist(r.json())


@dataclass
class RatingsReport:
    refreshed: int = 0
    skipped_fresh: int = 0
    no_imdb_id: int = 0
    errors: list[str] = field(default_factory=list)


def targets(s: Session) -> list[tuple[int, str]]:
    active = select(User.plex_id).where(User.removed_at.is_(None))
    followed = {(t, "tv") for t in s.scalars(select(Follow.tmdb_id).where(
        Follow.state == "following", Follow.plex_id.in_(active)))}
    picked = set(s.execute(select(Recommendation.tmdb_id, Recommendation.media_type)).all())
    picked |= set(s.execute(select(TrendingPick.tmdb_id, TrendingPick.media_type)).all())
    return sorted(followed | {(int(a), b) for a, b in picked})


def refresh(s: Session, tmdb: TMDBClient, omdb: OMDb, rt_source=None, force: bool = False) -> RatingsReport:
    """rt_source(media_type, tmdb_id) -> (critics, audience) | None; MDBList.rt when a key exists."""
    report = RatingsReport()
    now = utcnow()
    for tmdb_id, media_type in targets(s):
        if report.refreshed >= MAX_PER_RUN:
            break
        row = s.get(Rating, (tmdb_id, media_type))
        if row is not None and not force and aware(row.fetched_at) > now - MAX_AGE:
            report.skipped_fresh += 1
            continue
        row = row or Rating(tmdb_id=tmdb_id, media_type=media_type)
        try:
            title = s.get(Title, (tmdb_id, media_type))
            imdb_id = row.imdb_id or (title.imdb_id if title else None) or tmdb.imdb_id(media_type, tmdb_id)
            row.imdb_id = imdb_id
            if imdb_id:
                got = omdb.get(imdb_id) or {}
                row.imdb, row.imdb_votes = got.get("imdb"), got.get("imdb_votes")
                row.metacritic = got.get("metacritic")
                row.rt_critic = got.get("rt_critic")  # movies; OMDb has none for series
            else:
                report.no_imdb_id += 1
            if rt_source is not None:
                rt = rt_source(media_type, tmdb_id)
                if rt:
                    critic, audience = rt
                    row.rt_critic = critic if critic is not None else row.rt_critic
                    row.rt_audience = audience
            row.fetched_at = now  # a miss is also remembered for a week, so it isn't re-asked nightly
            s.merge(row)
            s.commit()
            report.refreshed += 1
        except Exception as e:  # noqa: BLE001 — one title's failure mustn't stop the run
            s.rollback()
            report.errors.append(f"{media_type}:{tmdb_id}: {type(e).__name__}")
            log.warning("ratings failed for %s:%s: %s", media_type, tmdb_id, e)
    return report


def lookup(s: Session, keys: list[tuple[int, str]]) -> dict[tuple[int, str], dict]:
    """Scores for display, keyed by (tmdb_id, media_type). Missing titles are simply absent."""
    if not keys:
        return {}
    ids = {k[0] for k in keys}
    out = {}
    for r in s.scalars(select(Rating).where(Rating.tmdb_id.in_(ids))):
        if (r.tmdb_id, r.media_type) in keys and (r.imdb or r.rt_critic or r.rt_audience):
            out[(r.tmdb_id, r.media_type)] = {"imdb": r.imdb, "imdb_votes": r.imdb_votes,
                                              "rt_critic": r.rt_critic, "rt_audience": r.rt_audience,
                                              "imdb_id": r.imdb_id}
    return out
