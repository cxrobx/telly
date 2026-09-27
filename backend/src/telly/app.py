"""FastAPI app: MCP for plexbot, /link, health, and the in-process scheduler."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from sqlalchemy import func, select

from . import linking
from .clients.plextv import PlexTV
from .config import get_settings
from .db import session_scope
from .api import router as api_router
from .jobs import run_all, run_hot, run_renewals
from .mcp_server import mcp
from .models import Event, Follow, Play, Title, User
from .requester import bearer, verify

log = logging.getLogger(__name__)
router = APIRouter()

def _mcp_transport():
    """A fresh streamable-HTTP transport. Its session manager runs once per instance, so
    each app gets its own (tests build many apps; production builds one)."""
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,  # one request per call; claude -p opens a fresh client every turn
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=get_settings().mcp_allowed_hosts),
    )


def create_app() -> FastAPI:
    mcp_app = _mcp_transport()
    session_manager = mcp.session_manager  # the one mcp_app just created

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        sched = None
        if get_settings().scheduler_enabled:
            sched = BackgroundScheduler(timezone="America/New_York")
            sched.add_job(run_all, "cron", hour=4, minute=10, id="nightly", max_instances=1,
                          coalesce=True)
            sched.add_job(run_hot, "cron", minute=25, id="hot", max_instances=1, coalesce=True)
            sched.add_job(run_renewals, "cron", day_of_week="sun", hour=5, minute=40, id="renewals",
                          max_instances=1, coalesce=True)
            sched.start()
        try:
            async with session_manager.run():
                yield
        finally:
            if sched:
                sched.shutdown(wait=False)

    application = FastAPI(title="Telly", lifespan=lifespan)
    application.router.routes.extend(mcp_app.routes)
    application.middleware("http")(mcp_requires_requester_token)
    application.middleware("http")(json_only_writes)
    application.include_router(router)
    application.include_router(api_router)
    return application


async def mcp_requires_requester_token(request: Request, call_next):
    """Refuse /mcp without a valid, unexpired plexbot token: not even the tool list leaks."""
    if request.url.path.rstrip("/") == "/mcp":
        if verify(bearer(request.headers.get("authorization"))) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


async def json_only_writes(request: Request, call_next):
    """Cookie-authed writes must be JSON: a cross-site form can't send that without CORS,
    and SameSite=Lax already keeps the cookie off cross-site POSTs. Belt and braces."""
    if (request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/")
            and "application/json" not in (request.headers.get("content-type") or "")):
        return JSONResponse({"detail": "JSON only"}, status_code=415)
    return await call_next(request)


@router.get("/health")
def health() -> dict:
    with session_scope() as s:
        counts = {name: s.scalar(select(func.count()).select_from(model))
                  for name, model in (("users", User), ("plays", Play), ("titles", Title),
                                      ("follows", Follow), ("events", Event))}
    return {"ok": True, **counts}


# ── /link (spec §4) ───────────────────────────────────────────────────────


class LinkCodeIn(BaseModel):
    discord_name: str = ""


@router.post("/internal/link-codes")
def create_link_code(body: LinkCodeIn, authorization: str | None = Header(default=None)) -> dict:
    """plexbot asks for a /link URL for the Discord user its token vouches for."""
    who = verify(bearer(authorization))
    if who is None or who.surface != "discord":
        raise HTTPException(401, "unauthorized")
    with session_scope() as s:
        return {"url": linking.create_code(s, who.uid, body.discord_name or who.name)}


@router.post("/internal/unlink")
def unlink(authorization: str | None = Header(default=None)) -> dict:
    who = verify(bearer(authorization))
    if who is None or who.surface != "discord":
        raise HTTPException(401, "unauthorized")
    with session_scope() as s:
        return {"unlinked": linking.unlink(s, who.uid)}


class CodeIn(BaseModel):
    code: str


def _link_call(fn, *args):
    try:
        with session_scope() as s:
            return fn(s, *args)
    except linking.LinkError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/api/link/describe")
def link_describe(body: CodeIn) -> dict:
    return _link_call(linking.describe_code, body.code)


@router.post("/api/link/start")
def link_start(body: CodeIn) -> dict:
    fwd = f"{get_settings().public_url}/link?code={body.code}&back=1"
    return _link_call(lambda s, c: linking.start(s, PlexTV(), c, fwd), body.code)


@router.post("/api/link/finish")
def link_finish(body: CodeIn) -> dict:
    return _link_call(lambda s, c: linking.finish(s, PlexTV(), c), body.code)


app = create_app()
