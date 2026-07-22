# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv

FROM dhi.io/python:3.14.6-debian13-dev@sha256:1df3badfd28c3fd54fb8371d55a4a050c4051b8a808f8367f7241442a334928b AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --no-install-project --no-editable

COPY README.md LICENSE ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --no-editable

FROM dhi.io/python:3.14.6-debian13@sha256:c43e37b1d2c740bf924149f7ce015a79636a084a3fd755ac8c5ffc2f4a850b3e AS runtime-base

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder --chown=65532:65532 /app/.venv /app/.venv

USER 65532:65532

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-m", "shittim_chest.runtime.health"]

ENTRYPOINT ["python", "-m", "shittim_chest"]

FROM runtime-base AS production

FROM production AS fault-test

COPY --chown=65532:65532 tests/__init__.py /fault-tests/tests/__init__.py
COPY --chown=65532:65532 tests/fixtures/container_process.py \
    /fault-tests/tests/fixtures/container_process.py

ENV PYTHONPATH=/fault-tests

FROM dhi.io/python:3.14.6-debian13-dev@sha256:1df3badfd28c3fd54fb8371d55a4a050c4051b8a808f8367f7241442a334928b AS break-glass

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder --chown=65532:65532 /app/.venv /app/.venv

RUN apt-get update \
    && apt-get install --yes --no-install-recommends bsdutils procps \
    && rm -rf /var/lib/apt/lists/* \
    && command -v /bin/sh \
    && command -v cat \
    && command -v ps \
    && command -v script

USER 65532:65532

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-m", "shittim_chest.runtime.health"]

ENTRYPOINT ["python", "-m", "shittim_chest"]
