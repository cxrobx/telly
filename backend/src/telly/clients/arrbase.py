"""Shared read-only Radarr/Sonarr v3 client. Shapes verified live 2026-11-22 (Radarr 6.0.4,
Sonarr 4.0.16). The key goes in the X-Api-Key header, never the URL, so it can't reach a log.
Nothing here writes: docs/spec-arr.md §8.3 keeps every *arr write out of Telly."""

from __future__ import annotations

import httpx


class ArrClient:
    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        self.http = client or httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v3", timeout=10.0,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
        )

    def _get(self, path: str, **params):
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def queue(self) -> list[dict]:
        """Every queue record (paged; a home queue is a page or two)."""
        out, page = [], 1
        while True:
            d = self._get("/queue", page=page, pageSize=200)
            recs = d.get("records") or []
            out.extend(recs)
            if not recs or len(out) >= (d.get("totalRecords") or 0):
                return out
            page += 1
