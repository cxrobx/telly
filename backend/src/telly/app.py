"""FastAPI app: MCP for plexbot, /link, health, and the in-process scheduler."""

from __future__ import annotations

import base64
import hmac
import ipaddress
import logging
import threading
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from sqlalchemy import func, select

from . import arrhook, linking
from .clients.overseerr import OverseerrClient
from .clients.plextv import PlexTV
from .config import get_settings
from .db import session_scope
from .api import router as api_router
from .jobs import run_recent, run_all, run_hot, run_renewals
from .logredact import install as install_log_redaction
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
    install_log_redaction()  # also under a bare `uvicorn telly.app:app`, not only `telly serve`
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
            sched.add_job(run_recent, "cron", minute=5, id="recent", max_instances=1, coalesce=True)
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


# ── Radarr/Sonarr webhooks (docs/spec-arr.md phase 2) ───────────────────────────

# LAN, Docker and the tailnet only. The public site proxies /api/* alone, so this is belt and
# braces: a request that came through Cloudflare carries cf-connecting-ip and is refused too.
_INTERNAL_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "::1/128")]


def _internal(request: Request) -> bool:
    if request.headers.get("cf-connecting-ip") or request.headers.get("cf-ray"):
        return False
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "testclient"  # Starlette's TestClient
    return any(ip in n for n in _INTERNAL_NETS)


def webhook_authorized(header: str | None) -> bool:
    """HTTP Basic, password = TELLY_ARR_WEBHOOK_SECRET (what the *arr Webhook connection sends).
    The username isn't a secret and isn't checked. No secret configured → nobody gets in."""
    secret = get_settings().arr_webhook_secret
    if not secret or not header or not header.lower().startswith("basic "):
        return False
    try:
        _user, _, password = base64.b64decode(header[6:].strip()).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(password.encode(), secret.encode())


_deliver_timer: threading.Timer | None = None
_deliver_lock = threading.Lock()
DELIVER_AFTER = 90.0  # seconds after the last import: a season pack's webhooks become one alert


def schedule_delivery() -> None:
    """Send member alerts shortly after the imports stop (the hourly run is the backstop)."""
    global _deliver_timer
    from .jobs import run_deliver

    def go():
        try:
            run_deliver()
        except Exception as e:  # noqa: BLE001
            log.warning("arr delivery failed: %s", e)

    with _deliver_lock:
        if _deliver_timer is not None:
            _deliver_timer.cancel()
        _deliver_timer = threading.Timer(DELIVER_AFTER, go)
        _deliver_timer.daemon = True
        _deliver_timer.start()


@router.post("/internal/arr-webhook/{app_name}")
async def arr_webhook(app_name: str, request: Request) -> JSONResponse:
    if app_name not in arrhook.APPS:
        return JSONResponse({"error": "unknown app"}, status_code=404)
    if not _internal(request):
        return JSONResponse({"error": "not here"}, status_code=404)
    if not webhook_authorized(request.headers.get("authorization")):
        log.warning("arr webhook: refused an unauthenticated %s call", app_name)
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="telly"'})
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "JSON only"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object only"}, status_code=400)

    def run() -> dict:
        with session_scope() as s:
            overseerr = OverseerrClient() if get_settings().overseerr_api_key else None
            return arrhook.handle(s, app_name, body, overseerr)

    import anyio
    out = await anyio.to_thread.run_sync(run)
    if out.get("member_event"):
        schedule_delivery()
    return JSONResponse(out)


app = create_app()
