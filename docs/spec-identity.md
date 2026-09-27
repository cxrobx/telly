# Telly — identity and access spec

Status: **approved by Chris 2026-09-27**, with the recommendation taken on all three open questions (§9).

Telly is multi-user: everyone on the Plex server gets their own history, followed shows,
alerts and recommendations. This spec answers one question: **when Telly answers or alerts,
how does it know whose data it's working with, and why can't someone get another person's data?**

## 1. Who is a user

A Telly user is a **plex.tv account id** (`plex_id`). Every row Telly stores about a person
hangs off it. Nothing else counts as identity: not a display name, a Plex username, or an email address.

**Admission:** a plex.tv account can use Telly only if it is the server owner or appears in the
server's account list (`GET /accounts` on PMS). Anyone else who signs in with Plex gets "this
Telly is for members of Chris's Plex server" and nothing is stored.

## 2. What was checked against the live systems (2026-09-27)

| Fact | Result |
|---|---|
| PMS history has a per-play `accountID` | Yes. 799 plays since 2025-02-26, 4 active accounts |
| `accountID` equals the plex.tv id | **Yes for everyone except the owner.** The owner shows up as `1` in PMS but has plex.tv id `495743363` |
| Show → TMDB mapping | Yes. The show's `Guid` list has `tmdb://…` (`includeGuids=1`) |
| Overseerr users carry `plexId` | Yes, all 6. They match the PMS ids, and the owner is `495743363` |
| Overseerr users carry `discordId` | **No, all empty.** So Discord ↔ Plex can't come from Overseerr (see §4) |
| Server members with no Overseerr account | 2, plus one blank-named account (probably a managed Home user) |

So the rule is: **PMS `accountID == 1` → the owner's plex.tv id (resolved once from the admin
token via `plex.tv/api/v2/user`); any other `accountID` is already the plex.tv id.**

## 3. Signing in to the web UI

Sign in with Plex, using Plex's PIN flow (the one Overseerr uses):

1. Telly creates a PIN (`POST plex.tv/api/v2/pins`, strong) and sends the browser to `app.plex.tv/auth#?…`.
2. Telly polls the PIN until it returns the user's `authToken`, then calls `GET plex.tv/api/v2/user` → `plex_id`.
3. Admission check (§1). If it passes, Telly sets a signed, `HttpOnly`, `Secure`, `SameSite=Lax` session cookie (30 days) that carries only `plex_id`.

*Added during the build:* the PIN being polled is kept in a signed `HttpOnly` cookie set by `/api/auth/start`, and **never taken from the request**. Every PIN shares Telly's client id, so a finish endpoint that accepted a PIN id could be used to poll guessed ids and catch someone else's sign-in in progress. Cookie-authed writes must also be `application/json` (415 otherwise), which blocks cross-site form posts on top of `SameSite=Lax`.

**The user's Plex token.** The token is the only way to read a person's *own* Plex watchlist (it
lives on plex.tv, not on the server). Telly keeps it **encrypted at rest** (Fernet, with the key
from env and never in the DB), uses it for exactly one call (`GET` watchlist on
`discover.provider.plex.tv`), and deletes it on "Disconnect Plex" in settings. The user can also
revoke it from their own Plex account's authorized-devices page. If the token dies, their
watchlist freezes at its last value and alerts keep working from history (§6).

## 4. Knowing who's asking in plexbot

plexbot already knows its requester and passes it to its MCP tools as env vars
(`PLEX_AGENT_REQUESTER_SURFACE` + `PLEX_AGENT_REQUESTER_USER_ID`, set in `bot.py:_subprocess_env`).
Both are already trusted: on Discord, discord.py hands over the authenticated sender; on the
web, `web.py` sets them only after HMAC-verifying the Cloudflare Worker's token.

| Surface | Requester id | → `plex_id` |
|---|---|---|
| `web` (Overseerr widget) | Overseerr user id | Overseerr API `GET /user/{id}` → `plexId`. Verified for all 6 users |
| `discord` | Discord user id | Telly's own `discord_links` table, filled only by `/link` (below) |

**`/link` (Discord → Plex):** the user types `/link` in Discord. plexbot asks Telly for a one-time
code bound to that Discord id (10-minute TTL, single use) and **DMs** them the URL
`telly…/link?code=…`. Opening it forces Sign in with Plex, and Telly stores `discord_id ↔ plex_id`.
Each side is proven by its own login: Discord by discord.py, Plex by OAuth. **Telly never accepts a
typed username or id as proof.** Re-linking replaces the old row, and `/unlink` deletes it.

## 5. How plexbot talks to Telly's MCP

Telly serves MCP over HTTP at `/mcp`. It doesn't trust one static key plus a "who I am" header.
Instead **plexbot mints a token for each chat turn:**

- Before each `claude -p` call, plexbot puts
  `TELLY_REQUESTER_TOKEN = base64(surface|requester_id|exp) + "." + HMAC-SHA256(TELLY_SHARED_SECRET, …)`
  in the subprocess env. `exp` is 15 minutes out.
- plexbot's `mcp.json` gets a `telly` server:
  `{"type": "http", "url": "http://telly-api:…/mcp", "headers": {"Authorization": "Bearer ${TELLY_REQUESTER_TOKEN}"}}`.
- Telly checks the HMAC and `exp`, resolves `surface|requester_id` to a `plex_id` (§4), and runs the tool **scoped to that `plex_id`**.

**What this rules out:**
- **No tool takes a user parameter.** The model can't ask for "Alexis's recommendations", because nothing in the tool schema names a user. The header is set by plexbot's process, not by the model.
- A leaked token is good for one person for 15 minutes, and it's read-only.
- If the requester has no mapping, every tool returns a message telling them to link their Plex account first (`/link`, or signing in to Telly). They never get someone else's data, and they never get the owner's as a fallback.

**Verified 2026-09-27 (Claude Code 2.1.283):** `claude -p --mcp-config cfg.json` expands `${VAR}`
in `headers`. A local listener received `Authorization: Bearer probe-abc123` from
`TELLY_REQUESTER_TOKEN=probe-abc123`. (`--mcp-config` is variadic, so the prompt has to come
before it, which plexbot's `_build_cmd` already does. That same argv also has
`--allowedTools mcp__plex-tools__*`, which will need `mcp__telly__*` added, or the tools load but
never get called.) If a future version stops expanding it,
the fallback is a thin stdio shim inside plexbot that adds the header itself. The trust model
doesn't change.

**v1 tools (all read-only, all scoped to the caller):** `whats_new`, `upcoming`, `recommend`,
`followed_shows`, `why(title)`. The only writes are follow/unfollow and 👍/👎 on a
recommendation, and those also apply only to the caller. **No cross-user tools, not even for the
admin.** Cross-user tools ("what's everyone watching") would be a separate decision later.

## 6. Alerts

- **Discord DM** only to a Discord id linked through `/link`. *Build decision (2026-09-27):* Telly sends it itself, as the plexbot bot, through Discord's REST API with the bot token, so people get it from the bot they already talk to. The alternative was an internal "send DM" endpoint on plexbot, but plexbot's web server is public (`plexbot.chrisx.art`). Telly never DMs a Discord id it hasn't linked.
- **ntfy:** Telly generates a random, unguessable topic for each user (24+ chars) and shows it once in settings with a "subscribe" QR code. On public `ntfy.sh` anyone who knows the topic can read it, so the alert text is kept to show and episode only ("Severance S3 renewed").
- **Once per user per event:** each send is keyed on `(plex_id, event_id, channel)` with a unique constraint, so a rerun or a status that flips back never double-pings.
- **Followed shows = watchlist ∪ inferred.** "Inferred" means someone has watched **≥ 3 distinct episodes, and either ≥ 50% of what has aired or anything from the latest aired season.** *Changed during the build (2026-09-27):* ended and canceled shows are **included**, because a revival is exactly the alert people want, and a dead show almost never produces events otherwise. That keeps alerts working for people who never sign in. Unfollow always wins over inferred: inference only ever adds rows, never changes an existing one.

## 7. Privacy between users

Each person sees only their own history, follows, alerts and recommendations, in the web UI and
through plexbot. Telly's pages expose nothing across users. Chris, as the admin, gets a members
page with **no viewing data** on it: just who has signed in and whether each person's Discord and
ntfy are linked.

## 8. Abuse and failure cases

| Case | What happens |
|---|---|
| A stranger signs in with Plex | Admission refuses, nothing is stored |
| A user is removed from the Plex server | The nightly job drops them from `/accounts`: sessions invalidated, Plex token deleted, no more alerts |
| The model tries a tool call "for" another user | Impossible, since no tool has a user parameter |
| A Discord user without `/link` asks plexbot | "Link your Plex first: `/link`" |
| Someone else's `/link` code gets forwarded to you | Whoever completes the Plex sign-in is the account linked to *the Discord id the code was minted for*. So a forwarded code links the sender's Discord to *your* Plex. Mitigations: codes last 10 min and are DM-only, and the link page shows the Discord username and asks "Link Discord user X to your Plex?" |
| `TELLY_SHARED_SECRET` leaks | Anyone could mint requests as any user. Rotate it in both `.env`s. Telly is only on the Docker network (see open question 1) |

## 9. Decisions (approved 2026-09-27)

1. **Web UI is public** on a `chrisx.art` hostname through `nas-tunnel`, gated by Plex sign-in plus admission. No Cloudflare Access. A Cloudflare rate limit goes on `/auth/*` and `/link`. **`/mcp` is never routed through the tunnel**: it's reachable only on the Docker network.
2. **Friends' Plex tokens are stored**, encrypted at rest as described in §3, so watchlists stay current between visits.
| Server members with no Overseerr account | 2, plus one blank-named account (probably a managed Home user) |
