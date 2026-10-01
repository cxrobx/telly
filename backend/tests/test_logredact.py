"""No key or token ever reaches the logs (logredact.py, clients/plex.py)."""

from __future__ import annotations

import logging

import httpx

from telly import logredact
from telly.clients.plex import PlexClient


def test_plex_token_goes_in_a_header_not_the_url():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"MediaContainer": {"Account": [{"id": 7, "name": "x"}]}})

    c = PlexClient("http://pms.test:32400", token="tok-secret-123",
                   client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.accounts() == [{"id": 7, "name": "x"}]
    assert "tok-secret-123" not in str(seen[0].url)
    assert seen[0].headers["X-Plex-Token"] == "tok-secret-123"


def test_redact_blanks_every_secret_param():
    url = ("GET https://www.omdbapi.com/?i=tt1&apikey=abc123 "
           "http://pms:32400/library?X-Plex-Token=plx&includeGuids=1 "
           "https://api.mdblist.com/tmdb/show/1?apikey=zz9&token=tk42")
    out = logredact.redact(url)
    for secret in ("abc123", "plx", "zz9", "tk42"):
        assert secret not in out
    assert "i=tt1" in out and "includeGuids=1" in out


def test_httpx_log_lines_are_redacted(caplog):
    logredact.install()
    logredact.install()  # idempotent: one filter, not two
    lg = logging.getLogger("httpx")
    assert sum(isinstance(f, logredact.RedactSecrets) for f in lg.filters) == 1
    with caplog.at_level(logging.INFO, logger="httpx"):
        # The exact call httpx makes: a format string plus args, one of them the URL.
        lg.info('HTTP Request: %s %s "%s %d %s"', "GET",
                httpx.URL("https://www.omdbapi.com/?i=tt1&apikey=abc123"), "HTTP/1.1", 200, "OK")
    assert "abc123" not in caplog.text
    assert "apikey=<redacted>" in caplog.text
