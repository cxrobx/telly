"""Plex Media Server (admin token) + plex.tv. Shapes verified live 2026-09-27 (spec §2)."""

from __future__ import annotations

import httpx

from ..config import get_settings


class PlexClient:
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 client: httpx.Client | None = None) -> None:
        s = get_settings()
        self.base_url = (base_url or s.plex_url).rstrip("/")
        self.token = token or s.plex_token
        self.client_id = s.plex_client_id
        self.http = client or httpx.Client(timeout=20.0)

    def _pms(self, path: str, **params) -> dict:
        r = self.http.get(f"{self.base_url}{path}", params={"X-Plex-Token": self.token, **params},
                          headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json().get("MediaContainer", {})

    def accounts(self) -> list[dict]:
        """Server accounts: [{id, name}]. The owner is id 1; everyone else's id is their plex.tv id."""
        return [{"id": a["id"], "name": a.get("name") or ""}
                for a in self._pms("/accounts").get("Account", []) if a.get("id")]

    def history_page(self, start: int, size: int = 200) -> tuple[int, list[dict]]:
        """One page of play history, newest first. Returns (totalSize, items)."""
        mc = self._pms("/status/sessions/history/all", sort="viewedAt:desc",
                       **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": size})
        return int(mc.get("totalSize") or 0), mc.get("Metadata", [])

    def item(self, rating_key: str) -> dict | None:
        """Library item with guids, or None if it's gone from the library."""
        try:
            items = self._pms(f"/library/metadata/{rating_key}", includeGuids=1).get("Metadata", [])
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        return items[0] if items else None

    def sections(self) -> list[dict]:
        return [{"key": d["key"], "type": d["type"], "title": d.get("title", "")}
                for d in self._pms("/library/sections").get("Directory", [])]

    def section_items(self, key: str) -> list[dict]:
        """[{rating_key, title, year}] for one library section (no guids: PMS omits them here)."""
        return [{"rating_key": str(m["ratingKey"]), "title": m.get("title", ""), "year": m.get("year")}
                for m in self._pms(f"/library/sections/{key}/all").get("Metadata", [])]

    def owner_plex_id(self) -> int:
        """The server owner's plex.tv id (PMS calls the owner account 1)."""
        r = self.http.get("https://plex.tv/api/v2/user", headers={
            "Accept": "application/json", "X-Plex-Token": self.token,
            "X-Plex-Client-Identifier": self.client_id, "X-Plex-Product": "Telly",
        })
        r.raise_for_status()
        return int(r.json()["id"])


def tmdb_id_from_guids(item: dict) -> int | None:
    for g in item.get("Guid", []) or []:
        gid = g.get("id", "")
        if gid.startswith("tmdb://"):
            try:
                return int(gid.removeprefix("tmdb://"))
            except ValueError:
                return None
    return None
