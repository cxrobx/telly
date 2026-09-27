"""Overseerr: user id → plexId. Shape verified live 2026-09-27 (spec §2)."""

from __future__ import annotations

import httpx

from ..config import get_settings


class OverseerrClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 client: httpx.Client | None = None) -> None:
        s = get_settings()
        self.http = client or httpx.Client(
            base_url=(base_url or s.overseerr_url).rstrip("/") + "/api/v1", timeout=10.0,
            headers={"X-Api-Key": api_key or s.overseerr_api_key},
        )

    def plex_id_for(self, overseerr_user_id: int) -> int | None:
        r = self.http.get(f"/user/{overseerr_user_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        pid = r.json().get("plexId")
        return int(pid) if pid else None

    def users(self) -> list[dict]:
        r = self.http.get("/user", params={"take": 500})
        r.raise_for_status()
        return [{"id": u["id"], "plex_id": u.get("plexId")} for u in r.json().get("results", [])]

    def request(self, overseerr_user_id: int, media_type: str, tmdb_id: int) -> dict:
        """File a request as that person (X-API-User, verified in Overseerr's auth middleware).
        Their own Overseerr permissions and approval rules apply, as if they'd clicked it there."""
        body: dict = {"mediaType": media_type, "mediaId": tmdb_id}
        if media_type == "tv":
            body["seasons"] = "all"
        r = self.http.post("/request", json=body, headers={"X-API-User": str(overseerr_user_id)})
        if r.status_code >= 400:
            try:
                msg = r.json().get("message") or r.text
            except ValueError:
                msg = r.text
            raise RuntimeError(f"Overseerr {r.status_code}: {msg[:200]}")
        d = r.json()
        return {"status": d.get("status"), "id": d.get("id")}

    def requests_of(self, overseerr_user_id: int) -> list[dict]:
        """That person's requests: [{tmdb_id, media_type, created_at}] (verified live 2026-09-27)."""
        from datetime import datetime
        out, skip = [], 0
        while True:
            r = self.http.get(f"/user/{overseerr_user_id}/requests", params={"take": 100, "skip": skip})
            r.raise_for_status()
            d = r.json()
            for req in d.get("results", []):
                m = req.get("media") or {}
                if m.get("tmdbId") and m.get("mediaType") in ("tv", "movie"):
                    created = req.get("createdAt")
                    out.append({"tmdb_id": int(m["tmdbId"]), "media_type": m["mediaType"],
                                "created_at": datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None})
            skip += len(d.get("results", []))
            if not d.get("results") or skip >= (d.get("pageInfo") or {}).get("results", 0):
                return out

