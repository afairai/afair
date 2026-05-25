# syntax=docker/dockerfile:1.7
# Multi-stage build. Single image runs in local, self-hosted, and managed (Fly) contexts.

FROM python:3.12-slim-bookworm AS base

# uv: fast Python package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ── Stage 1: install dependencies (cached when pyproject + lockfile unchanged) ──
FROM base AS deps

WORKDIR /app

# System libs needed by sqlite-vec / sqlite-utils at runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock* ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# ── Stage 2: build with project code ────────────────────────────────────────────
FROM deps AS builder
COPY neverforget ./neverforget
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ── Stage 3: runtime ────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libsqlite3-0 \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home --home-dir /home/app app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/neverforget /app/neverforget

# Persistent vault dir — on Fly this is the mounted volume; locally it's just a dir.
RUN mkdir -p /data/vault && chown -R app:app /data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=fly \
    VAULT_DIR=/data/vault \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8080

USER app

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "neverforget"]
