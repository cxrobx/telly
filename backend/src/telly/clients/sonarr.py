"""Sonarr v3 API (Sonarr 4), read-only (see arrbase.py)."""

from __future__ import annotations

import httpx

from ..config import get_settings
from .arrbase import ArrClient


class SonarrClient(ArrClient):
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 client: httpx.Client | None = None) -> None:
        s = get_settings()
        super().__init__(base_url or s.sonarr_url, api_key or s.sonarr_api_key, client)

    def series(self) -> list[dict]:
        """The whole library (84 series on 2026-11-22): id, tvdbId, tmdbId (Sonarr 4), title,
        monitored, statistics {episodeFileCount, episodeCount, totalEpisodeCount}."""
        return self._get("/series")
