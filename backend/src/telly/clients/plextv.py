"""plex.tv PIN sign-in ("Sign in with Plex", the flow Overseerr uses). Spec §3."""

from __future__ import annotations

from urllib.parse import quote, urlencode

import httpx

from ..config import get_settings


class PlexTV:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client_id = get_settings().plex_client_id
        self.http = client or httpx.Client(base_url="https://plex.tv/api/v2", timeout=15.0)

    def _headers(self, token: str | None = None) -> dict:
        h = {"Accept": "application/json", "X-Plex-Product": "Telly",
             "X-Plex-Client-Identifier": self.client_id}
        if token:
            h["X-Plex-Token"] = token
        return h

    def create_pin(self) -> dict:
        """→ {id, code}. The code goes in the auth URL; the id is what we poll."""
        r = self.http.post("/pins", params={"strong": "true"}, headers=self._headers())
        r.raise_for_status()
        d = r.json()
        return {"id": int(d["id"]), "code": d["code"]}

    def auth_url(self, code: str, forward_url: str | None = None) -> str:
        params = {"clientID": self.client_id, "code": code, "context[device][product]": "Telly"}
        if forward_url:
            params["forwardUrl"] = forward_url
        return "https://app.plex.tv/auth#?" + urlencode(params, quote_via=quote)

    def pin_token(self, pin_id: int) -> str | None:
        """The user's token once they've approved the PIN, else None."""
        r = self.http.get(f"/pins/{pin_id}", headers=self._headers())
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("authToken") or None

    def user(self, token: str) -> dict:
        r = self.http.get("/user", headers=self._headers(token))
        r.raise_for_status()
        d = r.json()
        return {"id": int(d["id"]), "username": d.get("username") or d.get("title") or ""}

    def watchlist(self, token: str) -> list[dict]:
        """The person's Plex watchlist → [{tmdb_id, media_type, title, added_at}] (verified live)."""
        out, start = [], 0
        while True:
            r = httpx.get("https://discover.provider.plex.tv/library/sections/watchlist/all",
                          headers=self._headers(token), timeout=20.0,
                          params={"includeGuids": 1, "X-Plex-Container-Start": start,
                                  "X-Plex-Container-Size": 100})
            r.raise_for_status()
            mc = r.json().get("MediaContainer", {})
            items = mc.get("Metadata", []) or []
            for i in items:
                tmdb = next((g["id"][7:] for g in i.get("Guid", []) or []
                             if g.get("id", "").startswith("tmdb://")), None)
                if tmdb and tmdb.isdigit() and i.get("type") in ("show", "movie"):
                    out.append({"tmdb_id": int(tmdb), "title": i.get("title") or "",
                                "media_type": "tv" if i["type"] == "show" else "movie",
                                "added_at": i.get("addedAt")})
            start += len(items)
            if not items or start >= int(mc.get("totalSize") or 0):
                return out

