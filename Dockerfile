# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv

FROM docker.io/library/python:3.14.6-slim-trixie@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015 AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_NO_DEV=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable

FROM docker.io/library/python:3.14.6-slim-trixie@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015 AS runtime-base

ARG APP_GID=10001
ARG APP_UID=10001

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid "${APP_GID}" shittim \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin shittim \
    && install --directory --owner "${APP_UID}" --group "${APP_GID}" --mode 0755 /app

WORKDIR /app

COPY --from=builder --chown=${APP_UID}:${APP_GID} /app/.venv /app/.venv

USER ${APP_UID}:${APP_GID}

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-m", "shittim_chest.runtime.health"]

ENTRYPOINT ["python", "-m", "shittim_chest"]

FROM runtime-base AS production

FROM production AS fault-test

USER root

COPY --chown=${APP_UID}:${APP_GID} tests/__init__.py /fault-tests/tests/__init__.py
COPY --chown=${APP_UID}:${APP_GID} tests/fixtures/container_process.py \
    /fault-tests/tests/fixtures/container_process.py

ENV PYTHONPATH=/fault-tests

USER ${APP_UID}:${APP_GID}

FROM runtime-base AS break-glass

USER root

RUN apt-get update \
    && apt-get install --yes --no-install-recommends bsdutils procps \
    && rm -rf /var/lib/apt/lists/* \
    && command -v /bin/sh \
    && command -v cat \
    && command -v ps \
    && command -v script

USER ${APP_UID}:${APP_GID}
