"""TMDB v3 API with a v4 read token (Bearer), the same way discoverr calls it."""

from __future__ import annotations

import httpx

from ..config import get_settings

BASE_URL = "https://api.themoviedb.org/3"


class TMDBClient:
    def __init__(self, token: str | None = None, client: httpx.Client | None = None) -> None:
        self.http = client or httpx.Client(
            base_url=BASE_URL, timeout=15.0,
            headers={"Authorization": f"Bearer {token or get_settings().tmdb_token}",
                     "Accept": "application/json"},
        )

    def _get(self, path: str, **params) -> dict | None:
        r = self.http.get(path, params=params)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def tv(self, tmdb_id: int) -> dict | None:
        return self._get(f"/tv/{tmdb_id}", append_to_response="external_ids")

    def imdb_id(self, media_type: str, tmdb_id: int) -> str | None:
        return (self._get(f"/{media_type}/{tmdb_id}/external_ids") or {}).get("imdb_id") or None

    def movie(self, tmdb_id: int) -> dict | None:
        return self._get(f"/movie/{tmdb_id}")

    def search_tv(self, query: str) -> list[dict]:
        return (self._get("/search/tv", query=query) or {}).get("results", [])

    def recommendations(self, media_type: str, tmdb_id: int) -> list[dict]:
        return (self._get(f"/{media_type}/{tmdb_id}/recommendations") or {}).get("results", [])

    def trending(self, media_type: str, window: str = "week") -> list[dict]:
        return (self._get(f"/trending/{media_type}/{window}") or {}).get("results", [])

    def find_imdb(self, imdb_id: str) -> dict | None:
        return self._get(f"/find/{imdb_id}", external_source="imdb_id")

    def search_multi(self, query: str) -> list[dict]:
        return [r for r in (self._get("/search/multi", query=query) or {}).get("results", [])
                if r.get("media_type") in ("tv", "movie")]

