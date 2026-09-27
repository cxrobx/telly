"""Settings, read from env (prefix TELLY_) and backend/.env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TELLY_", env_file=".env", extra="ignore")

    plex_url: str = "http://192.168.4.22:32400"
    plex_token: str = ""  # PMS admin (owner) token
    plex_client_id: str = "telly-cx-8f3a61c2"  # X-Plex-Client-Identifier for plex.tv calls
    tmdb_token: str = ""  # TMDB v4 read access token
    database_url: str = "sqlite:///data/telly.db"

    # How many consecutive polls a data-edit fact must hold before it counts (spec §6).
    confirm_polls: int = 2

    # Where people open Telly (link pages, alert click-through).
    public_url: str = "http://localhost:8940"

    # plexbot ↔ Telly (spec §5). HMAC secret shared with plex-agent; empty = /mcp refuses all.
    shared_secret: str = ""
    # Host headers the MCP transport accepts (DNS-rebinding guard). The SDK default is
    # localhost only, which would reject plexbot calling 192.168.4.22 or telly-api.
    mcp_allowed_hosts: list[str] = [
        "localhost:*", "127.0.0.1:*", "192.168.4.22:*", "100.88.68.22:*", "telly-api:*",
    ]

    # Overseerr: maps plexbot's web surface (Overseerr user id) → plexId (spec §4).
    overseerr_url: str = "http://192.168.4.22:5055"
    overseerr_api_key: str = ""

    # Alerts (spec §6). DMs go out as the plexbot bot via Discord REST.
    discord_bot_token: str = ""
    ntfy_url: str = "https://ntfy.sh"
    alert_max_age_hours: int = 72  # never send an alert about something older than this
    alert_max_attempts: int = 3

    # Web sessions (spec §3): signed cookie carrying only plex_id; Fernet key for stored
    # Plex tokens (never in the DB in the clear). Both come from Keychain.
    session_secret: str = ""
    token_key: str = ""
    session_days: int = 30
    cookie_secure: bool = False  # true on the NAS (https via the tunnel)
    # Local UI work only: GET /api/auth/dev signs in as this plex_id. 0 = route disabled.
    dev_login_plex_id: int = 0

    # Headless Claude (subscription via CLAUDE_CODE_OAUTH_TOKEN or a logged-in CLI).
    # Everything that uses it degrades cleanly when it's unavailable.
    llm_enabled: bool = True
    llm_model: str = "haiku"

    # Outside scores (ratings.py). OMDb: IMDb for everything, RT for movies. MDBList: RT for series, and IMDb when OMDb has none yet.
    omdb_api_key: str = ""
    mdblist_api_key: str = ""

    # plexbot's web chat inside Telly (plexbot.py): the same secret the Overseerr Worker signs with.
    plexbot_auth_secret: str = ""
    plexbot_chat_url: str = "https://plexbot.chrisx.art/chat.html?v=5"

    scheduler_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
