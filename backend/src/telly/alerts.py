"""Alert fan-out (spec §6): each recent event → each active follower → each enabled channel.

At-most-once per (person, event, channel): the Delivery row is committed as `sending`
*before* the send. A crash mid-send leaves it `sending`, which is never retried, so the
worst case is one missed alert, never a double ping. Only a send that raised (`failed`) is
retried, up to alert_max_attempts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .clients.notify import DiscordDM, Ntfy
from .config import get_settings
from .models import AlertPref, Delivery, Event, Follow, IdentityLink, Title, User, utcnow
from .shows import describe

log = logging.getLogger(__name__)

TMDB_IMG = "https://image.tmdb.org/t/p/w342"


@dataclass
class DeliveryReport:
    sent: int = 0
    failed: int = 0
    lines: list[str] = field(default_factory=list)


def _channels(s: Session, plex_id: int, discord: DiscordDM | None, ntfy: Ntfy | None):
    pref = s.get(AlertPref, plex_id) or AlertPref(plex_id=plex_id, discord_enabled=True)
    out = []
    if discord is not None and pref.discord_enabled:
        did = s.scalar(select(IdentityLink.external_id).where(
            IdentityLink.surface == "discord", IdentityLink.plex_id == plex_id))
        if did:  # only ever a Discord id proven by /link (spec §6)
            out.append(("discord", did))
    if ntfy is not None and pref.ntfy_topic:
        out.append(("ntfy", pref.ntfy_topic))
    return out


def deliver(s: Session, discord: DiscordDM | None, ntfy: Ntfy | None,
            now: datetime | None = None) -> DeliveryReport:
    cfg = get_settings()
    now = now or utcnow()
    report = DeliveryReport()
    events = list(s.scalars(select(Event).where(
        Event.detected_at >= now - timedelta(hours=cfg.alert_max_age_hours))))
    for ev in events:
        if (ev.payload or {}).get("quiet"):
            continue  # recorded for the timeline, not news (e.g. a renewal announced months ago)
        title = s.get(Title, (ev.tmdb_id, "tv"))
        name = title.name if title else f"TMDB {ev.tmdb_id}"
        text = describe(ev.kind, ev.payload, name)
        followers = s.scalars(select(Follow.plex_id).join(User, User.plex_id == Follow.plex_id).where(
            Follow.tmdb_id == ev.tmdb_id, Follow.state == "following", User.removed_at.is_(None)))
        for plex_id in list(followers):
            for channel, target in _channels(s, plex_id, discord, ntfy):
                d = s.scalar(select(Delivery).where(Delivery.plex_id == plex_id,
                                                    Delivery.event_id == ev.id,
                                                    Delivery.channel == channel))
                if d is None:
                    d = Delivery(plex_id=plex_id, event_id=ev.id, channel=channel, attempts=0)
                    s.add(d)
                elif d.status != "failed" or d.attempts >= cfg.alert_max_attempts:
                    continue  # sent, in flight when we crashed, or out of retries
                d.status, d.attempts = "sending", d.attempts + 1
                try:
                    s.commit()
                except IntegrityError:  # another run claimed this (person, event, channel)
                    s.rollback()
                    continue
                try:
                    if channel == "discord":
                        embed = {"title": name, "description": text, "color": 0x8B5CF6,
                                 "url": f"{cfg.public_url}/show/{ev.tmdb_id}"}
                        if title and title.poster_path:
                            embed["thumbnail"] = {"url": TMDB_IMG + title.poster_path}
                        discord.send(target, f"📺 {text}", embed)
                    else:
                        ntfy.send(target, "Telly", text, click=f"{cfg.public_url}/show/{ev.tmdb_id}")
                except Exception as e:  # noqa: BLE001 — one bad send must not stop the rest
                    d.status, d.error = "failed", f"{type(e).__name__}: {e}"[:300]
                    report.failed += 1
                    log.warning("alert %s → %s/%s failed: %s", ev.id, plex_id, channel, e)
                else:
                    d.status, d.sent_at, d.error = "sent", utcnow(), None
                    report.sent += 1
                    report.lines.append(f"{channel} → {plex_id}: {text}")
                s.commit()
    return report
