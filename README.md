# The Shittim Chest

The Shittim Chest is a Discord multi-agent debate Bot under incremental
development. A moderator
coordinates three configurable participant slots, shared evidence, revised
proposals, anonymous voting, and a mechanically calculated result through the
`/shittim` command.

## Status

Requirements and detailed design are available in the
[design-document mirror](https://github.com/pitekusu/shittim-chest/tree/main/docs).
The Python 3.14.6/uv project foundation, UUIDv7 debate/attempt identifiers,
immutable phase and Spot-recovery state machine, retry attempt boundary, and
domain tests are implemented.
Application use cases, external adapters, Discord Applications, AWS resources,
containers, and production workflows have not been implemented yet.

The planned runtime uses Python, Discord, the OpenAI Responses API, DynamoDB,
and one ARM64 ECS Fargate Spot task in the Tokyo Region. Fargate Spot
interruption is handled with checkpoints, fenced leases, and an outbox.

## Local validation

Install uv 0.11.29, then run:

```sh
uv lock --check
uv sync --frozen --all-groups
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy
uv run --frozen pytest
uv export --quiet --frozen --all-groups --no-emit-project --no-annotate \
  --output-file /tmp/shittim-chest-audit-requirements.txt
uv run --frozen pip-audit --strict --require-hashes \
  --requirement /tmp/shittim-chest-audit-requirements.txt
uv build --no-sources
```

The lock file selects Python 3.14.6 through `.python-version`. The domain has no
runtime dependency outside the Python standard library.

## Public and private boundaries

This repository contains generic slots and public-safe design information only.
Production Guild/channel/Application IDs, display names, persona prompts, Bot
tokens, and API keys are not stored here. Runtime configuration is designed to
be loaded from versioned AWS Systems Manager Parameter Store parameters.

Report security issues through GitHub's private vulnerability reporting rather
than a public Issue. See the
[security policy](https://github.com/pitekusu/shittim-chest/blob/main/SECURITY.md).

## Contributing and license

Implementation contributions are welcome; read the
[contribution guide](https://github.com/pitekusu/shittim-chest/blob/main/CONTRIBUTING.md).
Source code, infrastructure as code, tools, and samples are licensed under the
MIT License. Design documents and `AGENTS.md` are excluded; see
[the license scope](https://github.com/pitekusu/shittim-chest/blob/main/LICENSE-SCOPE.md).
