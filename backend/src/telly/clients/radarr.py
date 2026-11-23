"""Radarr v3, read-only (see arrbase.py)."""

from __future__ import annotations

import httpx

from ..config import get_settings
from .arrbase import ArrClient


class RadarrClient(ArrClient):
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 client: httpx.Client | None = None) -> None:
        s = get_settings()
        super().__init__(base_url or s.radarr_url, api_key or s.radarr_api_key, client)

    def movies(self) -> list[dict]:
        """The whole library (57 movies on 2026-11-22): id, tmdbId, title, monitored, hasFile, movieFile."""
        return self._get("/movie")
