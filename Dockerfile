# syntax=docker/dockerfile:1

ARG SOURCE_DATE_EPOCH=0

FROM ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a AS uv

FROM dhi.io/python:3.14.8-debian13-dev@sha256:5b7724d3ca177d536fe88db1de8c900f29671f2cca613358e618567acfe526b2 AS builder

ARG SOURCE_DATE_EPOCH

COPY --from=uv /uv /uvx /usr/local/bin/

ENV SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}" \
    UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# The DHI builder has the Debian fix; its runtime still carries libexpat1 2.8.3.
# APT authenticates the exact package installed below.
COPY tools/install_runtime_expat.py /tmp/install_runtime_expat.py
RUN mkdir /tmp/runtime-package \
    && cd /tmp/runtime-package \
    && apt-get update \
    && apt-get download "libexpat1=$(python /tmp/install_runtime_expat.py --version)" \
    && mv libexpat1_*.deb /tmp/libexpat1.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/runtime-package

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --no-install-project --no-editable

COPY README.md LICENSE ./
COPY src ./src
COPY tools/canonicalize_wheel_records.py /tmp/canonicalize_wheel_records.py
COPY tools/transfer_tree_deterministically.py /tmp/transfer_tree_deterministically.py

# CPython's bytecode serialization can vary with uv's parallel interpreter state.
# Compile once in a fixed order before normalizing the runtime tree.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --no-editable \
    && PYTHONHASHSEED=0 /app/.venv/bin/python -m compileall -q -f -j 1 \
        --invalidation-mode=checked-hash /app/.venv/lib \
    && python /tmp/canonicalize_wheel_records.py \
        --source-date-epoch "${SOURCE_DATE_EPOCH}" /app/.venv

FROM dhi.io/python:3.14.8-debian13@sha256:b2dee88be07689a608f86c392dd863f20399f8ffcbe99eff6bdb4f04485d9298 AS runtime-base

ARG SOURCE_DATE_EPOCH

ENV SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}" \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

USER 0:0
RUN --mount=type=bind,from=builder,source=/usr/bin,target=/run/package-tools/bin,ro \
    --mount=type=bind,from=builder,source=/usr/sbin,target=/run/package-tools/sbin,ro \
    --mount=type=bind,from=builder,source=/usr/lib,target=/run/package-tools/lib,ro \
    --mount=type=bind,from=builder,source=/tmp/libexpat1.deb,target=/run/libexpat1.deb,ro \
    --mount=type=bind,from=builder,source=/tmp/install_runtime_expat.py,target=/tmp/install_runtime_expat.py,ro \
    ["/usr/bin/python3.14", "/tmp/install_runtime_expat.py"]
USER 65532:65532

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
