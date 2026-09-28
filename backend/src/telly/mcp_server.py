"""Telly's MCP (spec §5). plexbot connects with a per-turn requester token.

Rules this file holds:
- No tool takes a user parameter. The caller is whoever the token says, resolved to a
  plex_id through proven links only (requester.resolve).
- An unlinked caller gets a "link first" answer from every tool, never anyone else's data.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable

import anyio
from sqlalchemy import select
from mcp.server.mcpserver import Context, MCPServer

from . import history, memory, recs, shows, taste
from .clients.tmdb import TMDBClient
from .config import get_settings
from .db import session_scope
from .models import AlertPref, IdentityLink
from .requester import bearer, resolve, verify

mcp = MCPServer(
    name="telly",
    instructions=(
        "Telly knows the caller's own Plex watch history, the shows they follow, and what's "
        "coming up for those shows (new episodes, season premieres, renewals, cancellations). "
        "Every tool answers for the person asking; there is no way to ask about someone else."
    ),
)

UNLINKED = {
    "discord": "Your Discord isn't linked to your Plex yet. Say !link in #plexbot and I'll DM "
               "you a one-time link (takes 30 seconds).",
    "web": "Your Overseerr account isn't connected to a Plex account on this server.",
}


def _as_caller(ctx: Context, fn: Callable) -> str:
    who = verify(bearer((ctx.headers or {}).get("authorization")))
    if who is None:  # the HTTP middleware already refuses these; belt and braces
        return json.dumps({"error": "unauthorized"})
    with session_scope() as s:
        plex_id = resolve(s, who)
        if plex_id is None:
            return json.dumps({"linked": False, "message": UNLINKED[who.surface]})
        return json.dumps(fn(s, plex_id), default=str)


async def _run(ctx: Context, fn: Callable) -> str:
    return await anyio.to_thread.run_sync(_as_caller, ctx, fn)


@mcp.tool()
async def whats_new(ctx: Context, days: int = 14) -> str:
    """What happened recently to the shows the caller follows: new episodes and seasons out,
    renewals, cancellations, premiere dates announced. Use for "anything new?" / "what dropped?"."""
    return await _run(ctx, lambda s, pid: {"events": shows.whats_new(s, pid, days=days)})


@mcp.tool()
async def upcoming(ctx: Context, days: int = 120) -> str:
    """The caller's timeline: dated upcoming episodes and season premieres for followed shows
    within `days`, plus shows renewed with no premiere date yet."""
    return await _run(ctx, lambda s, pid: shows.upcoming(s, pid, days=days))


@mcp.tool()
async def followed_shows(ctx: Context) -> str:
    """Shows the caller follows (from their Plex watchlist, their watch history, or added by
    hand), with status and next episode."""
    return await _run(ctx, lambda s, pid: {"shows": shows.followed_shows(s, pid)})


@mcp.tool()
async def recently_watched(ctx: Context, limit: int = 10) -> str:
    """What the caller has watched on Plex lately (shows and movies), most recent first."""
    return await _run(ctx, lambda s, pid: {"watched": shows.recently_watched(s, pid, limit)})


@mcp.tool()
async def search_shows(ctx: Context, query: str) -> str:
    """Find a TV show on TMDB by name → candidates with tmdb_id. Use before follow_show."""
    return await _run(ctx, lambda s, pid: {"results": shows.search(TMDBClient(), query)})


@mcp.tool()
async def follow_show(ctx: Context, tmdb_id: int) -> str:
    """Follow a show so the caller gets alerts for it. Get the tmdb_id from search_shows."""
    return await _run(ctx, lambda s, pid: shows.follow(s, TMDBClient(), pid, tmdb_id))


@mcp.tool()
async def unfollow_show(ctx: Context, tmdb_id: int) -> str:
    """Stop alerts for a show. Sticky: Telly won't re-follow it from watch history."""
    return await _run(ctx, lambda s, pid: shows.unfollow(s, pid, tmdb_id))


def _alert_settings(s, plex_id: int, phone_alerts: bool | None, discord_dms: bool | None) -> dict:
    pref = s.get(AlertPref, plex_id)
    if pref is None:
        pref = AlertPref(plex_id=plex_id, discord_enabled=True)
        s.add(pref)
    if discord_dms is not None:
        pref.discord_enabled = discord_dms
    if phone_alerts is True and not pref.ntfy_topic:
        pref.ntfy_topic = "telly-" + secrets.token_urlsafe(18)
    elif phone_alerts is False:
        pref.ntfy_topic = None
    s.flush()
    linked = s.scalar(select(IdentityLink.external_id).where(
        IdentityLink.surface == "discord", IdentityLink.plex_id == plex_id)) is not None
    ntfy = get_settings().ntfy_url
    return {
        "discord_dms": pref.discord_enabled,
        "discord_linked": linked,
        "phone_alerts": bool(pref.ntfy_topic),
        "phone_setup": (
            f"Install the ntfy app, tap +, and subscribe to {ntfy}/{pref.ntfy_topic} "
            "(keep this private: anyone with it can read your alerts)."
        ) if pref.ntfy_topic else None,
    }


@mcp.tool()
async def alert_settings(ctx: Context, phone_alerts: bool | None = None,
                         discord_dms: bool | None = None) -> str:
    """Show or change how the caller gets alerts. phone_alerts=true creates a private ntfy
    topic for iPhone/Android push and returns setup steps; false turns it off.
    discord_dms toggles Discord DMs (needs !link). Omit both to just read the settings."""
    return await _run(ctx, lambda s, pid: _alert_settings(s, pid, phone_alerts, discord_dms))


@mcp.tool()
async def recommend(ctx: Context, media: str = "any", limit: int = 8) -> str:
    """What the caller should watch next, from their own Plex history plus what's trending,
    each with a one-line reason. media: "any", "tv", "movie" or "anime" (Japanese animation,
    shows and movies). in_library=true means it's already on the Plex server; otherwise it can
    be requested in Overseerr."""
    media = media if media in ("tv", "movie", "anime") else "any"
    return await _run(ctx, lambda s, pid: {"picks": recs.for_user(s, pid, media, min(limit, 20))})


@mcp.tool()
async def rate_title(ctx: Context, tmdb_id: int, media_type: str, liked: bool) -> str:
    """Record that the caller liked (more like this) or disliked (never suggest it again) a
    title. media_type: "tv" or "movie". Takes effect in the next nightly refresh."""
    if media_type not in ("tv", "movie"):
        return json.dumps({"error": "media_type must be tv or movie"})
    return await _run(ctx, lambda s, pid: recs.give_feedback(s, pid, tmdb_id, media_type,
                                                             1 if liked else -1))


@mcp.tool()
async def record_taste(ctx: Context, title: str, liked: bool = True, year: int | None = None,
                       media_type: str | None = None, loved: bool = False,
                       status: str | None = None) -> str:
    """The caller watched a show or movie (anywhere, not just Plex) and said whether they liked
    it: "I loved X", "Y was mid", "finally saw Z, so good". It goes on their History as Loved it
    (loved=true: "loved", "favourite", "so good"), Liked it, or Not for me (liked=false).
    status, only if they said: watching | finished | dropped ("gave up on it"). Pass the title
    as they wrote it, plus year or media_type (tv/movie) only if they said one. Telly finds it,
    preferring something they've watched, and records it in one call. If the answer is
    `ambiguous`, ask which one they meant, then call record_watched with that tmdb_id. A
    download request is not taste."""
    if media_type not in (None, "tv", "movie"):
        return json.dumps({"error": "media_type must be tv or movie"})
    if status not in (None, *history.STATUSES):
        return json.dumps({"error": "status must be watching, finished or dropped"})

    def run(s, pid):
        tmdb = TMDBClient()
        found = taste.pick_title(tmdb.search_multi(title), taste.known_keys(s, pid), title, year, media_type)
        if found["pick"] is None:
            return {"ambiguous": True, "candidates": found["candidates"][:5]} if found["candidates"] \
                else {"error": f"Nothing on TMDB matches {title!r}."}
        p = found["pick"]
        out = _first_picks(s, pid, taste.record(s, tmdb, pid, p["tmdb_id"], p["media_type"], liked,
                                                loved, status))
        out["year"] = p["year"]
        others = [c for c in found["candidates"] if c is not p][:3]
        if others:
            out["other_matches"] = others  # for "wrong one? I meant the 2003 film"
        return out
    return await _run(ctx, run)


@mcp.tool()
async def search_titles(ctx: Context, query: str) -> str:
    """Find a show OR movie on TMDB by name → candidates with tmdb_id and media_type.
    Use before rate_title when the title could be a movie."""
    def run(s, pid):
        return {"results": [{"tmdb_id": r["id"], "media_type": r["media_type"],
                             "name": r.get("name") or r.get("title"),
                             "year": (r.get("first_air_date") or r.get("release_date") or "")[:4]}
                            for r in TMDBClient().search_multi(query)[:8]]}
    return await _run(ctx, run)


@mcp.tool()
async def record_watched(ctx: Context, tmdb_id: int, media_type: str, liked: bool = True,
                         loved: bool = False, status: str | None = None) -> str:
    """Like record_taste, when you already have the tmdb_id (after record_taste came back
    ambiguous, or to correct its pick). liked=false is Not for me: it counts against things
    like it."""
    if media_type not in ("tv", "movie"):
        return json.dumps({"error": "media_type must be tv or movie"})
    if status not in (None, *history.STATUSES):
        return json.dumps({"error": "status must be watching, finished or dropped"})
    return await _run(ctx, lambda s, pid: _first_picks(
        s, pid, taste.record(s, TMDBClient(), pid, tmdb_id, media_type, liked, loved, status)))


def _first_picks(s, pid: int, out: dict) -> dict:
    """Someone with no picks yet told Telly what they like: build them now, not tonight."""
    if out.get("liked") and recs.needs_warm(s, pid):
        s.commit()  # the build reads in its own session
        recs.warm_in_background(pid)
        out["note"] = "Building their first picks now: on Telly's home page in about a minute."
    return out



# ── Memory (docs/spec-memory.md) ─────────────────────────────────────────────


@mcp.tool()
async def my_memories(ctx: Context) -> str:
    """What the caller has told you about themselves before: preferences, their setup, plans.
    plexbot already puts these in your prompt each turn; call this when they ask "what do you
    remember about me?" or you need the ids fresh."""
    return await _run(ctx, lambda s, pid: {"memories": memory.listing(s, pid)})


@mcp.tool()
async def remember(ctx: Context, text: str, kind: str, replaces: int | None = None,
                   they_asked: bool = False) -> str:
    """Save one lasting fact about the caller, in a short line close to their words (under 200
    characters). kind: "preference" ("prefers subs to dubs for anime"), "setup" ("watches on
    the living-room Apple TV; bedroom TV can't play 4K") or "plan" ("watching Bleach
    canon-only, around episode 40"; plans are dropped after 60 days unless saved again).
    replaces: the id of their memory this updates (e.g. the plan's new episode), instead of
    adding a near-duplicate. they_asked: true when they said "remember…".
    Save only what lasts: not tonight's mood, not a one-off request, not what they watched or
    rated (record_taste does that). Never: contact details, credentials, payment details,
    anything about another person on the server, or anything sensitive (health, religion,
    politics, sexuality). Always tell them in a few words that you saved it."""
    return await _run(ctx, lambda s, pid: _saved(
        lambda: memory.save(s, pid, text, kind, "told" if they_asked else "chat", replaces)))


@mcp.tool()
async def forget(ctx: Context, memory_id: int) -> str:
    """Delete one of the caller's memories by its id ("forget that I…", or a saved fact that's
    no longer true and has no replacement)."""
    return await _run(ctx, lambda s, pid: {"forgotten": memory_id} if memory.forget(s, pid, memory_id)
                      else {"error": f"There's no memory {memory_id}."})


@mcp.tool()
async def forget_everything(ctx: Context, confirm_count: int) -> str:
    """Delete ALL of the caller's memories. First tell them how many there are and ask them to
    confirm; then call this with that number. A count that doesn't match deletes nothing."""
    def run(s, pid):
        n = len(memory.listing(s, pid))
        if confirm_count != n:
            return {"deleted": 0, "they_have": n,
                    "error": f"They have {n} memories, not {confirm_count}. Confirm that number with them."}
        return {"deleted": memory.forget_all(s, pid)}
    return await _run(ctx, run)


def _saved(save: Callable[[], dict]) -> dict:
    try:
        return {"saved": save()}
    except memory.MemoryError as e:
        return {"error": str(e)}
