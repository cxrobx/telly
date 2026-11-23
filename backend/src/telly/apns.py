"""Admin alerts to the owner's iOS app (telly-admin, a Ruddarr fork) through Apple's push
service. Ported from cxfeed's apns.py.

The server signs a short provider token with the team's APNs key (ES256, token auth) and posts
over HTTP/2 to Apple, the only host it ever talks to: a registered device token can't make
Telly POST anywhere else. The key comes from the environment (TELLY_APNS_KEY_P8: the .p8 file,
base64; TELLY_APNS_KEY_ID), never the repo. Until both are set this is **dormant**: it logs
once and sends nothing.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import ApnsDevice, User

log = logging.getLogger(__name__)
PRODUCTION = "https://api.push.apple.com"
SANDBOX = "https://api.sandbox.push.apple.com"
TOKEN = re.compile(r"^[0-9a-f]{64,200}$")
ENVIRONMENTS = ("development", "production")
# Apple refuses a provider token older than an hour and throttles one renewed more than every 20 min.
TOKEN_LIFE = 45 * 60
GONE = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}

_lock = threading.Lock()
_cached: tuple[float, str] | None = None
_dormant_logged = False


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def configured() -> bool:
    s = get_settings()
    return bool(s.apns_key_p8 and s.apns_key_id)


def token_ok(token: str) -> bool:
    return bool(TOKEN.match(token))


def _key() -> ec.EllipticCurvePrivateKey:
    return serialization.load_pem_private_key(base64.b64decode(get_settings().apns_key_p8), None)


def provider_token(key: ec.EllipticCurvePrivateKey | None = None, at: float | None = None) -> str:
    """The bearer token Apple wants: a JWT signed with the APNs key, reused for TOKEN_LIFE."""
    global _cached
    at = at or time.time()
    if key is None:
        with _lock:
            if _cached and at - _cached[0] < TOKEN_LIFE:
                return _cached[1]
    cfg = get_settings()
    head = b64u(json.dumps({"alg": "ES256", "kid": cfg.apns_key_id}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"iss": cfg.apns_team_id, "iat": int(at)}, separators=(",", ":")).encode())
    r, s = decode_dss_signature((key or _key()).sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    jwt = f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    if key is None:
        with _lock:
            _cached = (at, jwt)
    return jwt


def payload(message: dict) -> dict:
    """{title, body, deeplink} as an APNs alert. `deeplink` is the app's own route
    (tellyadmin://movies/open/{id}, QuickActions.swift)."""
    body = {"aps": {"alert": {"title": message.get("title", "Telly"), "body": message.get("body", "")},
                    "sound": "default", "thread-id": message.get("thread", "telly")}}
    if message.get("deeplink"):
        body["deeplink"] = message["deeplink"]
    return body


def client() -> httpx.Client:
    return httpx.Client(http2=True, timeout=20, follow_redirects=False)


def send(s: Session, device: ApnsDevice, message: dict, c: httpx.Client) -> bool:
    """True when Apple took it. A token Apple says is gone is forgotten."""
    if not configured() or not token_ok(device.token):
        return False
    host = SANDBOX if device.environment == "development" else PRODUCTION
    headers = {"authorization": f"bearer {provider_token()}", "apns-topic": get_settings().apns_topic,
               "apns-push-type": "alert", "apns-priority": "10",
               "apns-expiration": str(int(time.time()) + 86400)}
    if message.get("collapse"):
        headers["apns-collapse-id"] = str(message["collapse"])[:64]
    r = c.post(f"{host}/3/device/{device.token}", content=json.dumps(payload(message)).encode(),
               headers=headers)
    if r.status_code == 200:
        return True
    reason = ""
    try:
        reason = r.json().get("reason", "")
    except ValueError:
        pass
    if r.status_code == 410 or reason in GONE:
        s.delete(device)
        s.flush()
        log.info("apns: forgot a device Apple says is gone (%s)", reason or r.status_code)
        return False
    log.warning("apns refused (%s): %s", r.status_code, reason or r.text[:200])
    return False


def owner_devices(s: Session) -> list[ApnsDevice]:
    """Only the owner's phones: registration is owner-only, and this re-checks it at send time."""
    return list(s.scalars(select(ApnsDevice).join(User, User.plex_id == ApnsDevice.plex_id).where(
        User.is_owner.is_(True), User.removed_at.is_(None))))


def notify_owner(s: Session, message: dict) -> int:
    """Send one admin alert to every owner device. Returns how many Apple took."""
    global _dormant_logged
    if not configured():
        if not _dormant_logged:
            log.info("apns: dormant (no TELLY_APNS_KEY_P8 / TELLY_APNS_KEY_ID); admin alerts are not sent")
            _dormant_logged = True
        return 0
    devices = owner_devices(s)
    if not devices:
        return 0
    sent = 0
    with client() as c:
        for d in devices:
            try:
                sent += send(s, d, message, c)
            except httpx.HTTPError as e:
                log.warning("apns send failed: %s", type(e).__name__)
    return sent
