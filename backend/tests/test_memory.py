"""Per-person memory (docs/spec-memory.md), through the real MCP and web API.

Made-up people and made-up memories only: the repo is public.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from telly import db, memory, requester
from telly.config import get_settings
from telly.crypto import COOKIE, make_session
from telly.models import IdentityLink, Memory, User, utcnow
from telly.sync import sync_users

SECRET = "test-shared-secret"
ALICE, BOB, GONE = 111, 222, 333


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TELLY_DATABASE_URL", f"sqlite:///{tmp_path / 'mem.db'}")
    monkeypatch.setenv("TELLY_SHARED_SECRET", SECRET)
    monkeypatch.setenv("TELLY_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("TELLY_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TELLY_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TELLY_LLM_ENABLED", "false")
    monkeypatch.setenv("TELLY_PUBLIC_URL", "https://telly.test")
    monkeypatch.setenv("TELLY_PLEXBOT_AUTH_SECRET", "")
    get_settings.cache_clear()
    db._engine = db._factory = None
    db.Base.metadata.create_all(db.get_engine())
    with db.session_scope() as s:
        s.add_all([User(plex_id=ALICE, pms_account_id=1, username="alice", is_owner=True),
                   User(plex_id=BOB, pms_account_id=BOB, username="bob"),
                   User(plex_id=GONE, pms_account_id=GONE, username="gone", removed_at=utcnow())])
        s.flush()
        s.add_all([IdentityLink(surface="discord", external_id="d-alice", plex_id=ALICE, source="link"),
                   IdentityLink(surface="discord", external_id="d-bob", plex_id=BOB, source="link")])
    yield
    get_settings.cache_clear()
    db._engine = db._factory = None


@pytest.fixture
def client(env):
    from telly.app import create_app
    with TestClient(create_app(), base_url="http://localhost:8940") as c:
        yield c


def call(client, discord_id, tool, **args):
    token = requester.mint("discord", discord_id, secret=SECRET)
    r = client.post("/mcp", headers={"Accept": "application/json, text/event-stream",
                                     "Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}})
    assert r.status_code == 200, r.text
    return json.loads(r.json()["result"]["content"][0]["text"])


def web(client, pid):
    client.cookies.set(COOKIE, make_session(pid))
    return client


# ── through plexbot (MCP) ─────────────────────────────────────────────────


def test_remember_and_recall_are_the_callers_own(client):
    saved = call(client, "d-alice", "remember", text="Prefers subtitles to dubs", kind="preference")
    assert saved["saved"]["text"] == "Prefers subtitles to dubs"
    assert saved["saved"]["source"] == "chat"
    call(client, "d-bob", "remember", text="Watches on a projector", kind="setup", they_asked=True)

    alice = call(client, "d-alice", "my_memories")["memories"]
    bob = call(client, "d-bob", "my_memories")["memories"]
    assert [m["text"] for m in alice] == ["Prefers subtitles to dubs"]
    assert [(m["text"], m["source"]) for m in bob] == [("Watches on a projector", "told")]


def test_nobody_can_touch_another_persons_memory(client):
    mid = call(client, "d-alice", "remember", text="Likes slow burns", kind="preference")["saved"]["id"]
    assert "error" in call(client, "d-bob", "forget", memory_id=mid)
    assert "error" in call(client, "d-bob", "remember", text="hijacked", kind="preference", replaces=mid)
    assert [m["text"] for m in call(client, "d-alice", "my_memories")["memories"]] == ["Likes slow burns"]


def test_unlinked_callers_get_no_memory(client):
    out = call(client, "d-stranger", "remember", text="x likes y", kind="preference")
    assert out["linked"] is False
    with db.session_scope() as s:
        assert s.scalar(select(Memory)) is None


def test_replace_updates_in_place(client):
    mid = call(client, "d-alice", "remember", text="Watching Show X, around episode 40",
               kind="plan")["saved"]["id"]
    call(client, "d-alice", "remember", text="Watching Show X, around episode 80", kind="plan", replaces=mid)
    assert [(m["id"], m["text"]) for m in call(client, "d-alice", "my_memories")["memories"]] == [
        (mid, "Watching Show X, around episode 80")]


def test_forget_everything_needs_the_right_count(client):
    for i in range(3):
        call(client, "d-alice", "remember", text=f"Preference number {i}", kind="preference")
    wrong = call(client, "d-alice", "forget_everything", confirm_count=5)
    assert wrong["deleted"] == 0 and wrong["they_have"] == 3
    assert call(client, "d-alice", "forget_everything", confirm_count=3)["deleted"] == 3
    assert call(client, "d-alice", "my_memories")["memories"] == []


@pytest.mark.parametrize("text", [
    "email me at someone@example.com",
    "call 555-123-4567 after 6",
    "my plex password is hunter2",
    "key abcdefghijklmnopqrstuvwxyz0123456789ABCD",
    "lives at 12 Oak Street",
])
def test_never_saved(client, text):
    assert "doesn't save" in call(client, "d-alice", "remember", text=text, kind="setup")["error"]


def test_limits(client):
    assert "under 200" in call(client, "d-alice", "remember", text="x" * 201, kind="setup")["error"]
    assert "kind must be" in call(client, "d-alice", "remember", text="ok", kind="mood")["error"]
    with db.session_scope() as s:
        for i in range(memory.MAX_PER_PERSON):
            memory.save(s, ALICE, f"fact {i}", "preference", "web")
    assert "Replace or forget" in call(client, "d-alice", "remember", text="one more", kind="preference")["error"]


# ── plans expire, removed members lose theirs ─────────────────────────────


def test_plans_expire_after_60_days_and_others_stay(env):
    with db.session_scope() as s:
        plan = memory.save(s, ALICE, "Rewatching Show Y", "plan", "chat")
        memory.save(s, ALICE, "Prefers subs", "preference", "chat")
        assert plan["expires"] == (utcnow() + timedelta(days=60)).date().isoformat()
        s.get(Memory, plan["id"]).expires_at = utcnow() - timedelta(minutes=1)
        s.flush()
        assert [m["text"] for m in memory.listing(s, ALICE)] == ["Prefers subs"]  # hidden at once
        assert memory.purge_expired(s) == 1
        assert s.scalar(select(Memory).where(Memory.id == plan["id"])) is None


def test_removing_a_member_deletes_their_memories(env):
    with db.session_scope() as s:
        memory.save(s, BOB, "Watches on a projector", "setup", "chat")
        memory.save(s, ALICE, "Prefers subs", "preference", "chat")

    class Plex:
        def owner_plex_id(self):
            return ALICE

        def accounts(self):
            return [{"id": 1, "name": "alice"}]  # bob is gone from the server

    from telly.sync import SyncReport
    with db.session_scope() as s:
        sync_users(s, Plex(), SyncReport())
    with db.session_scope() as s:
        assert [m.plex_id for m in s.scalars(select(Memory))] == [ALICE]


# ── on the web (Your taste page) ─────────────────────────────────────────


def test_web_lists_adds_edits_deletes_only_your_own(client):
    c = web(client, ALICE)
    mid = c.post("/api/memories", json={"text": "Prefers subs", "kind": "preference"}).json()["memory"]["id"]
    assert c.post(f"/api/memories/{mid}/edit", json={"text": "Prefers subs, always",
                                                     "kind": "preference"}).status_code == 200
    got = c.get("/api/memories").json()
    assert [m["text"] for m in got["memories"]] == ["Prefers subs, always"] and got["max"] == 30

    b = web(client, BOB)
    assert b.get("/api/memories").json()["memories"] == []
    assert b.post(f"/api/memories/{mid}/delete", json={}).status_code == 404
    assert b.post(f"/api/memories/{mid}/edit", json={"text": "mine now", "kind": "setup"}).status_code == 404
    assert b.post("/api/memories/clear", json={}).json()["deleted"] == 0

    a = web(client, ALICE)
    assert a.post(f"/api/memories/{mid}/delete", json={}).status_code == 200
    assert a.get("/api/memories").json()["memories"] == []


def test_web_needs_a_session_and_rejects_bad_input(client):
    assert client.get("/api/memories").status_code == 401
    c = web(client, ALICE)
    assert c.post("/api/memories", json={"text": "call 555-123-4567", "kind": "setup"}).status_code == 400
    assert c.post("/api/memories", json={"text": "ok", "kind": "mood"}).status_code == 422
