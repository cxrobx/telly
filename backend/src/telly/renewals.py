"""Renewals from the news, for shows TMDB hasn't caught up on (weekly).

TMDB records a renewal only once someone adds the next season's row, which can trail the
announcement by weeks (SNW S5, Fallout S3 …). So once a week, for each followed show that's
between seasons with no next season on TMDB, Haiku searches the web. A "renewed" answer is
accepted only if:
  - it names the *next* season (last aired + 1),
  - its source is a trusted outlet (trades or the network/streamer itself), and
  - the cited page, fetched by Telly, mentions the show and a renewal (if the site lets us
    fetch it; trusted outlets that block bots are taken on the domain alone).
The event uses the same dedupe key as TMDB's own renewal path, so when TMDB catches up
nobody is alerted twice.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from . import llm
from .detect import ALIVE
from .models import Event, Follow, RenewalCheck, Title, User, aware, utcnow

log = logging.getLogger(__name__)

RECHECK = timedelta(days=7)
STALE_NEWS = timedelta(days=30)  # older announcements go on the timeline without an alert
MIN_GAP_DAYS = 21  # don't ask mid-season; the show is obviously still going
MAX_PER_RUN = 12

TRUSTED = {
    "deadline.com": "Deadline", "variety.com": "Variety", "hollywoodreporter.com": "THR",
    "tvline.com": "TVLine", "ew.com": "EW", "thewrap.com": "TheWrap", "indiewire.com": "IndieWire",
    "vulture.com": "Vulture", "hbo.com": "HBO", "max.com": "Max", "warnerbros.com": "Warner Bros.",
    "netflix.com": "Netflix", "apple.com": "Apple", "amazon.com": "Amazon",
    "aboutamazon.com": "Amazon", "disneyplus.com": "Disney+", "disney.com": "Disney",
    "thewaltdisneycompany.com": "Disney", "hulu.com": "Hulu", "fxnetworks.com": "FX",
    "paramountplus.com": "Paramount+", "paramount.com": "Paramount", "cbs.com": "CBS",
    "nbcuniversal.com": "NBCUniversal", "peacocktv.com": "Peacock", "amc.com": "AMC",
    "amcnetworks.com": "AMC", "starz.com": "Starz", "crunchyroll.com": "Crunchyroll",
    "adultswim.com": "Adult Swim", "startrek.com": "StarTrek.com",
}


@dataclass
class RenewalReport:
    checked: list[str] = field(default_factory=list)
    renewed: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def trusted_name(url: str | None) -> str | None:
    host = (urlparse(url or "").hostname or "").lower()
    for domain, name in TRUSTED.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def page_confirms(url: str, show: str, fetch=None) -> bool | None:
    """True/False if we could read the page; None if the site wouldn't let us."""
    try:
        r = (fetch or httpx.get)(url, follow_redirects=True, timeout=15.0,
                                 headers={"User-Agent": "Mozilla/5.0 (Telly renewal check)"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    text = _norm(r.text[:400_000])
    name = _norm(show)
    main = _norm(show.split(":")[0])
    return ("renew" in text) and (name in text or (len(main) > 3 and main in text))


def due(s: Session, today: date | None = None) -> list[Title]:
    """Followed (by an active member) shows that are alive, between seasons, with no next
    season on TMDB, and not checked in the last week."""
    today = today or date.today()
    followed = set(s.scalars(select(Follow.tmdb_id).join(User, User.plex_id == Follow.plex_id).where(
        Follow.state == "following", User.removed_at.is_(None))))
    out = []
    for t in s.scalars(select(Title).where(Title.media_type == "tv", Title.tmdb_id.in_(followed))):
        last = t.last_episode or {}
        if t.status not in ALIVE or t.next_episode or not last.get("air_date"):
            continue
        last_season = last.get("season") or 0
        if any((x.get("season") or 0) > last_season for x in t.seasons or []):
            continue  # TMDB already lists the next season
        if (today - date.fromisoformat(last["air_date"])).days < MIN_GAP_DAYS:
            continue
        if s.scalar(select(Event.id).where(
                Event.dedupe_key == f"renewed:{t.tmdb_id}:{last_season + 1}")):
            continue  # already known renewed
        chk = s.get(RenewalCheck, t.tmdb_id)
        if chk and aware(chk.checked_at) > utcnow() - RECHECK:
            continue
        out.append(t)
    return out[:MAX_PER_RUN]


def ask(t: Title, season: int) -> dict | None:
    networks = ", ".join(t.networks or []) or "unknown network"
    prompt = (
        f'Has the TV series "{t.name}" ({t.year or "?"}, {networks}) been officially renewed '
        f"for season {season}? Search the web. Answer renewed=true ONLY if the network/streamer "
        "announced it or a major trade outlet (Deadline, Variety, The Hollywood Reporter, "
        "TVLine) reports the official renewal. Rumors, fan petitions and 'in talks' are false. "
        "Reply with JSON only: "
        '{"renewed": true|false, "season": <int>, "source_url": "<url or null>", '
        '"source_name": "<outlet or null>", "announced": "YYYY-MM-DD or null", '
        '"quote": "<short quote from the source or null>"}'
    )
    out = llm.ask_json(prompt, web_search=True, timeout=240)
    return out if isinstance(out, dict) else None


def judge(t: Title, season: int, answer: dict | None, fetch=None) -> tuple[bool, str, dict]:
    """(accepted, why, payload). Pure apart from the optional page fetch."""
    if not answer or answer.get("renewed") is not True:
        return False, "not renewed", {}
    try:
        claimed = int(answer.get("season"))
    except (TypeError, ValueError):
        return False, "no season", {}
    if claimed != season:
        return False, f"season {claimed} isn't the next one ({season})", {}
    url = answer.get("source_url")
    outlet = trusted_name(url)
    if outlet is None:
        return False, f"untrusted source {url!r}", {}
    if page_confirms(url, t.name, fetch) is False:
        return False, "cited page doesn't mention a renewal of this show", {}
    return True, "ok", {"season": season, "via": "news", "source_url": url,
                        "source_name": outlet, "announced": announced_on(answer.get("announced"), url)}


def announced_on(model_date: str | None, url: str | None) -> str | None:
    """Trades put the month in the URL (/2025/05/). Models tend to fill an unknown date with
    today, which would make months-old news alert, so the earlier of the two wins."""
    m = re.search(r"/(20\d\d)/(0[1-9]|1[0-2])/", url or "")
    from_url = date(int(m.group(1)), int(m.group(2)), 1) if m else None
    try:
        from_model = date.fromisoformat(str(model_date)[:10]) if model_date else None
    except ValueError:
        from_model = None
    if from_model and from_url and (from_model.year, from_model.month) == (from_url.year, from_url.month):
        return from_model.isoformat()  # same month: the model's exact day is the better one
    dates = [d for d in (from_model, from_url) if d]
    return min(dates).isoformat() if dates else None


def _is_old(announced: str | None, today: date | None = None) -> bool:
    """A renewal announced long ago is a fact, not news: no alert. Unknown dates alert."""
    try:
        return (today or date.today()) - date.fromisoformat(str(announced)[:10]) > STALE_NEWS
    except ValueError:
        return False


def run(s: Session, asker=ask, fetch=None) -> RenewalReport:
    report = RenewalReport()
    if asker is ask and not llm.available():
        log.info("renewal check skipped: headless Claude unavailable")
        return report
    for t in due(s):
        season = ((t.last_episode or {}).get("season") or 0) + 1
        answer = asker(t, season)
        ok, why, payload = judge(t, season, answer, fetch)
        chk = s.get(RenewalCheck, t.tmdb_id) or RenewalCheck(tmdb_id=t.tmdb_id)
        chk.checked_at, chk.result = utcnow(), {"answer": answer, "accepted": ok, "why": why}
        s.merge(chk)
        report.checked.append(t.name)
        if ok:
            payload["quiet"] = _is_old(payload.get("announced"))
            s.execute(sqlite_insert(Event).values(
                tmdb_id=t.tmdb_id, kind="renewed", payload=payload,
                dedupe_key=f"renewed:{t.tmdb_id}:{season}", detected_at=utcnow(),
            ).on_conflict_do_nothing(index_elements=["dedupe_key"]))
            report.renewed.append(f"{t.name} S{season} ({payload['source_name']})")
        elif answer and answer.get("renewed") is True:
            report.rejected.append(f"{t.name}: {why}")
        s.commit()
    return report
