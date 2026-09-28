"""The web API behind the UI. Cookie sessions from Sign in with Plex (spec §3).

- Every route reads the caller from the session cookie; none takes a user id.
- The Plex PIN being polled lives in a signed HttpOnly cookie set by /auth/start, never in
  the request body. Otherwise anyone could poll guessed PIN ids through /auth/finish and
  catch someone else's sign-in, because every PIN shares Telly's client id.
- State-changing requests must be JSON: with SameSite=Lax cookies, that closes CSRF.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import func, select

from . import memory, plexbot, recs, shows, taste
from .clients.overseerr import OverseerrClient
from .clients.plextv import PlexTV
from .clients.tmdb import TMDBClient
from .config import get_settings
from .crypto import COOKIE, encrypt_token, make_session, read_session
from .db import session_scope
from .mcp_server import _alert_settings
from .models import AlertPref, IdentityLink, Play, User, utcnow

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

PIN_COOKIE = "telly_pin"
PIN_MAX_AGE = 15 * 60


def _pin_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="telly-pin")


def _set_cookie(resp: Response, name: str, value: str, max_age: int) -> None:
    resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax",
                    secure=get_settings().cookie_secure, path="/")


def current_user(request: Request) -> int:
    pid = read_session(request.cookies.get(COOKIE))
    if pid is None:
        raise HTTPException(401, "sign in")
    with session_scope() as s:
        u = s.get(User, pid)
        if u is None or u.removed_at is not None:
            raise HTTPException(401, "sign in")
    return pid


Me = Depends(current_user)


# ── Sign in with Plex ─────────────────────────────────────────────────────


@router.post("/auth/start")
def auth_start(response: Response) -> dict:
    plextv = PlexTV()
    pin = plextv.create_pin()
    _set_cookie(response, PIN_COOKIE, _pin_signer().dumps(pin["id"]), PIN_MAX_AGE)
    fwd = f"{get_settings().public_url}/signin?back=1"
    return {"auth_url": plextv.auth_url(pin["code"], fwd)}


@router.post("/auth/finish")
def auth_finish(request: Request, response: Response, background: BackgroundTasks) -> dict:
    try:
        pin_id = _pin_signer().loads(request.cookies.get(PIN_COOKIE) or "", max_age=PIN_MAX_AGE)
    except BadSignature:
        raise HTTPException(400, "Sign-in expired. Start again.") from None
    plextv = PlexTV()
    token = plextv.pin_token(int(pin_id))
    if token is None:
        return {"done": False}
    who = plextv.user(token)
    with session_scope() as s:
        u = s.get(User, who["id"])
        if u is None or u.removed_at is not None:
            response.delete_cookie(PIN_COOKIE, path="/")
            raise HTTPException(403, "That Plex account isn't a member of this Plex server.")
        u.plex_token_enc = encrypt_token(token)
        u.signed_in_at = utcnow()
    background.add_task(recs.warm, who["id"])  # a new member sees something before tonight
    response.delete_cookie(PIN_COOKIE, path="/")
    _set_cookie(response, COOKIE, make_session(who["id"]), get_settings().session_days * 86400)
    return {"done": True, "username": who["username"]}


@router.post("/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/dev")
def dev_login() -> Response:
    """Local UI work only. Disabled unless TELLY_DEV_LOGIN_PLEX_ID is set (never on the NAS)."""
    pid = get_settings().dev_login_plex_id
    if not pid:
        raise HTTPException(404)
    resp = RedirectResponse("/", status_code=303)
    _set_cookie(resp, COOKIE, make_session(pid), 86400)
    return resp


# ── Me / settings ─────────────────────────────────────────────────────────


@router.get("/me")
def me(pid: int = Me) -> dict:
    with session_scope() as s:
        u = s.get(User, pid)
        pref = s.get(AlertPref, pid)
        linked = s.scalar(select(IdentityLink.display_name).where(
            IdentityLink.surface == "discord", IdentityLink.plex_id == pid))
        return {
            "plex_id": pid, "username": u.username, "is_owner": u.is_owner,
            "watchlist_connected": u.plex_token_enc is not None,
            "can_request": u.overseerr_id is not None,
            "discord_linked": linked is not None, "discord_name": linked,
            "discord_dms": pref.discord_enabled if pref else True,
            "phone_topic": pref.ntfy_topic if pref else None,
            "ntfy_url": get_settings().ntfy_url,
            "following": len(shows.followed_shows(s, pid)),
        }


class SettingsIn(BaseModel):
    phone_alerts: bool | None = None
    discord_dms: bool | None = None


@router.post("/settings")
def settings(body: SettingsIn, pid: int = Me) -> dict:
    with session_scope() as s:
        return _alert_settings(s, pid, body.phone_alerts, body.discord_dms)


@router.post("/plex/disconnect")
def disconnect(pid: int = Me) -> dict:
    with session_scope() as s:
        s.get(User, pid).plex_token_enc = None
    return {"ok": True}


# ── Content ───────────────────────────────────────────────────────────────


@router.get("/home")
def home(pid: int = Me) -> dict:
    with session_scope() as s:
        timeline = shows.upcoming(s, pid, days=30)
        news = shows.whats_new(s, pid, days=30)[:12]
        picks = recs.for_user(s, pid, limit=recs.HOME_PICKS)
        hero = (timeline["dated"][0] if timeline["dated"] else None)
        u = s.get(User, pid)
        return {"hero": hero, "airing_soon": timeline["dated"][:10], "whats_new": news,
                "for_you": picks, "trending": recs.trending_for_user(s, pid),
                # nothing to base picks on yet: the home page asks them what they like
                "needs_taste": u.taste_prompt_dismissed_at is None and not recs.seeds_for(s, pid),
                "building": recs.warming(pid)}


@router.post("/home/taste-prompt/dismiss")
def dismiss_taste_prompt(pid: int = Me) -> dict:
    with session_scope() as s:
        s.get(User, pid).taste_prompt_dismissed_at = utcnow()
    return {"ok": True}


@router.get("/timeline")
def timeline(days: int = 180, pid: int = Me) -> dict:
    with session_scope() as s:
        return shows.upcoming(s, pid, days=min(max(days, 7), 400))


@router.get("/whats-new")
def whats_new(days: int = 60, pid: int = Me) -> dict:
    with session_scope() as s:
        return {"events": shows.whats_new(s, pid, days=min(days, 365))}


@router.get("/following")
def following(pid: int = Me) -> dict:
    with session_scope() as s:
        return {"shows": shows.followed_shows(s, pid)}


@router.get("/recs")
def recommendations(media: Literal["any", "tv", "movie", "anime"] = "any", pid: int = Me) -> dict:
    with session_scope() as s:
        return {"picks": recs.for_user(s, pid, media=media, limit=40)}


class FeedbackIn(BaseModel):
    tmdb_id: int
    media_type: Literal["tv", "movie"]
    value: Literal[-1, 0, 1]


@router.post("/recs/feedback")
def feedback(body: FeedbackIn, pid: int = Me) -> dict:
    with session_scope() as s:
        return recs.give_feedback(s, pid, body.tmdb_id, body.media_type, body.value)


@router.get("/shows/{tmdb_id}")
def show(tmdb_id: int, pid: int = Me) -> dict:
    with session_scope() as s:
        if shows.ensure_title(s, TMDBClient(), tmdb_id) is None:
            raise HTTPException(404, "No such show")
        return shows.show_detail(s, pid, tmdb_id)


@router.post("/shows/{tmdb_id}/follow")
def follow(tmdb_id: int, pid: int = Me) -> dict:
    with session_scope() as s:
        return shows.follow(s, TMDBClient(), pid, tmdb_id)


@router.post("/shows/{tmdb_id}/unfollow")
def unfollow(tmdb_id: int, pid: int = Me) -> dict:
    with session_scope() as s:
        return shows.unfollow(s, pid, tmdb_id)


@router.get("/search")
def search(q: str, pid: int = Me) -> dict:
    if len(q.strip()) < 2:
        return {"results": []}
    results = shows.search(TMDBClient(), q.strip(), limit=10)
    with session_scope() as s:
        mine = shows._following(s, pid)
    for r in results:
        r["following"] = r["tmdb_id"] in mine
    return {"results": results}


class RequestIn(BaseModel):
    tmdb_id: int
    media_type: Literal["tv", "movie"]


@router.post("/request")
def request_title(body: RequestIn, pid: int = Me) -> dict:
    with session_scope() as s:
        oid = s.get(User, pid).overseerr_id
    if oid is None:
        raise HTTPException(400, "You don't have an Overseerr account yet. Ask Chris for one.")
    try:
        return OverseerrClient().request(oid, body.media_type, body.tmdb_id)
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e


@router.get("/members")
def members(pid: int = Me) -> dict:
    """Owner only; who uses Telly and how they're connected. No viewing data (spec §7)."""
    with session_scope() as s:
        if not s.get(User, pid).is_owner:
            raise HTTPException(403)
        links = {l.plex_id for l in s.scalars(select(IdentityLink).where(IdentityLink.surface == "discord"))}
        prefs = {p.plex_id: p for p in s.scalars(select(AlertPref))}
        return {"members": [{
            "username": u.username, "signed_in": u.signed_in_at is not None,
            "watchlist_connected": u.plex_token_enc is not None,
            "discord_linked": u.plex_id in links,
            "phone_alerts": bool(prefs.get(u.plex_id) and prefs[u.plex_id].ntfy_topic),
            "removed": u.removed_at is not None,
        } for u in s.scalars(select(User).order_by(User.username))]}


# ── Taste beyond Plex (taste.py) ──────────────────────────────────────────


@router.get("/taste")
def taste_summary(pid: int = Me) -> dict:
    with session_scope() as s:
        plex_titles = s.scalar(select(func.count(func.distinct(Play.tmdb_id))).where(
            Play.plex_id == pid, Play.tmdb_id.is_not(None))) or 0
        return {"plex_titles": plex_titles, **taste.summary(s, pid)}


class ImdbIn(BaseModel):
    csv: str


MAX_CSV = 5_000_000


def _resolve_imdb_now(pid: int) -> None:
    try:
        with session_scope() as s:
            taste.resolve_imdb(s, TMDBClient())
    except Exception as e:  # noqa: BLE001 — the nightly run picks up anything left pending
        log.warning("imdb resolve failed: %s", e)
    recs.warm(pid)  # ratings only count once matched to TMDB


@router.post("/taste/imdb")
def import_imdb(body: ImdbIn, background: BackgroundTasks, pid: int = Me) -> dict:
    """An IMDb CSV export, sent as text inside JSON (so writes stay JSON-only)."""
    if len(body.csv) > MAX_CSV:
        raise HTTPException(413, "That file is too big for an IMDb export.")
    try:
        kind, rows = taste.parse_imdb_csv(body.csv)
    except taste.ImdbImportError as e:
        raise HTTPException(400, str(e)) from e
    with session_scope() as s:
        result = taste.store_imdb(s, pid, kind, rows)
    background.add_task(_resolve_imdb_now, pid)
    return result


@router.get("/taste/items")
def taste_items(pid: int = Me) -> dict:
    with session_scope() as s:
        return {"items": taste.listing(s, pid)}


class UseIn(BaseModel):
    tmdb_id: int
    media_type: Literal["tv", "movie"]
    use: bool


@router.post("/taste/use")
def taste_use(body: UseIn, pid: int = Me) -> dict:
    """Keep a title in the caller's taste, or not ("doesn't match what I like right now")."""
    with session_scope() as s:
        n = taste.set_use(s, pid, body.tmdb_id, body.media_type, body.use)
    if not n:
        raise HTTPException(404, "That title isn't in your taste.")
    return {"ok": True, "use": body.use}


class ToldIn(BaseModel):
    tmdb_id: int
    media_type: Literal["tv", "movie"]
    liked: bool = True


@router.post("/taste/told")
def taste_told(body: ToldIn, background: BackgroundTasks, pid: int = Me) -> dict:
    """The caller says they watched something (anywhere) and liked it, or didn't."""
    with session_scope() as s:
        name, poster = taste._details(s, TMDBClient(), body.tmdb_id, body.media_type)
        if not name:
            raise HTTPException(404, "No such title on TMDB.")
        if body.liked:
            taste.add(s, pid, body.tmdb_id, body.media_type, "told", name, poster_path=poster)
        else:
            recs.give_feedback(s, pid, body.tmdb_id, body.media_type, -1)
    if body.liked:
        background.add_task(recs.warm, pid)  # runs after the commit above
    return {"ok": True, "title": name, "liked": body.liked}


# ── Memory (docs/spec-memory.md): the caller's own, and nobody else's ──────────


@router.get("/memories")
def memories(pid: int = Me) -> dict:
    with session_scope() as s:
        return {"memories": memory.listing(s, pid), "max": memory.MAX_PER_PERSON,
                "max_len": memory.MAX_LEN}


class MemoryIn(BaseModel):
    text: str
    kind: Literal["preference", "setup", "plan"]


def _save_memory(pid: int, body: MemoryIn, replaces: int | None = None) -> dict:
    try:
        with session_scope() as s:
            return {"memory": memory.save(s, pid, body.text, body.kind, "web", replaces)}
    except memory.MemoryNotFound as e:  # someone else's id reads as missing, like a bad one
        raise HTTPException(404, str(e)) from None
    except memory.MemoryError as e:
        raise HTTPException(400, str(e)) from None


@router.post("/memories")
def add_memory(body: MemoryIn, pid: int = Me) -> dict:
    return _save_memory(pid, body)


@router.post("/memories/{memory_id}/edit")
def edit_memory(memory_id: int, body: MemoryIn, pid: int = Me) -> dict:
    return _save_memory(pid, body, replaces=memory_id)


@router.post("/memories/{memory_id}/delete")
def delete_memory(memory_id: int, pid: int = Me) -> dict:
    with session_scope() as s:
        if not memory.forget(s, pid, memory_id):
            raise HTTPException(404, "No such memory.")
    return {"ok": True}


@router.post("/memories/clear")
def clear_memories(pid: int = Me) -> dict:
    with session_scope() as s:
        return {"deleted": memory.forget_all(s, pid)}


@router.get("/search/titles")
def search_titles(q: str, pid: int = Me) -> dict:
    if len(q.strip()) < 2:
        return {"results": []}
    return {"results": [{"tmdb_id": r["id"], "media_type": r["media_type"], "name": r.get("name") or r.get("title"),
                         "year": (r.get("first_air_date") or r.get("release_date") or "")[:4],
                         "poster_path": r.get("poster_path")}
                        for r in TMDBClient().search_multi(q.strip())[:10]]}


# ── plexbot's web chat, inside Telly (plexbot.py) ─────────────────────────


@router.get("/plexbot-token")
def plexbot_token(pid: int = Me) -> dict:
    """A plexbot chat token for the signed-in member, as their Overseerr account. Members
    without one get a 404 and no chat button: plexbot's web identity is the Overseerr user."""
    cfg = get_settings()
    if not cfg.plexbot_auth_secret:
        raise HTTPException(503, "Chat isn't set up.")
    with session_scope() as s:
        u = s.get(User, pid)
        oid, name = u.overseerr_id, u.username
    if oid is None:
        raise HTTPException(404, "No Overseerr account, so no plexbot chat.")
    return {"token": plexbot.mint(oid, name), "id": str(oid), "name": name,
            "chat_url": cfg.plexbot_chat_url, "expires_in": plexbot.TTL}

