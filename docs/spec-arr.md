# Telly — Radarr/Sonarr (Ruddarr parity) spec

Status: **approved 2026-11-22 (decisions in §8). Phases 1 and 2 shipped 2026-11-22**: read-only clients and member status (`arr.py`), webhook alerts (`arrhook.py`), plus owner APNs for the admin app (`apns.py`, dormant until the key is set). Phases 3–5 moved to the iOS app (§8.3).

Goal: everything Ruddarr (MIT, iOS Radarr/Sonarr remote) does, inside Telly, so the NAS's
Radarr and Sonarr are run from the same app members already use. Ruddarr's feature list was
taken from its App Store page and README on 2026-11-22.

## 1. What's there today

- NAS: one `radarr`, one `sonarr`, `sabnzbd` (download client), `prowlarr` (indexers), `seerr`.
- Telly has **no Radarr/Sonarr client**. Members request through Seerr (`clients/overseerr.py`),
  and Seerr hands the request to Radarr/Sonarr.
- Telly already has an owner gate (`User.is_owner`, used by `/api/members`), a TMDB calendar
  (Timeline), alerts over Discord DM and ntfy, trending/discovery, and IMDb/RT ratings.

## 2. The one rule that shapes everything

Ruddarr is a single-user admin remote. Telly is shared by every member of the Plex server
(spec-identity §7). So Ruddarr's features split into two kinds:

| Kind | Who | Examples |
|---|---|---|
| **Status** (read-only, about titles, not people) | every member | "Downloading, 40%, ~12 min", "Missing, monitored", "In Plex", quality and size |
| **Admin** (writes, deletes, grabs, disk) | **owner only** | release search and grab, monitor/unmonitor, delete, queue cancel, disk, *arr history |

Admin routes check `is_owner` on the server, every call, the way `/api/members` does. The
Radarr/Sonarr API keys stay in the backend env; the browser never sees them or the *arr URLs.
**No admin tool goes into the MCP**, so plexbot (an LLM) can never delete or grab (see §8.1).

## 3. Ruddarr → Telly map

| Ruddarr | Telly | Who |
|---|---|---|
| Library grid, search, sort/filter (rating, root folder), counts | New **Manage → Library** page over Radarr movies + Sonarr series | owner |
| Media details: files, size on disk, bitrate, quality, tags, Trakt rating | Show page gets a **Files** panel; Telly's IMDb/RT ratings stand in for Trakt | status: all; files/tags: owner |
| Discovery grid, Popular This Week, hide what's in the library | **Already in Telly** (For You, Trending). Add "hide what's in the library" to Trending | all |
| Add movie/series, monitor collections | Members keep requesting through Seerr. Owner gets a direct **Add** (quality profile, root folder, monitor option) | owner |
| Monitor/unmonitor series, seasons, episodes | Toggles on the show page | owner |
| Delete movie/series/episode files | Delete with a typed confirmation; **"delete files" off by default** | owner |
| Interactive release search: filters (indexer, quality, group, language, freeleech), compact/detailed, force-grab season packs, search for replacement | **Release search** sheet on the show page, filters remembered per browser | owner |
| Queue: status per movie/season/episode, cancel, collapse packs, import pending, filter by client, warnings | **Manage → Queue**, polled every 5 s while open, nothing while idle | owner; members see status on titles |
| Calendar: per-instance and monitored filters, unmonitored faded, hide specials, jump to Today | **Already in Telly** (Timeline, TMDB). Add an owner toggle "*arr calendar": monitored state and file status from Radarr/Sonarr | all / owner |
| History events (grabbed, imported, upgraded, deleted) | **Manage → History** | owner |
| Notifications: grab, import, upgrade, downgrade, delete; tap opens the item | Radarr/Sonarr **webhook → Telly** (`/internal/arr-webhook`, shared secret, Docker network only) → existing alerts. Owner gets admin events; a member gets "it's in Plex" for titles they follow or requested | owner / all |
| Multiple instances, alternate URLs, auto protocol | Config is a list (`TELLY_ARR_INSTANCES`), one Radarr + one Sonarr today. Local/remote switching isn't needed: Telly talks to them on the NAS | — |
| Disk space per root folder and instance | **Manage → Disk** | owner |
| Open the instance's web UI | Link from Manage (tailnet/LAN URL) | owner |
| Appearance, accessibility | Already Telly's design system; check Reduced Motion and contrast on the new pages | all |
| Ruddarr+ push notifications ($5.99/yr) | ntfy already pushes to the phone for free. **Web push** (installed PWA) as an extra channel | all |
| Siri Shortcuts, Spotlight, iCloud sync, `ruddarr://` deep links | **Not portable to a web app.** Deep links become plain URLs (`/show/…`, `/manage/queue`) | — |

## 4. Backend

- `clients/radarr.py`, `clients/sonarr.py`: API v3, key in the `X-Api-Key` header (never the
  URL, so `logredact` isn't the only guard). Shapes verified live before use, as with Plex.
- `arr.py`: maps TMDB id → Radarr movie / Sonarr series (Sonarr by `tvdbId`, from TMDB's
  `external_ids`), caches status for 60 s, builds the member-safe status.
- `api.py`: `/api/arr/status/{tv|movie}/{tmdb_id}` (members), `/api/manage/*` (owner, a single
  `Owner` dependency in place of a per-route check).
- **Admin audit log**: every write (grab, delete, monitor, cancel) is a row with who, what,
  when, and the *arr response, shown on Manage → History.
- Writes are JSON-only (415 otherwise), as today.
- New env: `TELLY_RADARR_URL`, `TELLY_RADARR_API_KEY`, `TELLY_SONARR_URL`, `TELLY_SONARR_API_KEY`,
  `TELLY_ARR_WEBHOOK_SECRET`, through `scripts/nas-env.sh` from the Keychain. The keys come from
  each app's own config on the NAS; they aren't in the Keychain yet.

## 5. Web

- New owner-only **Manage** tab (hidden for members): Library, Queue, History, Disk.
- Show page: status chip for everyone; Files, monitor toggles, Release search, Delete for the owner.
- Must fit the closed Duo (382 wide). Release search on a phone is a full-screen sheet with
  compact rows.

## 6. Blast radius

- Delete defaults to keeping files; deleting files needs the title typed.
- Grab and delete aren't in the MCP and aren't reachable without an owner session.
- Local dev points at a **fake *arr** (recorded responses) by default; a real instance needs
  `TELLY_ARR_LIVE=1`, so tests and UI work can't grab or delete on the NAS.
- The webhook endpoint stays on the Docker network, like `/mcp`, never through the tunnel.

## 7. Phases (each ships on its own)

1. **Clients + member status**: read-only clients, TMDB → *arr mapping, status chip on show
   pages and Following. Fake *arr fixtures. No writes anywhere.
2. **Webhook alerts**: "it's in Plex" for followers and requesters; owner's grab/import/upgrade/delete alerts over Discord/ntfy.
3. **Manage (read)**: Library, Queue, History, Disk, owner-gated.
4. **Manage (write)**: monitor toggles, queue cancel/import, direct Add, Delete, audit log.
5. **Release search + grab**: interactive search, filters, force grab, search for replacement.
6. **Extras**: *arr calendar overlay, hide-in-library on Trending, web push.

## 8. Decisions

1. **Decided 2026-11-22: plexbot stays read-only toward Radarr/Sonarr.** Members can ask "is X downloading?" (status only). It gets no grab or delete tools, the owner's included, because the caller is an LLM.
2. **Decided 2026-11-22: members see download status** on titles, never who requested them. It's title data, not anyone's viewing data.
3. **Decided 2026-11-22: the admin half is an iOS app, not a web page.** Chris's own build of Ruddarr (MIT), forked from a pinned, audited commit, plus tabs that read Telly's API. It talks to Radarr/Sonarr directly over the tailnet, so the *arr keys never reach Telly, and telly.chrisx.art gets **no grab, delete or Manage routes**. That drops phases 3–5 and the owner-only admin rows in §3 from Telly. Telly keeps phase 1 (member status, read-only) and phase 2 (webhook alerts).
4. **Open: web push** as a third channel, or ntfy only. Recommended: ntfy now, web push in phase 6.
