# syntax=docker/dockerfile:1

ARG SOURCE_DATE_EPOCH=0

FROM ghcr.io/astral-sh/uv:0.11.33@sha256:77280f2f771df71f90786c314fe1bbc1e023feac652969bbf139c280babf2eb7 AS uv

FROM dhi.io/python:3.14.6-debian13-dev@sha256:b1b502f0903fb63589bb660570872c94569a2475f3ff53597754257baff8d06c AS builder

ARG SOURCE_DATE_EPOCH

COPY --from=uv /uv /uvx /usr/local/bin/

ENV SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --no-install-project --no-editable

COPY README.md LICENSE ./
COPY src ./src
COPY tools/canonicalize_wheel_records.py /tmp/canonicalize_wheel_records.py

RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --no-editable \
    && python /tmp/canonicalize_wheel_records.py /app/.venv

FROM dhi.io/python:3.14.6-debian13@sha256:f3c4e102e557c0eee652cfd14b7da473c89d9126a07f5b0ebd9f8e79183f4038 AS runtime-base

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --link --from=builder --chown=65532:65532 /app/.venv /app/.venv

USER 65532:65532

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-m", "shittim_chest.healthcheck"]

ENTRYPOINT ["python", "-m", "shittim_chest"]

FROM runtime-base AS production

FROM production AS fault-test

COPY --chown=65532:65532 tests/__init__.py /fault-tests/tests/__init__.py
COPY --chown=65532:65532 tests/fixtures/container_process.py \
    /fault-tests/tests/fixtures/container_process.py

ENV PYTHONPATH=/fault-tests

FROM dhi.io/python:3.14.6-debian13-dev@sha256:b1b502f0903fb63589bb660570872c94569a2475f3ff53597754257baff8d06c AS break-glass-tools

RUN apt-get update \
    && apt-get install --yes --no-install-recommends bsdutils procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/log/apt/* \
    && rm -f /var/log/dpkg.log \
    && command -v /bin/sh \
    && command -v cat \
    && command -v ps \
    && command -v script

FROM break-glass-tools AS break-glass

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --link --from=builder --chown=65532:65532 /app/.venv /app/.venv

USER 65532:65532

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-m", "shittim_chest.healthcheck"]

ENTRYPOINT ["python", "-m", "shittim_chest"]
