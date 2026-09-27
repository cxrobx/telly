# Telly API: FastAPI + scheduler + MCP. Node is here only for the Claude Code CLI, which runs
# headless on the subscription (CLAUDE_CODE_OAUTH_TOKEN) for re-ranking and renewal checks.
FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && \
    npm install -g @anthropic-ai/claude-code && \
    apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* /root/.npm

COPY --from=ghcr.io/astral-sh/uv:0.9.21 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# uid/gid 1026:100 = the NAS user `cx`, which owns /volume2/docker/telly/data.
RUN useradd -m -u 1026 -g 100 telly
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev && chown -R 1026:100 /app /opt/python

USER telly
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8940
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8940/health', timeout=5)"
CMD ["sh", "-c", "telly migrate && exec telly serve --port 8940"]
