# Telly

What-to-watch for everyone on Chris's Plex: alerts when followed shows get a new episode,
a new season date, or a renewal; a timeline of what's coming; recommendations from Plex
history + what's trending; and an MCP that plexbot (`~/Projects/plex-agent`) connects to.

**Live at https://telly.chrisx.art** (NAS, since 2026-09-27). plexbot uses its MCP in #plexbot
and in the Overseerr widget.

**The repo is PUBLIC** (github.com/cxrobx/telly, since 2026-09-27, one squashed commit; the earlier private history is `~/Archives/telly-private-history-2026-09-27.bundle`). Never commit a member's username, name, Plex or Discord id, or anything from the prod DB: tests use made-up ids. Scan before pushing: `gitleaks git . --redact`.

**Read `docs/spec-identity.md` before touching auth, users, the MCP, or alerts.** It's the
approved identity/access spec: identity is the plex.tv account id, and no MCP tool or web
route takes a user parameter.

## Try it

`scripts/ask "what's coming up for me?"` asks Telly as Chris (Overseerr id 1), the same path
plexbot uses: a minted requester token, then `claude -p` with Telly's MCP. It starts a local
Telly on :8940 (scheduler off) if one isn't running. `TELLY_AS=discord:<id>` tests another identity.

## Backend (`backend/`)

Python 3.12 + uv, FastAPI, SQLAlchemy 2 on **SQLite (WAL)**, Alembic, APScheduler in-process.
SQLite on purpose: a handful of users, one writer, no extra container on the NAS.

```bash
cd backend
uv run pytest -q                 # 129 tests: detection, sync/poll, MCP scoping + /link + alerts, web auth/scoping, recs, renewals, ratings, taste, migrations
uv run telly migrate             # alembic upgrade head
uv run telly run                 # sync → plexdata → poll → infer → deliver → recs (the nightly job, 04:10 ET)
uv run telly poll --hot          # only shows airing yesterday/today/tomorrow (hourly, then deliver)
uv run telly plexdata            # library, watchlists, Overseerr ids
uv run telly recs                # rebuild recommendations (~12 s/person with Haiku; only members with an Overseerr account)
uv run telly renewals            # news renewal check (Haiku + web search; weekly, Sun 05:40 ET)
uv run telly ratings             # refresh IMDb / RT scores (nightly, after recs)
uv run telly taste-add <plex_id> <tv|movie> <tmdb_id> --source told|mentioned   # record taste by hand
uv run telly deliver | events    # send pending alerts | list recent events
uv run telly serve               # API + scheduler on :8940 (/health)
```

Local secrets are in `backend/.env` (never read it; check by name), from the Keychain:
`secret inject TELLY_PLEX_TOKEN=PLEX_TOKEN TELLY_TMDB_TOKEN=TMDB_READ_TOKEN TELLY_SHARED_SECRET TELLY_OVERSEERR_API_KEY=OVERSEERR_API_KEY TELLY_SESSION_SECRET TELLY_TOKEN_KEY --into backend/.env`.
`TELLY_DISCORD_BOT_TOKEN` is **deliberately absent locally**, so no local run can DM a real
person. For UI work, `TELLY_DEV_LOGIN_PLEX_ID=495743363` enables `/api/auth/dev`. It stays off
unless set (a test checks it); it's never set on the NAS.

| File | What it does |
|---|---|
| `detect.py` | **Pure** change detection. Facts must hold for `confirm_polls` (2) polls before promotion (except `last_ep`, which confirms at once). A show's first poll is a baseline with no events. Events carry a `dedupe_key`, so a renewal reported through both the status and a new season row is stored once |
| `sync.py` | PMS `/accounts` → users (PMS id 1 = owner → plex.tv id), history → plays (idempotent on `historyKey`), inferred follows (only ever inserts) |
| `poll.py` | TMDB `/tv/{id}` → snapshot on `titles` + detection + events (`INSERT … ON CONFLICT DO NOTHING`) |
| `requester.py` | Verifies plexbot's per-turn token and resolves it to a plex_id. web → Overseerr `plexId` (cached 24h); discord → `/link` rows only. **Never falls back to anyone** |
| `mcp_server.py` | `MCPServer` (mcp SDK **2.x**; FastMCP was renamed). Tools read the caller from `ctx.headers`, and **no tool has a user parameter** (a test enforces this) |
| `api.py` | Web API (`/api/*`). Session cookie from Sign in with Plex. The PIN lives in a signed cookie, never in the request (spec §3). Writes must be JSON |
| `linking.py` | `/link`: plexbot gets a code for the Discord id its token vouches for, and the person signs in with Plex (PIN flow) on the web app's `/link` page |
| `plexdata.py` | Library mirror, watchlists → follows (`source=watchlist`, removed when it leaves the watchlist; unfollow stays sticky), Overseerr ids for Request |
| `recs.py` | Seeds (history decayed by recency, and thumbs-up) → TMDB recs and trending → exclusions → Haiku re-rank with reasons. Falls back to templated reasons. **The nightly run skips members with no Overseerr account** (Chris, 2026-09-27: they can't request or use plexbot's chat, so it's wasted compute); their existing picks stay |
| `renewals.py` | Weekly news check for shows TMDB lags on. Accepts only the next season, from a trusted outlet, confirmed by fetching the page. Old announcements (>30 d, URL month wins) are `quiet`: timeline only, no alert, not in What's new |
| `alerts.py` | Fan-out, at most once: the Delivery row is committed as `sending` before the send, so a crash means a missed alert, never a double one |
| `ratings.py` | IMDb (OMDb) + Rotten Tomatoes critics/audience (MDBList, `api.mdblist.com/tmdb/{show,movie}/{id}`; OMDb has RT for movies only, 0 of 12 series). Weekly per title, for everything followed or picked; `telly ratings --force` refills early. The audience source is `popcorn` on the new host and `tomatoesaudience` on the legacy one, so both are parsed |
| `taste.py` | Taste beyond Plex plays, per person. Strong: `told` (Your taste page, plexbot `record_watched`) and `imdb_rating` (IMDb CSV upload; IMDb has no API or sign-in). **Weak by Chris's rule**: `overseerr` requests and `mentioned` chat mentions are interest, not liking (a request can be a try-out or for a friend), so they count at a third of a few watched episodes. Any title can be switched off (`use_for_picks`) and is still never recommended back |
| `llm.py` | Headless `claude -p` on the subscription. **Thinking off and effort pinned**: inheriting Chris's `xhigh` made Haiku think for 11k tokens, ~100 s a call |
| (taste in chat) | MCP `record_taste(title, liked)` finds the title itself (`taste.pick_title`: something they've watched wins, then an exact name, then TMDB order; two same-name titles nobody watched → `ambiguous`, never a guess) and records it in one call. plexbot's fast path calls it directly. `record_watched` is for when the tmdb_id is already known |
| `shows.py` | Per-person reads/writes shared by the MCP tools and the web API |
| `plexbot.py` | Signs plexbot web-chat tokens for signed-in members (`GET /api/plexbot-token`), in the exact format the Overseerr Worker uses: same secret (`PLEXBOT_AUTH_SECRET`), identity = the member's Overseerr id. No Overseerr account → 404 and no chat button. Format pinned by `PLEXBOT_WEB_VECTOR` in both repos' tests |
| `app.py` | `create_app()` (the MCP session manager runs once per instance, so tests build fresh apps) + the scheduler |

**The token contract** is pinned in both repos by one vector: `CONTRACT_TOKEN` in
`plex-agent/tests/test_telly.py` and `test_accepts_plexbots_token_format` here. Change them together.

## Web (`web/`)

Next.js 16 + Tailwind v4 (read `web/AGENTS.md`: this Next has breaking changes). Local:
`npm run dev -- -p 3940` against the API on :8940. **Only `/api/*` is proxied** (`next.config.ts`),
so `/mcp` and `/internal/*` 404 through the public site. The design system lives in
`src/app/globals.css`: cinematic dark glass. Real `backdrop-filter` goes only on floating
surfaces, motion runs only on arrival/hover/change/exit, and nothing runs while the page is idle.
Verify UI changes with Playwright at 1440 and 390 (no horizontal overflow on any page).

**plexbot's chat is embedded** (`components/plexbot.tsx`): a floating button → plexbot's own
`chat.html` in an iframe (full screen on phones), token minted on load and re-minted on open near
its 1 h expiry. plex-agent's `static/chat.js` must list `https://telly.chrisx.art` in
`ALLOWED_PARENT_ORIGINS`, or the frame shows "access denied".

## Deploy (NAS)

```bash
scripts/deploy.sh      # ships git archive HEAD, builds beside the live stack, swaps, health-checks
scripts/nas-env.sh     # (re)writes /volume2/docker/telly/.env from the Keychain; nothing printed
```

- Stack `/volume2/docker/telly`: `telly-api` :18940 (LAN; plexbot calls `http://192.168.4.22:18940/mcp`),
  `telly-web` :18941 (the only port the `nas-tunnel` routes to), subnet `10.230.0.0/24`,
  `data/telly.db` (700/600, the container runs as `cx` 1026:100).
- Cloudflare: `telly.chrisx.art` → `:18941` on `nas-tunnel`; one rate-limit rule (the Free plan's
  only one) on `/api/auth/*` and `/api/link/*`, 20 requests per 10 s per IP.
- **Headless Claude on the NAS** uses the subscription token plexbot already has (`claude setup-token`,
  a year-long token): Keychain `TELLY_CLAUDE_OAUTH_TOKEN` → `scripts/nas-env.sh` → `CLAUDE_CODE_OAUTH_TOKEN`.
  After a new token: re-run `nas-env.sh`, then `docker compose -p telly up -d --force-recreate telly-api`
  (a plain restart doesn't re-read `.env`). Without it, picks keep their earlier reasons and renewals are skipped.
- plexbot needs `TELLY_SHARED_SECRET` + `TELLY_URL` in its NAS `.env` (set 2026-09-27).
- Telly needs `TELLY_PLEXBOT_AUTH_SECRET` (Keychain `PLEXBOT_AUTH_SECRET`, via `nas-env.sh`) for the embedded chat; unset → the button never shows.
- OMDb (`OMDB_API_KEY`) and MDBList (`MDBLIST_API_KEY`) free keys: 1,000 calls a day each; a nightly run uses ~150.

## Gotchas

- PMS history only goes back to **2025-02-26** (799 plays). That's the whole taste signal until Trakt import exists.
- TMDB lags real-world renewals (SNW S5, Fallout S3). The weekly news check covers it. The two-poll rule trades a day of delay for no flip-flop alerts.
- **mcp SDK 2.x:** the transport's default allowed hosts are localhost only (plexbot's calls to `192.168.4.22` would get a 421), so Telly passes `mcp_allowed_hosts`. And `session_manager.run()` works once per instance.
- **`claude --bare` can't be used**: it only accepts an API key, never the subscription.
- **SQLite returns naive datetimes** even for `timezone=True` columns. Compare through `models.aware()`.
- **No `body` background in CSS**: with `html`'s set, body's paints over the `z-index:-1` backdrop and hides it.
- **The secrets hook blocks inline env handling.** Remote env files go through a committed script (`scripts/nas-env.sh`: `secret run` → `ssh … dd`), never `cat > .env`.
- 3 history items never match TMDB (personal videos in a movie library). This is expected; they're kept as `plex_items.match_status != 'matched'`.
