"""The scheduled jobs, composed from sync + poll + alerts. Each opens its own session."""

from __future__ import annotations

import logging

from .alerts import DeliveryReport, deliver
from .clients.notify import DiscordDM, Ntfy
from .clients.plex import PlexClient
from .clients.tmdb import TMDBClient
from .config import get_settings
from .db import session_scope
from .clients.overseerr import OverseerrClient
from .clients.plextv import PlexTV
from .plexdata import PlexDataReport, sync_library, sync_overseerr_ids, sync_watchlists
from .poll import PollReport, poll
from . import ratings, recs, renewals, taste
from .sync import SyncReport, infer_follows, sync_history, sync_users

log = logging.getLogger(__name__)


def run_sync() -> SyncReport:
    report = SyncReport()
    with session_scope() as s:
        plex = PlexClient()
        sync_users(s, plex, report)
        sync_history(s, plex, report)
    log.info("sync: %s", report)
    return report


def run_poll(hot_only: bool = False) -> PollReport:
    with session_scope() as s:
        report = poll(s, TMDBClient(), hot_only=hot_only)
    log.info("poll(hot=%s): polled=%d events=%d errors=%d", hot_only, report.polled,
             len(report.events), len(report.errors))
    return report


def run_infer() -> SyncReport:
    report = SyncReport()
    with session_scope() as s:
        infer_follows(s, report)
    return report


def run_deliver() -> DeliveryReport:
    cfg = get_settings()
    discord = DiscordDM() if cfg.discord_bot_token else None
    with session_scope() as s:
        report = deliver(s, discord, Ntfy())
    log.info("deliver: sent=%d failed=%d", report.sent, report.failed)
    return report


def run_plexdata() -> PlexDataReport:
    """Library, watchlists, Overseerr ids. Each part is independent: one failing (Overseerr
    down, say) mustn't stop the others."""
    report = PlexDataReport()
    for name, fn in (("library", lambda s: sync_library(s, PlexClient(), report)),
                     ("watchlists", lambda s: sync_watchlists(s, PlexTV(), report)),
                     ("overseerr", lambda s: sync_overseerr_ids(s, OverseerrClient(), report)),
                     ("requests", lambda s: taste.sync_overseerr_requests(s, OverseerrClient(), TMDBClient())),
                     ("imdb", lambda s: taste.resolve_imdb(s, TMDBClient())),
                     ("posters", lambda s: taste.backfill_posters(s, TMDBClient()))):
        try:
            with session_scope() as s:
                fn(s)
        except Exception as e:  # noqa: BLE001
            log.warning("plexdata %s failed: %s", name, e)
    log.info("plexdata: %s", report)
    return report


def run_recs() -> dict[int, int]:
    with session_scope() as s:
        out = recs.build_all(s, TMDBClient())
    log.info("recs: %s", out)
    return out


def run_renewals() -> renewals.RenewalReport:
    with session_scope() as s:
        report = renewals.run(s)
    log.info("renewals: %s", report)
    run_deliver()
    return report


def run_ratings(force: bool = False) -> ratings.RatingsReport:
    if not get_settings().omdb_api_key:
        log.info("ratings skipped: no OMDb key")
        return ratings.RatingsReport()
    rt = ratings.MDBList().rt if get_settings().mdblist_api_key else None
    with session_scope() as s:
        report = ratings.refresh(s, TMDBClient(), ratings.OMDb(), rt_source=rt, force=force)
    log.info("ratings: %s", report)
    return report


def run_all() -> tuple:
    """Nightly: history (new shows become titles) and watchlists (new follows) first, then
    poll, then infer follows (needs each show's aired-episode count from the poll), then
    alerts, then recommendations (needs the library and watchlists)."""
    return (run_sync(), run_plexdata(), run_poll(), run_infer(), run_deliver(), run_recs(),
            run_ratings())


def run_hot() -> tuple[PollReport, DeliveryReport]:
    """Hourly: shows airing around now, then alerts, so a new episode lands within the hour."""
    return run_poll(hot_only=True), run_deliver()
