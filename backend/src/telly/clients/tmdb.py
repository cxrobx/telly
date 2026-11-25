"""TMDB v3 API with a v4 read token (Bearer), the same way discoverr calls it."""

from __future__ import annotations

from datetime import date

import httpx

from ..config import get_settings

BASE_URL = "https://api.themoviedb.org/3"


class TMDBClient:
    def __init__(self, token: str | None = None, client: httpx.Client | None = None) -> None:
        self._anime_pages: dict[tuple[str, int], list[dict]] = {}
        self._new_pages: dict[tuple[str, str, str, int], list[dict]] = {}
        self._airing_pages: dict[tuple[str, str, int], list[dict]] = {}
        self._genre_pages: dict[tuple[str, int, int], list[dict]] = {}
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

    def anime_discovery(self, media_type: str, page: int = 1) -> list[dict]:
        """Public anime discovery, shared across members in one build. No watch history sent."""
        key = (media_type, page)
        if key not in self._anime_pages:
            self._anime_pages[key] = (self._get(
                f"/discover/{media_type}", with_genres=16, with_original_language="ja",
                include_adult="false", sort_by="popularity.desc", page=page,
                **{"vote_count.gte": 50}) or {}).get("results", [])
        return self._anime_pages[key]

    def new_releases(self, media_type: str, since: date, until: date, page: int = 1) -> list[dict]:
        """Public new TV premieres or movie releases in [since, until], cached across a batch."""
        lo, hi = since.isoformat(), until.isoformat()
        key = (media_type, lo, hi, page)
        if key not in self._new_pages:
            if media_type == "tv":
                params = {"first_air_date.gte": lo, "first_air_date.lte": hi, "vote_count.gte": 10}
            else:
                params = {"primary_release_date.gte": lo, "primary_release_date.lte": hi,
                          "vote_count.gte": 15}
            self._new_pages[key] = (self._get(
                f"/discover/{media_type}", include_adult="false", sort_by="popularity.desc",
                page=page, **params) or {}).get("results", [])
        return self._new_pages[key]

    def airing_recently(self, since: date, until: date, page: int = 1) -> list[dict]:
        """Public TV shows actively airing episodes in [since, until] (catches new seasons)."""
        lo, hi = since.isoformat(), until.isoformat()
        key = (lo, hi, page)
        if key not in self._airing_pages:
            self._airing_pages[key] = (self._get(
                "/discover/tv", include_adult="false", sort_by="popularity.desc", page=page,
                **{"air_date.gte": lo, "air_date.lte": hi, "vote_count.gte": 30}) or {}).get("results", [])
        return self._airing_pages[key]

    def discover_genre(self, media_type: str, genre_id: int, page: int = 1) -> list[dict]:
        """Public popular, well-rated titles for a single genre, cached across a batch."""
        key = (media_type, genre_id, page)
        if key not in self._genre_pages:
            self._genre_pages[key] = (self._get(
                f"/discover/{media_type}", with_genres=genre_id, include_adult="false",
                sort_by="popularity.desc", page=page,
                **{"vote_count.gte": 50, "vote_average.gte": 6.8}) or {}).get("results", [])
        return self._genre_pages[key]

    def acclaimed_anime(self, media_type: str, page: int = 1) -> list[dict]:
        return (self._get(f"/discover/{media_type}", with_genres=16,
            with_original_language="ja", include_adult="false", sort_by="vote_average.desc",
            page=page, **{"vote_count.gte": 200, "vote_average.gte": 7.5}) or {}).get("results", [])

    def find_imdb(self, imdb_id: str) -> dict | None:
        return self._get(f"/find/{imdb_id}", external_source="imdb_id")

    def search_multi(self, query: str) -> list[dict]:
        return [r for r in (self._get("/search/multi", query=query) or {}).get("results", [])
                if r.get("media_type") in ("tv", "movie")]

