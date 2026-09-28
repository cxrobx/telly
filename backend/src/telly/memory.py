"""Per-person memory: what someone told Telly or plexbot about themselves (docs/spec-memory.md).

Every function takes the caller's plex_id from its caller (the MCP's token, the web session),
never from the model or a request body, and touches only that person's rows. A memory id
belonging to someone else is simply "not found".

Nothing here logs memory text: the repo is public and logs get shared.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import Memory, aware, utcnow

KINDS = ("preference", "setup", "plan")
SOURCES = ("chat", "told", "web")
MAX_LEN = 200
MAX_PER_PERSON = 30
PLAN_DAYS = 60

# What code can catch of the never-saved list (spec §1). Sensitive topics are the model's call
# (system prompt + tool description); these are the mechanical ones.
_NEVER = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "an email address"),
    (re.compile(r"(?<!\d)(?:\+?\d[\s().-]?){10,}(?!\d)"), "a phone number"),
    (re.compile(r"\b(?:passw(?:or)?d|passcode|api[ _-]?key|token|secret|pin code|credit card|"
                r"card number|cvv|iban|routing number)\b", re.I), "a credential or payment detail"),
    (re.compile(r"\b[A-Za-z0-9_-]{32,}\b"), "something that looks like a key or token"),
    (re.compile(r"\b\d{1,5}\s+\w+(?:\s\w+)?\s+(?:st|street|ave|avenue|rd|road|blvd|lane|ln|dr|drive|"
                r"ct|court|way)\b", re.I), "a street address"),
]


class MemoryError(ValueError):
    """Why a memory can't be saved, worded for the person (the model relays it)."""


class MemoryNotFound(MemoryError):
    """No such memory for this person (someone else's id included)."""


def _clean(text: str, kind: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        raise MemoryError("A memory needs some text.")
    if len(text) > MAX_LEN:
        raise MemoryError(f"Keep a memory under {MAX_LEN} characters; this one is {len(text)}.")
    if kind not in KINDS:
        raise MemoryError(f"kind must be one of {', '.join(KINDS)}.")
    for pattern, what in _NEVER:
        if pattern.search(text):
            raise MemoryError(f"Telly doesn't save {what}. Leave that out.")
    return text


def _live(plex_id: int):
    now = utcnow()
    return select(Memory).where(Memory.plex_id == plex_id).where(
        (Memory.expires_at.is_(None)) | (Memory.expires_at > now))


def _row(m: Memory) -> dict:
    out = {"id": m.id, "kind": m.kind, "text": m.text, "source": m.source,
           "updated_at": aware(m.updated_at).date().isoformat()}
    if m.expires_at:
        out["expires"] = aware(m.expires_at).date().isoformat()
    return out


def listing(s: Session, plex_id: int) -> list[dict]:
    """Oldest first, so ids read in the order they were learned."""
    return [_row(m) for m in s.scalars(_live(plex_id).order_by(Memory.id))]


def _own(s: Session, plex_id: int, memory_id: int) -> Memory | None:
    m = s.get(Memory, memory_id)
    return m if m is not None and m.plex_id == plex_id else None


def save(s: Session, plex_id: int, text: str, kind: str, source: str,
         replaces: int | None = None) -> dict:
    """Save a memory, or replace one of theirs (`replaces`). Plans get a fresh 60 days."""
    if source not in SOURCES:
        raise ValueError(source)
    text = _clean(text, kind)
    now = utcnow()
    expires = now + timedelta(days=PLAN_DAYS) if kind == "plan" else None
    if replaces is not None:
        m = _own(s, plex_id, replaces)
        if m is None:
            raise MemoryNotFound(f"There's no memory {replaces} to replace.")
        m.text, m.kind, m.source, m.updated_at, m.expires_at = text, kind, source, now, expires
    else:
        count = s.scalar(select(func.count()).select_from(_live(plex_id).subquery())) or 0
        if count >= MAX_PER_PERSON:
            raise MemoryError(f"They already have {MAX_PER_PERSON} memories, the most Telly keeps. "
                              "Replace or forget an old one first.")
        m = Memory(plex_id=plex_id, kind=kind, text=text, source=source, created_at=now,
                   updated_at=now, expires_at=expires)
        s.add(m)
    s.flush()
    return _row(m)


def forget(s: Session, plex_id: int, memory_id: int) -> bool:
    m = _own(s, plex_id, memory_id)
    if m is None:
        return False
    s.delete(m)
    s.flush()
    return True


def forget_all(s: Session, plex_id: int) -> int:
    return s.execute(delete(Memory).where(Memory.plex_id == plex_id)).rowcount or 0


def purge_expired(s: Session) -> int:
    """Nightly: plans past their 60 days."""
    return s.execute(delete(Memory).where(Memory.expires_at.is_not(None),
                                          Memory.expires_at <= utcnow())).rowcount or 0
