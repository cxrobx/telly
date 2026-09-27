"""/link: prove a Discord id and a Plex account belong to the same person (spec §4).

plexbot vouches for the Discord side (its requester token) and asks for a code; the person
opens the DM'd link and signs in with Plex, which proves the Plex side. Nothing typed by
the person is ever taken as proof.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from .clients.plextv import PlexTV
from .config import get_settings
from .models import IdentityLink, LinkCode, User, aware, utcnow

CODE_TTL = timedelta(minutes=10)


class LinkError(Exception):
    pass


def create_code(s: Session, discord_id: str, discord_name: str) -> str:
    code = secrets.token_urlsafe(18)
    s.add(LinkCode(code=code, discord_id=discord_id, discord_name=discord_name,
                   expires_at=utcnow() + CODE_TTL))
    s.flush()
    return f"{get_settings().public_url}/link?code={code}"


def _live(s: Session, code: str) -> LinkCode:
    lc = s.get(LinkCode, code)
    if lc is None or lc.used_at is not None or aware(lc.expires_at) < utcnow():
        raise LinkError("This link has expired or was already used. Say !link in #plexbot for a new one.")
    return lc


def describe_code(s: Session, code: str) -> dict:
    return {"discord_name": _live(s, code).discord_name}


def start(s: Session, plextv: PlexTV, code: str, forward_url: str | None = None) -> dict:
    lc = _live(s, code)
    pin = plextv.create_pin()
    lc.pin_id = pin["id"]
    s.flush()
    return {"auth_url": plextv.auth_url(pin["code"], forward_url), "discord_name": lc.discord_name}


def finish(s: Session, plextv: PlexTV, code: str) -> dict:
    lc = _live(s, code)
    if lc.pin_id is None:
        raise LinkError("Start by signing in with Plex.")
    token = plextv.pin_token(lc.pin_id)
    if token is None:
        return {"done": False}
    who = plextv.user(token)  # the token is used once and not kept (linking needs no watchlist)
    member = s.get(User, who["id"])
    if member is None or member.removed_at is not None:
        raise LinkError("That Plex account isn't a member of this Plex server.")
    link = s.get(IdentityLink, ("discord", lc.discord_id))
    if link is None:
        link = IdentityLink(surface="discord", external_id=lc.discord_id, plex_id=who["id"], source="link")
        s.add(link)
    link.plex_id, link.source, link.display_name, link.checked_at = who["id"], "link", lc.discord_name, utcnow()
    lc.used_at = utcnow()
    s.flush()
    return {"done": True, "plex_username": who["username"], "discord_name": lc.discord_name}


def unlink(s: Session, discord_id: str) -> bool:
    link = s.get(IdentityLink, ("discord", discord_id))
    if link is None:
        return False
    s.delete(link)
    s.flush()
    return True
