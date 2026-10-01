"""Keep keys out of the logs. httpx logs every request URL at INFO, and OMDb and MDBList
only take their key as a query parameter, so a logged URL can carry a secret.

The Plex token goes in a header (clients/plex.py) for the same reason; this filter is the
backstop for anything still in a URL.
"""

from __future__ import annotations

import logging
import re

_SECRET_PARAM = re.compile(r"(?i)([?&](?:x-plex-token|apikey|api_key|access_token|token|key)=)[^&\s\"']+")


def redact(text: str) -> str:
    return _SECRET_PARAM.sub(r"\1<redacted>", text)


class RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        return True


def install() -> None:
    """Attach the filter to the httpx logger once (idempotent)."""
    lg = logging.getLogger("httpx")
    if not any(isinstance(f, RedactSecrets) for f in lg.filters):
        lg.addFilter(RedactSecrets())
