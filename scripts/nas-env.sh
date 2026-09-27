#!/usr/bin/env bash
# Write /volume2/docker/telly/.env on the NAS from the Keychain. Nothing is printed: the values
# go from `secret run` straight into a chmod-600 file over ssh (dd, since the NAS has no sftp).
# Same pattern as ~/Projects/aimedia/scripts/nas-env.sh.
#
# Headless Claude (recommendation reasons, the weekly renewal check) switches on by itself once
# a subscription token is stored:  claude setup-token  →  copy it  →  sk TELLY_CLAUDE_OAUTH_TOKEN
# then run this again and restart telly-api.
#
#   scripts/nas-env.sh [public url]        default https://telly.chrisx.art
set -euo pipefail
cd "$(dirname "$0")/.."
URL="${1:-https://telly.chrisx.art}"
DIR=/volume2/docker/telly

keys=(-k TELLY_PLEX_TOKEN=PLEX_TOKEN -k TELLY_TMDB_TOKEN=TMDB_READ_TOKEN -k TELLY_SHARED_SECRET
      -k TELLY_OVERSEERR_API_KEY=OVERSEERR_API_KEY -k TELLY_SESSION_SECRET -k TELLY_TOKEN_KEY
      -k TELLY_DISCORD_BOT_TOKEN=PLEXBOT_DISCORD_TOKEN -k TELLY_OMDB_API_KEY=OMDB_API_KEY)
# Rotten Tomatoes for series (ratings.py): free key from mdblist.com → Preferences → API
if secret has MDBLIST_API_KEY >/dev/null 2>&1; then keys+=(-k TELLY_MDBLIST_API_KEY=MDBLIST_API_KEY); fi
# plexbot's web chat inside Telly (plexbot.py): the secret plex-agent verifies web tokens with
if secret has PLEXBOT_AUTH_SECRET >/dev/null 2>&1; then keys+=(-k TELLY_PLEXBOT_AUTH_SECRET=PLEXBOT_AUTH_SECRET); fi
LLM=false
if secret has TELLY_CLAUDE_OAUTH_TOKEN >/dev/null 2>&1; then
  keys+=(-k CLAUDE_CODE_OAUTH_TOKEN=TELLY_CLAUDE_OAUTH_TOKEN)
  LLM=true
fi

ssh nas "mkdir -p $DIR"
secret run "${keys[@]}" -- env TELLY_PUBLIC_URL="$URL" TELLY_COOKIE_SECURE=true TELLY_LLM_ENABLED="$LLM" \
  python3 -c 'import os; print("\n".join(f"{k}={os.environ[k]}" for k in sorted(os.environ) if k.startswith("TELLY_") or k == "CLAUDE_CODE_OAUTH_TOKEN"))' \
  | ssh nas "umask 077 && dd of=$DIR/.env status=none && chmod 600 $DIR/.env && wc -l < $DIR/.env | xargs echo lines:"
echo "public url: $URL · headless Claude: $LLM"
