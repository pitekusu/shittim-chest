# syntax=docker/dockerfile:1

ARG SOURCE_DATE_EPOCH=0

FROM ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 AS uv

FROM dhi.io/python:3.14.7-debian13-dev@sha256:3b7b01e1377d7462bb50f450d795340daff1a4987c0a37ab5a94816f11c6e309 AS builder

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
COPY tools/transfer_tree_deterministically.py /tmp/transfer_tree_deterministically.py

RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --no-editable \
    && python /tmp/canonicalize_wheel_records.py \
        --source-date-epoch "${SOURCE_DATE_EPOCH}" /app/.venv

FROM dhi.io/python:3.14.7-debian13@sha256:b7f0db069020910078cdd92f26326ec4b07c3d746e865b1a8aaf16a22b62e55e AS runtime-base

ARG SOURCE_DATE_EPOCH

ENV SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}" \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN --mount=type=bind,from=builder,source=/app/.venv,target=/tmp/source-venv,ro \
    --mount=type=bind,from=builder,source=/tmp/transfer_tree_deterministically.py,target=/tmp/transfer_tree_deterministically.py,ro \
    ["/usr/bin/python3.14", "/tmp/transfer_tree_deterministically.py", "--uid", "65532", "--gid", "65532", "/tmp/source-venv", "/app/.venv"]

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
