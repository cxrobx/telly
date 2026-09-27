"""Encryption at rest for people's Plex tokens (spec §3), and signed session cookies."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import get_settings

COOKIE = "telly_session"


def _fernet() -> Fernet:
    key = get_settings().token_key
    if not key:
        raise RuntimeError("TELLY_TOKEN_KEY is not set; refusing to store a Plex token")
    return Fernet(key.encode())


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(blob: str) -> str | None:
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def _signer() -> URLSafeTimedSerializer:
    secret = get_settings().session_secret
    if not secret:
        raise RuntimeError("TELLY_SESSION_SECRET is not set")
    return URLSafeTimedSerializer(secret, salt="telly-session")


def make_session(plex_id: int) -> str:
    return _signer().dumps({"pid": plex_id})


def read_session(cookie: str | None) -> int | None:
    """plex_id from a valid, unexpired cookie; None otherwise (fails closed if unconfigured)."""
    if not cookie or not get_settings().session_secret:
        return None
    try:
        data = _signer().loads(cookie, max_age=get_settings().session_days * 86400)
    except (BadSignature, SignatureExpired):
        return None
    pid = data.get("pid") if isinstance(data, dict) else None
    return int(pid) if isinstance(pid, int) else None
