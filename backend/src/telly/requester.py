"""Who is asking (spec §4–5).

plexbot mints a token per chat turn: ``<b64url(json{surface, uid, exp})>.<hex hmac>``, signed
with the shared secret. The token proves plexbot vouches for (surface, uid); this module then
maps that to a plex_id using only proven links. Nothing a model says can change the answer:
the token is set by plexbot's process, and no tool takes a user parameter.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from .clients.overseerr import OverseerrClient
from .config import get_settings
from .models import IdentityLink, User, aware, utcnow

log = logging.getLogger(__name__)

SURFACES = {"web", "discord"}
OVERSEERR_RECHECK = timedelta(hours=24)


@dataclass(frozen=True)
class Requester:
    surface: str
    uid: str
    name: str = ""


def _sig(secret: str, payload_b64: str) -> str:
    return hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()


def mint(surface: str, uid: str, name: str = "", ttl: int = 900, secret: str | None = None) -> str:
    """Same format plex-agent mints (kept here for tests and tooling)."""
    secret = secret or get_settings().shared_secret
    payload = json.dumps({"surface": surface, "uid": str(uid), "name": name,
                          "exp": int(time.time()) + ttl}, separators=(",", ":"))
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{b64}.{_sig(secret, b64)}"


def verify(token: str | None, secret: str | None = None) -> Requester | None:
    """Return the requester if the token is well-formed, signed, and unexpired."""
    secret = secret if secret is not None else get_settings().shared_secret
    if not secret or not token or token.count(".") != 1:
        return None
    b64, sig = token.split(".")
    if not hmac.compare_digest(_sig(secret, b64), sig):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    if data.get("surface") not in SURFACES or not data.get("uid"):
        return None
    if int(data.get("exp") or 0) < time.time():
        return None
    return Requester(surface=data["surface"], uid=str(data["uid"]), name=str(data.get("name") or ""))


def bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _active(s: Session, plex_id: int) -> bool:
    u = s.get(User, plex_id)
    return u is not None and u.removed_at is None


def resolve(s: Session, who: Requester, overseerr: OverseerrClient | None = None) -> int | None:
    """Map a verified requester to an active member's plex_id, or None (→ "link first").

    web:     Overseerr user id → Overseerr's own plexId (cached, rechecked daily).
    discord: only a row written by a completed /link.
    Never falls back to anyone else, the owner included.
    """
    link = s.get(IdentityLink, (who.surface, who.uid))

    if who.surface == "web":
        fresh = link is not None and aware(link.checked_at) > utcnow() - OVERSEERR_RECHECK
        if not fresh:
            try:
                plex_id = (overseerr or OverseerrClient()).plex_id_for(int(who.uid))
            except Exception as e:  # noqa: BLE001 — Overseerr down: use the cache if any
                log.warning("overseerr lookup failed for web:%s: %s", who.uid, e)
                plex_id = link.plex_id if link else None
            else:
                if plex_id is None or s.get(User, plex_id) is None:
                    if link is not None:
                        s.delete(link)
                    s.flush()
                    return None
                if link is None:
                    link = IdentityLink(surface="web", external_id=who.uid, plex_id=plex_id,
                                        source="overseerr")
                    s.add(link)
                link.plex_id, link.checked_at, link.display_name = plex_id, utcnow(), who.name
                s.flush()

    if link is None or not _active(s, link.plex_id):
        return None
    return link.plex_id
