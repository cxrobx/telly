"""Outbound alert channels: Discord DM (as the plexbot bot) and ntfy."""

from __future__ import annotations

import httpx

from ..config import get_settings


class DiscordDM:
    """Discord REST, bot auth. Same bot identity people already talk to in #plexbot."""

    def __init__(self, token: str | None = None, client: httpx.Client | None = None) -> None:
        self.http = client or httpx.Client(
            base_url="https://discord.com/api/v10", timeout=15.0,
            headers={"Authorization": f"Bot {token or get_settings().discord_bot_token}"},
        )
        self._channels: dict[str, str] = {}

    def send(self, discord_id: str, content: str, embed: dict | None = None) -> None:
        ch = self._channels.get(discord_id)
        if ch is None:
            r = self.http.post("/users/@me/channels", json={"recipient_id": discord_id})
            r.raise_for_status()
            ch = self._channels[discord_id] = r.json()["id"]
        body: dict = {"content": content}
        if embed:
            body["embeds"] = [embed]
        r = self.http.post(f"/channels/{ch}/messages", json=body)
        r.raise_for_status()


class Ntfy:
    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None) -> None:
        self.base = (base_url or get_settings().ntfy_url).rstrip("/")
        self.http = client or httpx.Client(timeout=15.0)

    def send(self, topic: str, title: str, body: str, click: str | None = None) -> None:
        # JSON publish to the root URL: header publishing can't carry non-ASCII titles.
        msg = {"topic": topic, "title": title, "message": body, "tags": ["tv"]}
        if click:
            msg["click"] = click
        r = self.http.post(self.base, json=msg)
        r.raise_for_status()
