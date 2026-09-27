"""Taste beyond Plex plays: what someone told Telly they liked, IMDb, and (weakly) Overseerr
requests and chat mentions.

Every signal belongs to one person (plex_id) and feeds recs.py two ways: a liked title is a
taste seed, and anything already seen, rated or requested is kept out of their picks.

IMDb has no API or sign-in for personal data. Its only official way out is the CSV export
(Your Ratings → Export; Watchlist → Export), so that is what Settings accepts. Columns are
found by header name, not position, because IMDb has reshuffled its export before.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .clients.overseerr import OverseerrClient
from .clients.tmdb import TMDBClient
from .models import ImdbRow, Play, Rating, TasteSignal, Title, User, utcnow

log = logging.getLogger(__name__)

TV_TYPES = {"tv series", "tv mini series", "tvseries", "tvminiseries", "tv mini-series"}
MOVIE_TYPES = {"movie", "tv movie", "tvmovie", "film"}
LIKE_FROM = 7      # an IMDb rating this high is a taste seed
DISLIKE_TO = 4     # this low counts against, like a thumbs down
# Interest, not liking: a request can be a try-out or for a friend; a chat mention is often a
# fix-it question. They count at a third of a few watched episodes, and can be switched off.
WEAK_SOURCES = {"overseerr", "mentioned"}
WEAK_FACTOR = 0.35
SOURCES = ("told", "imdb_rating", "overseerr", "mentioned", "imdb_watchlist")


class ImdbImportError(ValueError):
    pass


def _col(header: list[str], *names: str) -> int | None:
    low = [h.strip().lower() for h in header]
    for n in names:
        if n in low:
            return low.index(n)
    return None


def _date(v: str | None) -> datetime | None:
    """IMDb exports dates as YYYY-MM-DD; anything else is kept only if it's ISO."""
    v = (v or "").strip()
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_imdb_csv(text: str) -> tuple[str, list[dict]]:
    """→ (kind, rows). kind is "watchlist" when the export has a Position column (IMDb's
    watchlist and list exports do), else "rating". Rows: {imdb_id, title, title_type,
    rating, noted_at}."""
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ImdbImportError("The file is empty.") from None
    c_id = _col(header, "const", "imdb id", "tconst")
    if c_id is None:
        raise ImdbImportError("That doesn't look like an IMDb export: there's no Const column.")
    kind = "watchlist" if _col(header, "position") is not None else "rating"
    c_title = _col(header, "title")
    c_type = _col(header, "title type", "type")
    c_rating = _col(header, "your rating")
    c_date = _col(header, "date rated", "date added", "created")
    rows = []
    for r in reader:
        if not r or len(r) <= c_id or not r[c_id].strip().startswith("tt"):
            continue
        get = lambda i: r[i].strip() if i is not None and i < len(r) else ""  # noqa: E731
        rating = get(c_rating)
        rows.append({
            "imdb_id": get(c_id), "title": get(c_title), "title_type": get(c_type),
            "rating": int(float(rating)) if rating.replace(".", "", 1).isdigit() else None,
            "noted_at": _date(get(c_date)),
        })
    if kind == "rating" and rows and not any(x["rating"] for x in rows):
        raise ImdbImportError("This export has no ratings in it. For a watchlist, use the watchlist export.")
    return kind, rows


def store_imdb(s: Session, plex_id: int, kind: str, rows: list[dict]) -> dict:
    """Keep the rows (pending a TMDB match). Re-uploading the same export changes nothing."""
    added = updated = 0
    for x in rows:
        row = s.get(ImdbRow, (plex_id, x["imdb_id"], kind))
        if row is None:
            s.add(ImdbRow(plex_id=plex_id, imdb_id=x["imdb_id"], kind=kind, title=x["title"],
                          title_type=x["title_type"], rating=x["rating"], noted_at=x["noted_at"]))
            added += 1
        elif (row.rating, row.title_type) != (x["rating"], x["title_type"]):
            row.rating, row.title_type, row.title = x["rating"], x["title_type"], x["title"]
            row.match = "pending" if row.match == "pending" else row.match
            updated += 1
    s.flush()
    return {"kind": kind, "rows": len(rows), "added": added, "updated": updated}


def _known_tmdb(s: Session, imdb_id: str) -> tuple[int, str] | None:
    t = s.execute(select(Title.tmdb_id, Title.media_type).where(Title.imdb_id == imdb_id)).first()
    if t:
        return int(t[0]), t[1]
    r = s.execute(select(Rating.tmdb_id, Rating.media_type).where(Rating.imdb_id == imdb_id)).first()
    return (int(r[0]), r[1]) if r else None


def _media_type(title_type: str) -> str | None:
    t = title_type.strip().lower()
    return "tv" if t in TV_TYPES else "movie" if t in MOVIE_TYPES else None


def resolve_imdb(s: Session, tmdb: TMDBClient, limit: int = 2000) -> dict:
    """Match pending IMDb rows to TMDB, then (re)derive the person's IMDb signals."""
    counts = {"matched": 0, "none": 0, "skipped": 0}
    pending = list(s.scalars(select(ImdbRow).where(ImdbRow.match == "pending").limit(limit)))
    for row in pending:
        want = _media_type(row.title_type)
        if row.title_type and want is None:  # episodes, games, shorts, podcasts
            row.match = "skipped"
            counts["skipped"] += 1
            continue
        hit = _known_tmdb(s, row.imdb_id)
        if hit is None:
            found = tmdb.find_imdb(row.imdb_id) or {}
            tv, mv = found.get("tv_results") or [], found.get("movie_results") or []
            if want == "tv" and tv:
                hit = (tv[0]["id"], "tv")
            elif want == "movie" and mv:
                hit = (mv[0]["id"], "movie")
            elif want is None and (tv or mv):
                hit = (tv[0]["id"], "tv") if tv else (mv[0]["id"], "movie")
        if hit is None:
            row.match = "none"
            counts["none"] += 1
            continue
        row.tmdb_id, row.media_type, row.match = hit[0], hit[1], "matched"
        counts["matched"] += 1
        source = "imdb_rating" if row.kind == "rating" else "imdb_watchlist"
        sig = s.get(TasteSignal, (row.plex_id, hit[0], hit[1], source))
        if sig is None:
            s.add(TasteSignal(plex_id=row.plex_id, tmdb_id=hit[0], media_type=hit[1], source=source,
                              title=row.title, rating=row.rating, noted_at=row.noted_at))
        else:
            sig.rating, sig.title, sig.noted_at = row.rating, row.title, row.noted_at
        s.flush()
    return counts


def _details(s: Session, tmdb: TMDBClient, tmdb_id: int, media_type: str) -> tuple[str, str | None]:
    """(name, poster_path), from our own titles when we have them, else TMDB."""
    t = s.get(Title, (tmdb_id, media_type))
    if t and t.name and t.poster_path:
        return t.name, t.poster_path
    d = (tmdb.tv(tmdb_id) if media_type == "tv" else tmdb.movie(tmdb_id)) or {}
    name = (t.name if t and t.name else None) or d.get("name") or d.get("title") or ""
    return name, d.get("poster_path") or (t.poster_path if t else None)


def _name(s: Session, tmdb: TMDBClient, tmdb_id: int, media_type: str) -> str:
    return _details(s, tmdb, tmdb_id, media_type)[0]


def sync_overseerr_requests(s: Session, overseerr: OverseerrClient, tmdb: TMDBClient) -> int:
    """Each member's own Overseerr requests become their signals (never anyone else's)."""
    n = 0
    for u in list(s.scalars(select(User).where(User.overseerr_id.is_not(None), User.removed_at.is_(None)))):
        for req in overseerr.requests_of(u.overseerr_id):
            key = (u.plex_id, req["tmdb_id"], req["media_type"], "overseerr")
            sig = s.get(TasteSignal, key)
            if sig is None:
                name, poster = _details(s, tmdb, req["tmdb_id"], req["media_type"])
                s.add(TasteSignal(plex_id=u.plex_id, tmdb_id=req["tmdb_id"], media_type=req["media_type"],
                                  source="overseerr", noted_at=req["created_at"], title=name,
                                  poster_path=poster))
                n += 1
            elif not sig.poster_path:
                sig.title, sig.poster_path = _details(s, tmdb, req["tmdb_id"], req["media_type"])
        s.flush()
    return n


def backfill_posters(s: Session, tmdb: TMDBClient, limit: int = 300) -> int:
    """Fill in names/posters for signals recorded without them (one TMDB lookup per title)."""
    n = 0
    missing = s.scalars(select(TasteSignal).where(TasteSignal.poster_path.is_(None)).limit(limit))
    cache: dict[tuple[int, str], tuple[str, str | None]] = {}
    for sig in list(missing):
        key = (sig.tmdb_id, sig.media_type)
        if key not in cache:
            cache[key] = _details(s, tmdb, *key)
        name, poster = cache[key]
        sig.title, sig.poster_path = sig.title or name, poster
        n += poster is not None
    s.flush()
    return n


def add(s: Session, plex_id: int, tmdb_id: int, media_type: str, source: str, title: str = "",
        noted_at: datetime | None = None, poster_path: str | None = None) -> bool:
    sig = s.get(TasteSignal, (plex_id, tmdb_id, media_type, source))
    if sig is not None:
        sig.use_for_picks = True  # saying it again turns it back on
        s.flush()
        return False
    s.add(TasteSignal(plex_id=plex_id, tmdb_id=tmdb_id, media_type=media_type, source=source,
                      title=title, noted_at=noted_at or utcnow(), poster_path=poster_path))
    s.flush()
    return True


def set_use(s: Session, plex_id: int, tmdb_id: int, media_type: str, use: bool) -> int:
    """Switch every signal for this title on/off for the caller's picks (not their exclusions)."""
    rows = list(s.scalars(select(TasteSignal).where(TasteSignal.plex_id == plex_id, TasteSignal.tmdb_id == tmdb_id,
                                                   TasteSignal.media_type == media_type)))
    for r in rows:
        r.use_for_picks = use
    s.flush()
    return len(rows)


def listing(s: Session, plex_id: int) -> list[dict]:
    """Every title in the caller's taste, one row per title with all its sources."""
    by_title: dict[tuple[int, str], dict] = {}
    for sig in s.scalars(select(TasteSignal).where(TasteSignal.plex_id == plex_id)):
        key = (sig.tmdb_id, sig.media_type)
        row = by_title.setdefault(key, {"tmdb_id": sig.tmdb_id, "media_type": sig.media_type, "title": sig.title,
                                        "poster_path": sig.poster_path, "sources": [], "rating": None,
                                        "use_for_picks": True, "noted_at": None})
        row["sources"].append(sig.source)
        row["title"] = row["title"] or sig.title
        row["poster_path"] = row["poster_path"] or sig.poster_path
        row["rating"] = sig.rating if sig.rating is not None else row["rating"]
        row["use_for_picks"] = row["use_for_picks"] and sig.use_for_picks
        d = sig.noted_at.isoformat()[:10] if sig.noted_at else None
        row["noted_at"] = max(filter(None, (row["noted_at"], d)), default=None)
    for row in by_title.values():
        row["sources"].sort(key=SOURCES.index)
    return sorted(by_title.values(), key=lambda r: (SOURCES.index(r["sources"][0]), r["title"].lower()))


def summary(s: Session, plex_id: int) -> dict:
    by_source = dict(s.execute(select(TasteSignal.source, func.count()).where(
        TasteSignal.plex_id == plex_id).group_by(TasteSignal.source)).all())
    pending = s.scalar(select(func.count()).select_from(ImdbRow).where(
        ImdbRow.plex_id == plex_id, ImdbRow.match == "pending")) or 0
    unmatched = s.scalar(select(func.count()).select_from(ImdbRow).where(
        ImdbRow.plex_id == plex_id, ImdbRow.match == "none")) or 0
    off = s.scalar(select(func.count(func.distinct(TasteSignal.tmdb_id))).where(
        TasteSignal.plex_id == plex_id, TasteSignal.use_for_picks.is_(False))) or 0
    return {"told": by_source.get("told", 0), "overseerr": by_source.get("overseerr", 0),
            "mentioned": by_source.get("mentioned", 0), "switched_off": off,
            "imdb_ratings": by_source.get("imdb_rating", 0), "imdb_watchlist": by_source.get("imdb_watchlist", 0),
            "imdb_pending": pending, "imdb_unmatched": unmatched}


# ── A title typed in chat → the TMDB entry they mean ─────────────────────────────────────────


def _norm(t: str | None) -> str:
    t = re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()
    return t[4:] if t.startswith("the ") else t


def known_keys(s: Session, plex_id: int) -> set[tuple[int, str]]:
    """(tmdb_id, media_type) this person watched on Plex or already has as taste."""
    plays = s.execute(select(Play.tmdb_id, Play.media_type).where(
        Play.plex_id == plex_id, Play.tmdb_id.is_not(None)).distinct())
    sigs = s.execute(select(TasteSignal.tmdb_id, TasteSignal.media_type).where(
        TasteSignal.plex_id == plex_id))
    return {(a, b) for a, b in plays} | {(a, b) for a, b in sigs}


def _year(r: dict) -> int | None:
    y = (r.get("first_air_date") or r.get("release_date") or "")[:4]
    return int(y) if y.isdigit() else None


def pick_title(results: list[dict], known: set[tuple[int, str]], title: str,
               year: int | None = None, media_type: str | None = None) -> dict:
    """Which of TMDB's search results someone means. Something they've watched wins (that's
    how 2015 Daredevil beats Born Again for a Daredevil fan), then an exact name match, then
    TMDB's own order. `pick` is None when it's a toss-up: two same-name titles, neither
    watched, no year to split them. Then the caller asks instead of guessing."""
    cands = [r for r in results if r.get("media_type") in ("tv", "movie")
             and (media_type is None or r["media_type"] == media_type)
             and (year is None or _year(r) is None or abs(_year(r) - year) <= 1)]
    out = [{"tmdb_id": r["id"], "media_type": r["media_type"], "name": r.get("name") or r.get("title") or "",
            "year": _year(r), "watched": (r["id"], r["media_type"]) in known} for r in cands[:8]]
    if not out:
        return {"pick": None, "candidates": []}
    want = _norm(title)
    exact = [c for c in out if _norm(c["name"]) == want]
    # Watched wins only when its name contains what they typed: "daredevil" → Daredevil: Born
    # Again if that's what they watch, but "game of thrones" must never become House of the Dragon.
    watched = [c for c in out if c["watched"] and want in _norm(c["name"])]
    if watched:
        pick = next((c for c in watched if _norm(c["name"]) == want), watched[0])
    elif len(exact) == 1 or (exact and year is not None):
        pick = exact[0]
    elif len(exact) > 1:
        pick = None
    else:
        pick = out[0]
    return {"pick": pick, "candidates": out}


def record(s: Session, tmdb: TMDBClient, plex_id: int, tmdb_id: int, media_type: str, liked: bool) -> dict:
    """Liked → a `told` taste seed. Disliked → a thumbs-down. Either way never picked for them."""
    from . import recs  # recs imports taste
    name, poster = _details(s, tmdb, tmdb_id, media_type)
    if liked:
        add(s, plex_id, tmdb_id, media_type, "told", name, poster_path=poster)
    else:
        recs.give_feedback(s, plex_id, tmdb_id, media_type, -1)
    return {"ok": True, "title": name, "tmdb_id": tmdb_id, "media_type": media_type, "liked": liked,
            "note": "Picks update in tonight's refresh."}
