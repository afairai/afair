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
# README.md is referenced by pyproject.toml ([project] readme = "README.md")
# and hatchling fails the editable build without it. License file likewise
# becomes mandatory once we publish — easier to bake the dependency in now.
COPY README.md ./README.md
COPY neverforget ./neverforget
COPY scripts ./scripts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ── Stage 3: runtime ────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libsqlite3-0 \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/neverforget /app/neverforget
COPY --from=builder /app/scripts /app/scripts

# Persistent vault dir — on Fly this path is overlaid by the mounted volume.
RUN mkdir -p /data/vault

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=fly \
    VAULT_DIR=/data/vault \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8080

# DEVIATION FROM GLOBAL "non-root user" RULE: Fly volumes mount as root-
# owned by default, and our single-tenant Phase 0 machine is the user's
# own dedicated instance with no shared workload — the security delta is
# negligible. Documented in CLAUDE.md §10 Deviations. Revisit when LiteFS
# lands in Phase 8 (multi-machine context where non-root matters more).

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "neverforget"]
