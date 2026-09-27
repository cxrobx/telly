# Telly

What to watch next, for everyone on a Plex server.

Telly reads each person's Plex watch history and turns it into:

- **Alerts** when a show they follow gets a new episode, a season date, a renewal or a
  cancellation, sent as a Discord DM or a phone push (ntfy).
- **A timeline** of what's coming for the shows they follow.
- **Picks**: recommendations from their history, what they've said they liked, their IMDb
  ratings and what's trending, each with a one-line reason. IMDb and Rotten Tomatoes scores
  are shown alongside.
- **An MCP server** so a chat bot can answer "what's new for me?" or record "I loved The Bear"
  for whoever is asking, and only for them.

It's built for one server and a handful of people, and it's running on a Synology NAS.

## How it works

```
Plex (history, accounts) ─┐
TMDB (shows, dates)       ├─► backend: FastAPI + SQLite + APScheduler ─► web (Next.js)
Overseerr (requests)      │         │                                  ─► Discord DMs, ntfy
OMDb / MDBList (scores)  ─┘         └─► /mcp (MCP, per-caller token)   ─► a chat bot
```

- **Identity** is the plex.tv account. People sign in with Plex. The MCP takes a short-lived,
  HMAC-signed token for the person asking, and no tool has a user parameter, so a bot can't
  ask about anyone else. The design is in [`docs/spec-identity.md`](docs/spec-identity.md).
- **Change detection** waits for a fact to hold across two polls before alerting, so TMDB
  edits that flip back don't send anything. Each event has a dedupe key, and an alert is sent
  at most once.
- **Renewals TMDB hasn't caught up on** come from a weekly news check that only accepts a
  trusted outlet, for the next season, confirmed by fetching the article.
- **Picks** re-rank TMDB recommendations with a small model, using headless Claude Code on a
  subscription. Without it, they fall back to templated reasons.
- **Requests aren't taste.** A request can be a try-out or for a friend, so Overseerr requests
  count weakly, and any title can be switched off for picks.

## Run it locally

Needs Python 3.12 with [uv](https://docs.astral.sh/uv/), and Node 22.

```bash
cd backend
cp .env.example .env        # fill in TELLY_PLEX_TOKEN, TELLY_TMDB_TOKEN, ... (see config.py)
uv run telly migrate
uv run telly run            # sync history, poll TMDB, build picks
uv run telly serve          # API on :8940

cd ../web
npm install
npm run dev -- -p 3940      # proxies /api/* to :8940
```

`uv run pytest -q` runs the backend tests. Every setting is in
[`backend/src/telly/config.py`](backend/src/telly/config.py) (env prefix `TELLY_`).

## Status

A personal project, shared as-is. [`CLAUDE.md`](CLAUDE.md) is the working notes for the
deployment it runs in, and the scripts in `scripts/` and `deploy/` assume that setup.
