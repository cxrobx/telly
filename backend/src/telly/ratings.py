"""IMDb and Rotten Tomatoes scores for what people see: followed shows and their picks.

Sources (checked 2026-09-27 on 12 series from Chris's picks and follows):
- OMDb (by IMDb id): IMDb score and votes for 12 of 12 series and movies. Rotten Tomatoes
  for movies only: 0 of 12 series had one.
- MDBList (by TMDB id): Rotten Tomatoes critics + audience for series and movies. Shape read
  from the live API on 2026-09-27 (Fallout 94/96, Succession 95/88): `ratings[]` items with
  `source` + `value`; critics are "tomatoes", and the audience is "popcorn" on api.mdblist.com
  but "tomatoesaudience" on the legacy mdblist.com/api host, so both names are accepted.

OMDb lags on new titles (Neagley and Unabomber had no score there a week after release while
MDBList had 7.8 and 6.2), so MDBList's IMDb score fills in when OMDb has none.

TMDB sometimes lists one season of an anthology as its own show with no IMDb id ("Monster: The
Lizzie Borden Story" is season 4 of IMDb's "Monster"). Those get the parent series from OMDb by
the name before the colon, and the average of that season's rated episodes, or the series score
until its episodes have one (`season_show`).

Scores move slowly, so each title is refreshed weekly. OMDb's free key allows 1,000 calls a
day and a nightly run needs ~150.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

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

    def _get(self, **params) -> dict | None:
        r = self.http.get("https://www.omdbapi.com/", params={**params, "apikey": self.key})
        r.raise_for_status()
        d = r.json()
        return None if d.get("Response") == "False" else d

    def get(self, imdb_id: str) -> dict | None:
        d = self._get(i=imdb_id)
        return parse_omdb(d) if d else None

    def search_series(self, title: str) -> list[dict]:
        return (self._get(s=title, type="series") or {}).get("Search") or []

    def total_seasons(self, imdb_id: str) -> int | None:
        return _num((self._get(i=imdb_id) or {}).get("totalSeasons"), int)

    def season(self, imdb_id: str, n: int) -> list[dict]:
        return (self._get(i=imdb_id, Season=n) or {}).get("Episodes") or []


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


SEASON_MATCH_DAYS = 45  # an IMDb season's episodes must air this close to TMDB's premiere date


def _spans(years: str, year: int) -> bool:
    """OMDb's "2022–", "2004–2005" or "2022" covers `year`."""
    m = re.match(r"(\d{4})(?:\D(\d{4})?)?", years or "")
    if not m:
        return False
    start, end, open_ = int(m.group(1)), m.group(2), "–" in years or "-" in years
    return start <= year and (int(end) >= year if end else open_ or start == year)


def season_show(omdb: OMDb, name: str, premiered: date | None) -> tuple[str, float | None] | None:
    """(parent series IMDb id, that season's average episode score) for a TMDB show that is
    really one season of an IMDb series, or None when the parent isn't unambiguous. A None
    score means the season has no rated episodes yet: the caller falls back to the series."""
    series = name.split(":", 1)[0].strip()
    if series == name.strip() or not series or premiered is None:
        return None
    hits = [h for h in omdb.search_series(series)
            if h.get("Title", "").casefold() == series.casefold() and _spans(h.get("Year", ""), premiered.year)]
    if len(hits) != 1:
        return None  # two running shows with that name: showing either score could be wrong
    imdb_id = hits[0]["imdbID"]
    last = omdb.total_seasons(imdb_id) or 1
    for n in range(last, max(last - 2, 0), -1):  # the newest listed season, or the one before
        episodes = omdb.season(imdb_id, n)
        aired = [d for e in episodes if (d := _date(e.get("Released")))]
        if aired and min(abs((d - premiered).days) for d in aired) <= SEASON_MATCH_DAYS:
            scores = [x for e in episodes if (x := _num(e.get("imdbRating"))) is not None]
            return imdb_id, round(sum(scores) / len(scores), 1) if scores else None
    return imdb_id, None


def _date(v: str | None) -> date | None:
    try:
        return date.fromisoformat(v or "")
    except ValueError:
        return None


AUDIENCE_SOURCES = {"popcorn", "tomatoesaudience"}


def parse_mdblist(d: dict) -> dict | None:
    """{rt_critic, rt_audience, imdb, imdb_votes} from an MDBList title, or None if it has none."""
    out: dict = {"rt_critic": None, "rt_audience": None, "imdb": None, "imdb_votes": None}
    for r in d.get("ratings") or []:
        value = r.get("value")
        if value is None:
            continue
        if r.get("source") == "tomatoes":
            out["rt_critic"] = int(round(float(value)))
        elif r.get("source") in AUDIENCE_SOURCES:
            out["rt_audience"] = int(round(float(value)))
        elif r.get("source") == "imdb":
            out["imdb"], out["imdb_votes"] = float(value), r.get("votes")
    return out if any(v is not None for v in out.values()) else None


class MDBList:
    BASE = "https://api.mdblist.com"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None) -> None:
        self.key = api_key or get_settings().mdblist_api_key
        self.http = client or httpx.Client(timeout=20.0)

    def scores(self, media_type: str, tmdb_id: int) -> dict | None:
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
    """rt_source(media_type, tmdb_id) -> parse_mdblist dict | None; MDBList.scores when a key exists."""
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
            # TMDB first, every time: a season-show's row holds its parent's id, and TMDB may
            # add the real one later.
            imdb_id = (title.imdb_id if title else None) or tmdb.imdb_id(media_type, tmdb_id)
            season_score = None
            if not imdb_id and media_type == "tv":
                parent = season_show(omdb, *_name_and_premiere(s, tmdb, tmdb_id, title))
                if parent:
                    imdb_id, season_score = parent
            row.imdb_id = imdb_id
            if imdb_id:
                got = omdb.get(imdb_id) or {}
                row.imdb, row.imdb_votes = got.get("imdb"), got.get("imdb_votes")
                row.metacritic = got.get("metacritic")
                row.rt_critic = got.get("rt_critic")  # movies; OMDb has none for series
                if season_score is not None:
                    row.imdb, row.imdb_votes = season_score, None  # the votes were the series'
            else:
                report.no_imdb_id += 1
            if rt_source is not None:
                got = rt_source(media_type, tmdb_id)
                if got:
                    row.rt_critic = got["rt_critic"] if got["rt_critic"] is not None else row.rt_critic
                    row.rt_audience = got["rt_audience"]
                    if row.imdb is None and got["imdb"] is not None:  # OMDb lags on new titles
                        row.imdb, row.imdb_votes = got["imdb"], got["imdb_votes"]
            row.fetched_at = now  # a miss is also remembered for a week, so it isn't re-asked nightly
            s.merge(row)
            s.commit()
            report.refreshed += 1
        except Exception as e:  # noqa: BLE001 — one title's failure mustn't stop the run
            s.rollback()
            report.errors.append(f"{media_type}:{tmdb_id}: {type(e).__name__}")
            log.warning("ratings failed for %s:%s: %s", media_type, tmdb_id, e)
    return report


def _name_and_premiere(s: Session, tmdb: TMDBClient, tmdb_id: int,
                       title: Title | None) -> tuple[str, date | None]:
    d = tmdb.tv(tmdb_id) or {}
    return d.get("name") or (title.name if title else ""), _date(d.get("first_air_date"))


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
