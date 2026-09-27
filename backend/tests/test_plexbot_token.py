"""plexbot web-chat tokens signed by Telly (plexbot.py).

PLEXBOT_WEB_VECTOR is also asserted in plex-agent/tests/test_telly.py, verified there by
plexbot's own _verify_token. Change the format in both places or neither."""

from __future__ import annotations

from telly.plexbot import mint

PLEXBOT_WEB_VECTOR = (
    "eyJpZCI6IjEiLCJuYW1lIjoiQ2hyaXMiLCJpYXQiOjE3OTAwMDAwMDAsImV4cCI6MTc5MDAwMzYwMH0"
    ".Cw2a-88sXVpRPtCRiS6BF5NNyem8I-vItAPkuJ3QhPM"
)


def test_matches_the_format_plexbot_verifies():
    assert mint(1, "Chris", ttl=3600, secret="contract-secret", now=1790000000) == PLEXBOT_WEB_VECTOR
