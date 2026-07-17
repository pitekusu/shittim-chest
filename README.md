# The Shittim Chest

[![CI](https://github.com/pitekusu/shittim-chest/actions/workflows/ci.yml/badge.svg)](https://github.com/pitekusu/shittim-chest/actions/workflows/ci.yml)
[![Release Tool Versions](https://github.com/pitekusu/shittim-chest/actions/workflows/tool-versions.yml/badge.svg)](https://github.com/pitekusu/shittim-chest/actions/workflows/tool-versions.yml)

The Shittim Chest is a Discord multi-agent debate Bot under incremental
development. A moderator
coordinates three configurable participant slots, shared evidence, revised
proposals, anonymous voting, and a mechanically calculated result through the
`/shittim` command.

## Status

Requirements and detailed design are available in the
[design-document mirror](https://github.com/pitekusu/shittim-chest/tree/main/docs).
The Python 3.14.6/uv project foundation, UUIDv7 debate/attempt identifiers,
immutable phase and Spot-recovery state machine, deterministic voting, and the
SDK-independent application core are implemented. STEP-04B also provides the
concrete DynamoDB persistence adapter: transactional acceptance/retry/terminal
updates, three fenced lease slots, 20-second lease renewal, strongly consistent
operation-result idempotency, recoverable-session pagination, and a durable
outbox. The application layer exposes typed accept/run/cancel/retry/resume use
cases through Protocol boundaries and fake-based async tests. Pull requests are
checked with read-only GitHub
Actions jobs for quality, tests, security, packaging, source SBOM, and public
documentation safety. GitHub's managed Dependency Graph is compared with the
tested uv inventory on a weekly schedule. Betterleaks scans the complete Git
history; release assets are digest-pinned and its checksums are verified with
Sigstore. The persistence suite runs against digest-pinned DynamoDB Local and
SDK Stubber. STEP-05A adds an OpenAI adapter for initial opinions, final
proposals, voting, and final decisions using the stable Responses API, strict
Pydantic schemas, private persona configuration, bounded concurrency,
content-free usage records, and domain-safe error mapping. Its contract tests
use the official SDK with a mock HTTP transport and make no paid API calls.
STEP-05B adds a fail-safe deterministic question router and hosted
Responses API Web search boundary. It classifies searches as none, optional,
or required, defaults unknown wording to optional search, and stores the router
version/reason with one immutable summary and source set for every participant;
persists optional search failure while continuing; and fails closed when
current evidence is required. Evidence persistence uses schema v3 with v2
reader migration.

Luna-to-Terra escalation, the Discord adapter, runtime recovery wiring,
Discord Applications, containers, CDK/AWS resources,
and production workflows have not been implemented yet. Responses API
Multi-agent beta is intentionally not used; Python application orchestration
remains the authority for persona concurrency, voting, checkpoints, and resume.
No production AWS or OpenAI service is contacted by the current tests.

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
uv run --frozen lint-imports
uv run --frozen pytest
uv run --frozen python tools/check_public_surface.py
uv run --frozen python -m tools.check_docs
uv run --frozen python tools/check_tool_versions.py validate \
  .github/tool-versions.json
uv export --quiet --frozen --all-groups --format cyclonedx1.5 \
  --output-file /tmp/shittim-chest-source-sbom.cdx.json
uv run --frozen python tools/check_sbom.py validate \
  /tmp/shittim-chest-source-sbom.cdx.json
uv export --quiet --frozen --all-groups --no-emit-project --no-annotate \
  --output-file /tmp/shittim-chest-audit-requirements.txt
uv run --frozen pip-audit --strict --require-hashes \
  --requirement /tmp/shittim-chest-audit-requirements.txt
uv build --no-sources
```

The weekly `Release Tool Versions` workflow compares the actionlint and
Betterleaks pins with their latest stable GitHub releases. It
reports drift but never updates or merges a version automatically.

The lock file selects Python 3.14.6 through `.python-version`. The domain remains
standard-library-only; boto3 is isolated at the DynamoDB adapter boundary and
synchronous SDK calls are moved off the event loop.

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
