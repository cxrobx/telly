# Telly — "Connect Discord" spec (parked)

Status: **parked 2026-09-27, not built.** Chris's call: today nobody but him uses plexbot on
Discord, so a one-click link is overkill. Build it when the tripwire below trips. This is the
ten-minute prose review for a new auth flow, done ahead of time, so the build can start cold.

## 1. The problem it solves

Telly knows people by their plex.tv account (see [`spec-identity.md`](spec-identity.md)).
Discord tells it nothing about who someone is on Plex, so a Discord user is linked only after
proving both sides. Today that is `!link` in #plexbot: the bot DMs a one-time link, the person
opens it and signs in with Plex (`linking.py`). It works, but it's a round trip through a DM
and a second sign-in for someone who is usually already signed in to Telly.

Things already ruled out, so nobody re-checks them:
- **Overseerr's Discord ID field.** All 6 Overseerr users have it empty (checked 2026-09-27).
- **Plex ↔ Discord.** Neither knows about the other; Discord's "connections" don't include Plex.
- **Matching display names.** A guess. A wrong guess sends one friend's alerts to another.
- **The owner.** Already linked with no steps (`source=admin`: plexbot's `DISCORD_ADMIN_USER_ID`
  ↔ the Plex owner, both facts Chris configured). This spec is for everyone else.

## 2. Tripwire (when to build it)

CXTasks script `plexbot-discord-friends` (in the CXTasks tripwires folder, checked weekly).
It trips when **anyone other than Chris has used plexbot on Discord in the last 30 days**,
read from plexbot's session transcripts on the NAS. Test identities, the system prompt's
example sender and the admin are excluded. A friend asking for Discord alerts is the same
signal by word of mouth: build it then too.

## 3. The flow

For someone already signed in to Telly with Plex, which is already the proof of the Plex side:

1. Settings → **Connect Discord**. `GET /api/link/discord/start` (requires the session cookie)
   makes a random `state`, stores it in a signed, short-lived cookie (`telly_dstate`, 10 min,
   HttpOnly, SameSite=Lax, bound to the session's plex_id), and redirects to
   `https://discord.com/oauth2/authorize?response_type=code&client_id=…&scope=identify&state=…&redirect_uri=…&prompt=none`.
2. Discord shows its own "Authorize" screen (just the `identify` scope: id and username, no
   email, no servers, no messages).
3. Discord redirects to `GET /api/link/discord/callback?code=…&state=…`. Telly checks:
   - the session cookie is present and its plex_id is the one the state cookie was bound to;
   - `state` matches the cookie (constant-time compare), then deletes the cookie (one use);
   - if `error=access_denied` is present, it goes back to Settings with "not connected" and changes nothing.
4. Telly exchanges the code at `POST https://discord.com/api/oauth2/token` (form body, client
   id and secret by HTTP Basic, same `redirect_uri`), calls `GET https://discord.com/api/users/@me`
   with the access token, then **revokes the token** (`POST /api/oauth2/token/revoke`). Telly
   never stores a Discord token. It only needs the id once.
5. Upsert `IdentityLink(surface="discord", external_id=<id>, plex_id=<session>, source="oauth",
   display_name=<username>)`, then redirect to Settings, which shows "Linked to @username".

**Conflicts:**
- **That Discord account is linked to a different Plex account** → refuse with "that Discord
  account is already linked to another member; unlink it there first". Never silently move a link.
- **This Plex account is linked to a different Discord** → replace it. It's the same person,
  proved on both sides just now.

**Unlink:** a button next to "Linked to …" deletes the row (the same effect as `!unlink`).
`!link` keeps working for anyone who prefers it.

## 4. What it needs from Chris (the only manual step)

In the Discord Developer Portal, on plexbot's existing application → OAuth2:
1. Add the redirect `https://telly.chrisx.art/api/link/discord/callback`.
2. Reset or copy the **client secret** (this is not the bot token), then save it with `sk TELLY_DISCORD_CLIENT_SECRET`.

The client id isn't secret (it's the application id). Put it in config as
`TELLY_DISCORD_CLIENT_ID`. `scripts/nas-env.sh` gains `-k TELLY_DISCORD_CLIENT_SECRET`. No new app,
no new bot, and no new Cloudflare rule: `/api/link/*` is already rate-limited (20 requests per 10 s per IP).

## 5. Build plan

- `backend/src/telly/discord_oauth.py`: `authorize_url(state)`, `exchange(code) -> {id, username}`
  (token → @me → revoke), with an injectable HTTP client for tests.
- `api.py`: the two routes plus `DELETE /api/link/discord`. Every route requires `current_user`.
- `web/src/app/settings/page.tsx`: when not linked, a Connect Discord button (a plain link to
  `/api/link/discord/start`). When linked, "Linked to @name" and an Unlink button. Hide the
  button if the server says the feature is unconfigured (`me.discord_connect_available`).
- Config: `discord_client_id`, `discord_client_secret`. If either is empty, the routes 404 and the button hides.

**Tests (all offline, with Discord faked):**
- the happy path links the session's plex_id, and the token gets revoked;
- a state mismatch, a missing state cookie, or a state bound to another plex_id → 400, and nothing is written;
- no session → 401 before any redirect;
- a Discord id already linked to someone else → refused, and the existing row is unchanged;
- `access_denied` → back to Settings, nothing written;
- unconfigured → 404, and the button hides;
- the state cookie is single-use: replaying the callback URL fails.

**Verify:** local with a dev-login session and a fake Discord; then production once with Chris
unlinking and relinking his own Discord through the button, checking that the `IdentityLink`
row's `source` goes from `admin` to `oauth`.

Estimated at about 2 hours, most of it the tests.
