# syntax=docker/dockerfile:1.9
# Multi-stage build. Three targets, deliberately:
#   api     -> the request path. No torch, no transformers, no provider SDKs.
#   worker  -> ingestion. Carries the heavy `knowledge` extra (ADR-005).
#   evals   -> CI only.
# Building one fat image would put a multi-gigabyte ML stack on every replica of a
# service that never computes an embedding.

ARG PYTHON_VERSION=3.12

# ----------------------------------------------------------------------- base
FROM python:${PYTHON_VERSION}-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.23 /uv /usr/local/bin/uv

WORKDIR /app

# ------------------------------------------------------------------ deps-api
FROM base AS deps-api
COPY pyproject.toml uv.lock README.md ./
# --frozen: the lockfile is the contract. A build that silently resolves different
# versions than CI tested is not reproducible.
#
# The API retrieves on every request, so it needs the store clients. Built without them
# it fell back to an in-memory vector store -- empty, because the worker writes to Qdrant
# -- and every question came back "no documented information" with only a warning in the
# log. It does NOT get `parsing`: it never opens a PDF, and docling brings torch.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev \
        --extra knowledge --extra memory --extra databases

# --------------------------------------------------------------- deps-worker
FROM base AS deps-worker
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev \
        --extra knowledge --extra parsing --extra memory --extra sources --extra databases

# ------------------------------------------------------------------ runtime
FROM base AS runtime
# UID is fixed so volume ownership is stable across rebuilds and hosts.
RUN groupadd --gid 10001 forge \
 && useradd --uid 10001 --gid forge --create-home --shell /usr/sbin/nologin forge \
 && mkdir -p /var/lib/agent-forge/ledger /data \
 && chown -R forge:forge /var/lib/agent-forge /data /app

# --------------------------------------------------------------------- api
FROM runtime AS api
COPY --from=deps-api --chown=forge:forge /opt/venv /opt/venv
COPY --chown=forge:forge pyproject.toml uv.lock README.md ./
COPY --chown=forge:forge src ./src
COPY --chown=forge:forge configs ./configs
COPY --chown=forge:forge scripts ./scripts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

USER forge
EXPOSE 8080
# Liveness only: readiness depends on other services and would make a healthy container
# restart whenever a dependency blinks.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health/live || exit 1
CMD ["uvicorn", "agent_forge.api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8080", "--no-server-header", "--proxy-headers"]

# ------------------------------------------------------------------- worker
FROM runtime AS worker
COPY --from=deps-worker --chown=forge:forge /opt/venv /opt/venv
COPY --chown=forge:forge pyproject.toml uv.lock README.md ./
COPY --chown=forge:forge src ./src
COPY --chown=forge:forge configs ./configs
COPY --chown=forge:forge scripts ./scripts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
        --extra knowledge --extra memory --extra sources --extra databases

USER forge
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import agent_forge; print('ok')" || exit 1
CMD ["python", "-m", "agent_forge.knowledge.ingestion"]
