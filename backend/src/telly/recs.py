"""Recommendations: what to watch next, per person.

1. Taste seeds from Plex history: log(episodes watched) decayed by recency (half-life 120d),
   plus what they told Telly they liked, IMDb ratings of 7+, thumbs up, and (weakly) Overseerr
   requests and chat mentions (taste.py). Top 12.
2. Candidates: TMDB recommendations for each seed (rank-discounted, weighted by the seed),
   plus this week's trending TV and movies (a smaller boost).
3. Drop what they've watched, follow, watchlisted, requested, rated on IMDb, or thumbed down; drop
   obscure titles (under 50 votes); nudge by rating.
4. Haiku re-ranks the top 40 into 20 and writes a one-line reason for each. If Haiku is
   unavailable, the code order stands and reasons are templated ("Because you watched …").

Trending (the home page rail, `trending_for`) is built in the same pass, from the same TMDB
calls: this week's trending titles they haven't seen and the home page doesn't already show as
a pick, ordered by buzz × freshness × fit. Fit is a direct match (TMDB recommends it off one of
their seeds) plus a genre profile of everything recommended off their seeds, so a bad fit sinks
but isn't hidden.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import llm
from .clients.tmdb import TMDBClient
from .ratings import lookup
from . import taste
from .models import (Feedback, Follow, LibraryItem, Play, Recommendation, TasteSignal, Title,
                     TrendingPick, User, WatchlistItem, aware, utcnow)

log = logging.getLogger(__name__)

HALF_LIFE_DAYS = 120
MAX_SEEDS = 12
PER_SEED = 20
MIN_VOTES = 50
SHORTLIST = 40
KEEP = 20
TRENDING_WEIGHT = 0.35
ANIMATION = 16  # TMDB genre id, the same for TV and movies
TRENDING_MIN_VOTES = 10  # this week's premieres haven't had time to collect 50
TRENDING_KEEP = 20
HOME_PICKS = 10  # the For you rail on the home page; trending never repeats one of these
FRESH_DAYS, STALE_DAYS = 60, 365
# How far an old title that's trending again sinks. TV sinks less: TMDB dates a show by its
# first season, and an old show trending is usually a new season, which is fresh.
STALE_FLOOR = {"movie": 0.45, "tv": 0.7}
MISFIT_FLOOR = 0.25  # a title far outside their genres keeps a quarter of its buzz


@dataclass
class Seed:
    tmdb_id: int
    media_type: str
    name: str
    weight: float


@dataclass
class Candidate:
    tmdb_id: int
    media_type: str
    title: str
    year: int | None
    overview: str
    poster_path: str | None
    backdrop_path: str | None
    rating: float
    score: float = 0.0
    because: dict[str, float] = field(default_factory=dict)
    trending: bool = False
    genre_ids: list[int] = field(default_factory=list)
    released: date | None = None
    trend_rank: int | None = None  # 0-based, within its media type's trending list
    anime: bool = False


def seeds_for(s: Session, plex_id: int, now: datetime | None = None) -> list[Seed]:
    now = now or utcnow()
    rows = s.execute(
        select(Play.tmdb_id, Play.media_type, Title.name,
               func.count(func.distinct(Play.season * 10000 + func.coalesce(Play.episode, 0))),
               func.max(Play.viewed_at))
        .join(Title, (Title.tmdb_id == Play.tmdb_id) & (Title.media_type == Play.media_type))
        .where(Play.plex_id == plex_id, Play.tmdb_id.is_not(None))
        .group_by(Play.tmdb_id, Play.media_type, Title.name))
    seeds: dict[tuple[int, str], Seed] = {}
    for tmdb_id, media_type, name, n, last in rows:
        n = 3 if media_type == "movie" else max(int(n or 1), 1)  # a movie ≈ a few episodes
        age = (now - aware(last)).days if isinstance(last, datetime) else 365
        w = math.log1p(n) * 0.5 ** (max(age, 0) / HALF_LIFE_DAYS)
        seeds[(tmdb_id, media_type)] = Seed(tmdb_id, media_type, name, w)
    top = max((x.weight for x in seeds.values()), default=1.0)
    # Taste from outside Plex (taste.py). Foundational: what they told Telly they liked and
    # their IMDb ratings. Weak: Overseerr requests and chat mentions, which show interest, not
    # liking (a request can be a try-out or for a friend). Each can be switched off per title.
    for sig in s.scalars(select(TasteSignal).where(TasteSignal.plex_id == plex_id,
                                                   TasteSignal.use_for_picks.is_(True))):
        key = (sig.tmdb_id, sig.media_type)
        if sig.source == "told":
            w = top
        elif sig.source == "imdb_rating" and (sig.rating or 0) >= taste.LIKE_FROM:
            w = top * (sig.rating - 5) / 5 * 0.8
        elif sig.source in taste.WEAK_SOURCES:
            age = (now - aware(sig.noted_at)).days if sig.noted_at else 365
            w = math.log1p(3) * taste.WEAK_FACTOR * 0.5 ** (max(age, 0) / HALF_LIFE_DAYS)
        else:
            continue  # a watchlist entry is interest, not taste; a low rating is a dislike
        name = sig.title or (seeds[key].name if key in seeds else "")
        if name and (key not in seeds or seeds[key].weight < w):
            seeds[key] = Seed(sig.tmdb_id, sig.media_type, name, w)
    for f in s.scalars(select(Feedback).where(Feedback.plex_id == plex_id, Feedback.value > 0)):
        key = (f.tmdb_id, f.media_type)
        name = f.title or (seeds[key].name if key in seeds else "")
        seeds[key] = Seed(f.tmdb_id, f.media_type, name, top * 1.1)  # asked for beats watched
    return sorted(seeds.values(), key=lambda x: -x.weight)[:MAX_SEEDS]


def _candidate(r: dict, media_type: str) -> Candidate:
    date_ = r.get("first_air_date") or r.get("release_date") or ""
    try:
        released = date.fromisoformat(date_[:10])
    except ValueError:
        released = None
    return Candidate(
        tmdb_id=r["id"], media_type=media_type, title=r.get("name") or r.get("title") or "",
        year=int(date_[:4]) if date_[:4].isdigit() else None,
        overview=(r.get("overview") or "")[:300], poster_path=r.get("poster_path"),
        backdrop_path=r.get("backdrop_path"), rating=float(r.get("vote_average") or 0),
        genre_ids=[int(g) for g in r.get("genre_ids") or []], released=released,
        anime=is_anime(r))


def is_anime(r: dict) -> bool:
    """Japanese animation: TMDB has no anime genre, so Animation + Japanese origin."""
    return ANIMATION in (r.get("genre_ids") or []) and (
        r.get("original_language") == "ja" or "JP" in (r.get("origin_country") or []))


def _excluded(s: Session, plex_id: int) -> set[tuple[int, str]]:
    ex = set(s.execute(select(Play.tmdb_id, Play.media_type).where(
        Play.plex_id == plex_id, Play.tmdb_id.is_not(None))).all())
    ex |= {(t, "tv") for t in s.scalars(select(Follow.tmdb_id).where(Follow.plex_id == plex_id))}
    ex |= set(s.execute(select(WatchlistItem.tmdb_id, WatchlistItem.media_type).where(
        WatchlistItem.plex_id == plex_id)).all())
    ex |= set(s.execute(select(Feedback.tmdb_id, Feedback.media_type).where(
        Feedback.plex_id == plex_id, Feedback.value < 0)).all())
    # seen, rated, requested or watchlisted anywhere: they know about it already
    ex |= set(s.execute(select(TasteSignal.tmdb_id, TasteSignal.media_type).where(
        TasteSignal.plex_id == plex_id)).all())
    return {(int(a), b) for a, b in ex}


def candidates(s: Session, tmdb: TMDBClient, plex_id: int, seeds: list[Seed],
               trending: dict[str, list[dict]]) -> list[Candidate]:
    excluded = _excluded(s, plex_id)
    pool: dict[tuple[int, str], Candidate] = {}
    top = max((x.weight for x in seeds), default=1.0)

    def consider(r: dict, media_type: str, min_votes: int = MIN_VOTES) -> Candidate | None:
        key = (r.get("id"), media_type)
        if not r.get("id") or key in excluded or r.get("adult") or (r.get("vote_count") or 0) < min_votes:
            return None
        if key not in pool:
            pool[key] = _candidate(r, media_type)
        return pool[key]

    for seed in seeds:
        for rank, r in enumerate(tmdb.recommendations(seed.media_type, seed.tmdb_id)[:PER_SEED]):
            c = consider(r, seed.media_type)
            if c is not None:
                gain = seed.weight / math.sqrt(rank + 1)
                c.score += gain
                c.because[seed.name] = c.because.get(seed.name, 0) + gain
    for media_type, results in trending.items():
        for rank, r in enumerate(results[:20]):
            c = consider(r, media_type, TRENDING_MIN_VOTES)
            if c is not None:
                c.trending = True
                c.trend_rank = rank
                c.score += TRENDING_WEIGHT * top / math.sqrt(rank + 1)
    for c in pool.values():
        c.score *= 0.6 + 0.4 * (c.rating / 10)
    return sorted(pool.values(), key=lambda c: -c.score)


def _because(c: Candidate) -> list[str]:
    return [name for name, _ in sorted(c.because.items(), key=lambda kv: -kv[1])[:2]]


def template_reason(c: Candidate) -> str:
    names = _because(c)
    if len(names) >= 2:
        return f"Because you like {names[0]} and {names[1]}."
    if names:
        return f"Because you like {names[0]}." + (" Trending this week too." if c.trending else "")
    return "Trending this week."


def rerank(seeds: list[Seed], shortlist: list[Candidate]) -> list[tuple[Candidate, str]] | None:
    """Haiku picks and orders KEEP of the shortlist with a reason each; None = use code order."""
    if not llm.available() or not shortlist:
        return None
    taste = "; ".join(f"{x.name} ({'show' if x.media_type == 'tv' else 'movie'})" for x in seeds)
    lines = [
        f'{i}. [{c.media_type}:{c.tmdb_id}] {c.title} ({c.year or "?"}), rating {c.rating:.1f}'
        f'{", trending" if c.trending else ""}; similar to: {", ".join(_because(c)) or "n/a"}. '
        f'{c.overview[:160]}'
        for i, c in enumerate(shortlist, 1)
    ]
    prompt = (
        "You pick what someone should watch next on their Plex server.\n"
        f"What they've watched most (strongest first): {taste}\n\n"
        "Candidates:\n" + "\n".join(lines) + "\n\n"
        f"Choose the best {KEEP} for this person, best first. Favor real fit with their taste "
        "over popularity; keep a mix of shows and movies if both fit. For each, write one "
        "specific sentence (max 18 words) saying why, naming something they watched when "
        "it helps. No spoilers. Reply with JSON only: "
        '[{"key": "tv:123", "reason": "..."}]'
    )
    out = llm.ask_json(prompt)
    if not isinstance(out, list):
        return None
    by_key = {f"{c.media_type}:{c.tmdb_id}": c for c in shortlist}
    picked, seen = [], set()
    for row in out:
        key = str(row.get("key", "")) if isinstance(row, dict) else ""
        reason = str(row.get("reason", "")).strip() if isinstance(row, dict) else ""
        if key in by_key and key not in seen and reason:
            seen.add(key)
            picked.append((by_key[key], reason[:200]))
    if len(picked) < KEEP // 2:
        return None  # the model went off the rails; the code order is safer
    for c in shortlist:  # top up from code order if it returned fewer than KEEP
        if len(picked) >= KEEP:
            break
        if f"{c.media_type}:{c.tmdb_id}" not in seen:
            picked.append((c, template_reason(c)))
    return picked[:KEEP]


def genre_profile(pool: list[Candidate]) -> dict[int, float]:
    """Genre → 0..1 affinity, from everything TMDB recommended off their seeds, weighted by how
    strongly. It covers movies and shows alike (our own Title rows only have TV genres)."""
    weight: dict[int, float] = defaultdict(float)
    for c in pool:
        fit = sum(c.because.values())
        for g in c.genre_ids:
            weight[g] += fit
    top = max(weight.values(), default=0.0)
    return {g: w / top for g, w in weight.items()} if top else {}


def freshness(c: Candidate, today: date) -> float:
    if c.released is None:
        return STALE_FLOOR[c.media_type]
    age = (today - c.released).days
    if age <= FRESH_DAYS:
        return 1.0  # also anything not out yet
    floor = STALE_FLOOR[c.media_type]
    return max(floor, 1 - (1 - floor) * (age - FRESH_DAYS) / (STALE_DAYS - FRESH_DAYS))


def trending_for(pool: list[Candidate], skip: set[tuple[int, str]],
                 today: date | None = None) -> list[tuple[Candidate, float, str]]:
    """This week's trending titles, best for them first: buzz × freshness × fit.

    `pool` is `candidates()` output (already without anything they've seen or rejected);
    `skip` is the picks the home page shows, so the two rails never repeat. Only those: the
    best trending fits are often picks too, and skipping all 20 would hollow the rail out."""
    today = today or utcnow().date()
    profile = genre_profile(pool)
    top_direct = max((sum(c.because.values()) for c in pool), default=0.0) or 1.0
    out = []
    for c in pool:
        if c.trend_rank is None or (c.tmdb_id, c.media_type) in skip:
            continue
        buzz = 1 / math.sqrt(c.trend_rank + 1)
        if profile:
            genres = [profile.get(g, 0.0) for g in c.genre_ids]
            genre_fit = sum(genres) / len(genres) if genres else 0.5
            fit = MISFIT_FLOOR + (1 - MISFIT_FLOOR) * genre_fit
            fit += 0.5 * min(1.0, sum(c.because.values()) / top_direct)
        else:
            fit = 1.0  # too little history to judge: plain buzz × freshness
        fresh = freshness(c, today)
        score = buzz * fresh * fit * (0.6 + 0.4 * c.rating / 10)
        out.append((c, score, trending_reason(c, fresh == 1.0, today)))
    out.sort(key=lambda x: -x[1])
    return out[:TRENDING_KEEP]


def trending_reason(c: Candidate, fresh: bool, today: date) -> str:
    parts = [f"#{c.trend_rank + 1} in {'TV' if c.media_type == 'tv' else 'movies'} this week"]
    names = _because(c)
    if names:
        parts.append(f"like {names[0]}")
    elif fresh:
        parts.append("just out" if c.released and c.released <= today else "coming soon")
    return " · ".join(parts)


def build_for(s: Session, tmdb: TMDBClient, plex_id: int,
              trending: dict[str, list[dict]]) -> int:
    seeds = seeds_for(s, plex_id)
    if not seeds:
        return 0
    ranked = candidates(s, tmdb, plex_id, seeds, trending)
    shortlist = ranked[:SHORTLIST]
    # Without Haiku, a pick that was already on their list keeps the reason it had (often
    # Haiku's) rather than dropping to the template.
    before = {(r.tmdb_id, r.media_type): r.reason for r in s.scalars(
        select(Recommendation).where(Recommendation.plex_id == plex_id)) if r.reason}
    picked = rerank(seeds, shortlist) or [
        (c, before.get((c.tmdb_id, c.media_type)) or template_reason(c)) for c in shortlist[:KEEP]]
    library = set(s.execute(select(LibraryItem.tmdb_id, LibraryItem.media_type)).all())
    s.execute(delete(Recommendation).where(Recommendation.plex_id == plex_id))
    now = utcnow()
    for rank, (c, reason) in enumerate(picked, 1):
        s.add(Recommendation(
            plex_id=plex_id, tmdb_id=c.tmdb_id, media_type=c.media_type, rank=rank,
            score=round(c.score, 4), title=c.title, year=c.year, poster_path=c.poster_path,
            backdrop_path=c.backdrop_path, overview=c.overview, reason=reason,
            because=_because(c), trending=c.trending, anime=c.anime,
            in_library=(c.tmdb_id, c.media_type) in library, generated_at=now))
    s.execute(delete(TrendingPick).where(TrendingPick.plex_id == plex_id))
    for rank, (c, score, reason) in enumerate(
            trending_for(ranked, {(c.tmdb_id, c.media_type) for c, _ in picked[:HOME_PICKS]}), 1):
        s.add(TrendingPick(
            plex_id=plex_id, tmdb_id=c.tmdb_id, media_type=c.media_type, rank=rank,
            score=round(score, 4), title=c.title, year=c.year, poster_path=c.poster_path,
            backdrop_path=c.backdrop_path, overview=c.overview, reason=reason,
            in_library=(c.tmdb_id, c.media_type) in library, generated_at=now))
    s.flush()
    return len(picked)


def build_all(s: Session, tmdb: TMDBClient) -> dict[int, int]:
    """Rebuild picks for every member with an Overseerr account. Chris's call (2026-09-27):
    without one they can't request anything or use plexbot's web chat, so a nightly Haiku
    run for them is wasted compute. Their existing picks are left as they are."""
    trending = {"tv": tmdb.trending("tv"), "movie": tmdb.trending("movie")}
    out = {}
    for u in list(s.scalars(select(User).where(User.removed_at.is_(None), User.overseerr_id.is_not(None)))):
        try:
            out[u.plex_id] = build_for(s, tmdb, u.plex_id, trending)
            s.commit()
        except Exception as e:  # noqa: BLE001 — one person's failure mustn't stop the rest
            s.rollback()
            log.warning("recs failed for %s: %s", u.plex_id, e)
    return out


def for_user(s: Session, plex_id: int, media: str = "any", limit: int = 20) -> list[dict]:
    q = select(Recommendation).where(Recommendation.plex_id == plex_id)
    if media in ("tv", "movie"):
        q = q.where(Recommendation.media_type == media)
    elif media == "anime":  # shows and movies alike
        q = q.where(Recommendation.anime.is_(True))
    rows = list(s.scalars(q.order_by(Recommendation.rank).limit(limit)))
    scores = lookup(s, [(r.tmdb_id, r.media_type) for r in rows])
    return [{
        "tmdb_id": r.tmdb_id, "media_type": r.media_type, "title": r.title, "year": r.year,
        "reason": r.reason, "because": r.because, "trending": r.trending,
        "in_library": r.in_library, "anime": r.anime, "poster_path": r.poster_path,
        "backdrop_path": r.backdrop_path, "overview": r.overview,
        "ratings": scores.get((r.tmdb_id, r.media_type)),
    } for r in rows]


def trending_for_user(s: Session, plex_id: int, limit: int = 20) -> list[dict]:
    rows = list(s.scalars(select(TrendingPick).where(TrendingPick.plex_id == plex_id)
                          .order_by(TrendingPick.rank).limit(limit)))
    scores = lookup(s, [(r.tmdb_id, r.media_type) for r in rows])
    return [{
        "tmdb_id": r.tmdb_id, "media_type": r.media_type, "title": r.title, "year": r.year,
        "reason": r.reason, "because": [], "trending": True, "in_library": r.in_library,
        "poster_path": r.poster_path, "backdrop_path": r.backdrop_path, "overview": r.overview,
        "ratings": scores.get((r.tmdb_id, r.media_type)),
    } for r in rows]


def give_feedback(s: Session, plex_id: int, tmdb_id: int, media_type: str, value: int) -> dict:
    """value: +1, -1, or 0 to clear. A thumbs down also drops it from their current list."""
    f = s.get(Feedback, (plex_id, tmdb_id, media_type))
    rec = s.get(Recommendation, (plex_id, tmdb_id, media_type))
    title = rec.title if rec else (f.title if f else "")
    if value == 0:
        if f is not None:
            s.delete(f)
    elif f is None:
        s.add(Feedback(plex_id=plex_id, tmdb_id=tmdb_id, media_type=media_type, value=value, title=title))
    else:
        f.value = value
    if value < 0 and rec is not None:
        s.delete(rec)
    if value < 0 and (hot := s.get(TrendingPick, (plex_id, tmdb_id, media_type))) is not None:
        s.delete(hot)
    s.flush()
    return {"ok": True, "value": value}
