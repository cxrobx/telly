"""plexbot web-chat tokens, signed by Telly for its signed-in members.

For Overseerr, a Cloudflare Worker checks the Overseerr login and signs this token; on Telly,
Telly's own session is the proof and Telly signs it. plexbot can't tell the two apart: same
secret (PLEXBOT_AUTH_SECRET), same format, same identity (the member's Overseerr user id,
plexbot's "web" surface). So the chat, its history and its admin rights are the same ones the
person has in Overseerr.

Format, pinned by plex-agent's web.py (mint_token / _verify_token) and by a shared vector in
both repos' tests: ``<b64url(json{id, name, iat, exp})>.<b64url(hmac_sha256(secret, payload_b64))>``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from .config import get_settings

TTL = 3600


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def mint(overseerr_id: int | str, name: str = "", ttl: int = TTL, secret: str | None = None,
         now: int | None = None) -> str:
    secret = get_settings().plexbot_auth_secret if secret is None else secret
    if not secret:
        raise RuntimeError("TELLY_PLEXBOT_AUTH_SECRET is not set")
    iat = int(time.time()) if now is None else now
    payload = _b64(json.dumps({"id": str(overseerr_id), "name": name, "iat": iat, "exp": iat + ttl},
                              separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64(sig)}"
