"""Headless Claude on the subscription (`claude -p`), never the metered API.

Runs in a scratch cwd so no CLAUDE.md is loaded (each load costs ~40k tokens), on Sonnet 5.5,
with no tools unless the caller asks for web search. Returns parsed JSON or None; every
caller has a non-LLM fallback, so None is always safe.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

from .config import get_settings

log = logging.getLogger(__name__)


def available() -> bool:
    return get_settings().llm_enabled and shutil.which("claude") is not None


def _extract_json(text: str) -> dict | list | None:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def ask_json(prompt: str, *, web_search: bool = False, timeout: int = 180,
             effort: str = "low") -> dict | list | None:
    if not available():
        return None
    # Thinking off and effort pinned: otherwise the host's settings apply, and with Chris's
    # (xhigh) Haiku spent 11k thinking tokens / ~100 s on one rerank. Off, it's ~15 s.
    cmd = ["claude", "-p", prompt, "--model", get_settings().llm_model, "--effort", effort,
           "--settings", '{"alwaysThinkingEnabled": false}',
           "--output-format", "json", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    if web_search:
        cmd += ["--allowedTools", "WebSearch,WebFetch"]
    else:
        cmd += ["--disallowedTools", "Bash,Edit,Write,Read,WebSearch,WebFetch,Glob,Grep"]
    with tempfile.TemporaryDirectory(prefix="telly-llm-") as cwd:
        try:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL, env={**os.environ, "MAX_THINKING_TOKENS": "0"})
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("claude -p failed: %s", e)
            return None
    if p.returncode != 0:
        log.warning("claude -p exit %s: %s", p.returncode, p.stderr[-300:])
        return None
    try:
        envelope = json.loads(p.stdout[p.stdout.find("{"):])
    except json.JSONDecodeError:
        return None
    if envelope.get("is_error"):
        return None
    return _extract_json(envelope.get("result") or "")
