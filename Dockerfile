# syntax=docker/dockerfile:1

ARG SOURCE_DATE_EPOCH=0

FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

FROM dhi.io/python:3.15.2-debian13-dev@sha256:c73e2cb9e8d56cfab7f01b99f67b2c830e84e7397dfe666d8bfaf5a69ad0ce85 AS builder

ARG SOURCE_DATE_EPOCH

COPY --from=uv /uv /uvx /usr/local/bin/

ENV SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}" \
    UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# DHI prunes hashed aliases. Regenerate them with the patched upstream policy
# before transferring the complete CA store to the runtime.
RUN update-ca-certificates --fresh

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --no-install-project --no-editable

COPY PACKAGE_DESCRIPTION.md LICENSE ./
COPY src ./src
COPY tools/canonicalize_wheel_records.py /tmp/canonicalize_wheel_records.py
COPY tools/transfer_tree_deterministically.py /tmp/transfer_tree_deterministically.py
COPY tools/transfer_tree_deterministically.py /tmp/tools/transfer_tree_deterministically.py
COPY tools/install_runtime_ca.py /tmp/install_runtime_ca.py

# CPython's bytecode serialization can vary with uv's parallel interpreter state.
# Compile once in a fixed order before normalizing the runtime tree.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --no-editable \
    && PYTHONHASHSEED=0 /app/.venv/bin/python -m compileall -q -f -j 1 \
        --invalidation-mode=checked-hash /app/.venv/lib \
    && python /tmp/canonicalize_wheel_records.py \
        --source-date-epoch "${SOURCE_DATE_EPOCH}" /app/.venv

FROM dhi.io/python:3.15.2-debian13@sha256:c6c9414d78bd285be1a4dacfb94ae61732c77019605b1678792823e319b720f6 AS runtime-base

ARG SOURCE_DATE_EPOCH

ENV SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}" \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

USER 0:0
# Both pinned bases contain the upstream CA fix. Retain the validated overlay
# and builder-generated hashed aliases until their removal is verified separately.
# Replace the complete generated/anchor trees so stale aliases cannot survive.
RUN --mount=type=bind,from=builder,source=/etc/ssl/certs,target=/run/updated-ca/certs,ro \
    --mount=type=bind,from=builder,source=/usr/share/ca-certificates,target=/run/updated-ca/anchors,ro \
    --mount=type=bind,from=builder,source=/etc/ca-certificates.conf,target=/run/updated-ca/ca-certificates.conf,ro \
    --mount=type=bind,from=builder,source=/tmp/tools,target=/tmp/tools,ro \
    --mount=type=bind,from=builder,source=/tmp/install_runtime_ca.py,target=/tmp/install_runtime_ca.py,ro \
    ["/usr/bin/python3.15", "/tmp/install_runtime_ca.py"]

USER 65532:65532

RUN --mount=type=bind,from=builder,source=/app/.venv,target=/tmp/source-venv,ro \
    --mount=type=bind,from=builder,source=/tmp/transfer_tree_deterministically.py,target=/tmp/transfer_tree_deterministically.py,ro \
    ["/usr/bin/python3.15", "/tmp/transfer_tree_deterministically.py", "--uid", "65532", "--gid", "65532", "/tmp/source-venv", "/app/.venv"]

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
