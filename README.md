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
current evidence is required. Evidence persistence was introduced in schema v3.
STEP-05C adds deterministic post-vote shadow quality signals,
SDK-independent Luna standard, Terra standard, and Luna pro policies, schema v4
persistence, and an explicitly gated blind A/B evaluation tool. Shadow mode
does not make an additional OpenAI request.
STEP-05C.1A hardens that evaluator by separating the scorer artifact from the
policy key and performance metrics, preserving per-policy refusals and
operational failures without aborting the complete run, and producing a
content-free aggregate recommendation. STEP-05C.1B uses preference-only blind
review because requiring 100 independent rubric scores was not operationally
reasonable for one maintainer.

STEP-06A now provides the SDK-independent Discord contract foundation: four
generic Bot slots, fail-closed Guild/channel/Application configuration,
deterministic 2,000-character message chunks, UUIDv7 nonces, content hashes,
versioned control-panel IDs, and a typed outbox boundary. Starter message,
thread, and control-panel message IDs are persisted separately through an
idempotent binding use case, and DynamoDB schema v5 migrates the immediately
previous v4 representation. STEP-06B adds the discord.py 2.7.1 publisher: it
claims only persisted outbox chunks, sends with all mentions disabled and a
nonce that discord.py maps to `enforce_nonce=true`, then conditionally stores
the returned message ID. A reclaimed operation scans the dedicated thread for
the same Bot author, nonce, and content hash before sending, so a Discord
success followed by a DynamoDB completion failure does not immediately create
a duplicate. SDK-exhausted 429/408/409/5xx failures are rescheduled without an
additional in-process retry loop. Every client uses a 30-second maximum SDK
rate-limit wait, and Discord operations have a 45-second timeout within the
shared 60-second outbox claim.

STEP-06C adds the offline Discord interaction runtime. Four independent clients
use only the GUILDS Intent and new debates are accepted only while all four are
READY. The moderator registers a Guild-scoped `/shittim` schema, defers command
and component responses immediately, creates a nonce-protected starter message,
Public Thread, and control panel, and reconciles an interrupted setup from
Discord history before creating another resource. Cancel/retry buttons are
bound to the immutable source attempt and are accepted only after Application,
Guild, thread, panel message, debate, attempt, and actor checks. The controller
owns and awaits its debate tasks, and one client exiting causes all four clients
to close.

STEP-07A adds the process runtime lifecycle. A separate fail-closed admission
gate stays closed through startup until all four Discord identities are READY,
the Guild Command schema is synchronized when needed, and recoverable debates
are started as an owned task. SIGINT and SIGTERM immediately close admission
and interaction dispatch; owned debate/recovery tasks are cancelled and awaited
so the existing application checkpoint contract runs. One Bot disconnect closes
admission immediately, checkpoints after a 60-second continuous outage, and
automatically resumes after all four identities return READY without marking a
connectivity failure as a failed debate. Graceful cleanup has a 90-second
application deadline, leaving 30 seconds below the planned Fargate
`stopTimeout=120` for client, logging, and container-runtime shutdown.

STEP-07B connects persisted outbox recovery to every leased debate before phase
work resumes. Pending chunks are read with a strongly consistent, paginated
DynamoDB Query and drained in persisted order. Future retry times and live claim
expiries are awaited without busy looping while the lease heartbeat continues.
Retryable Discord failures use the publisher's persisted reschedule; permanent
delivery conflicts fail with the stable Discord code. Cancellation stops new
delivery immediately and leaves the outbox for the next fenced owner. Outbox
waiting is excluded from the debate's active-processing deadline.

STEP-07C adds the production composition root and the executable
`python -m shittim_chest` entry point. Strict Pydantic configuration validates
the generic runtime and four private persona payloads before any SDK client is
created. The process then owns one reusable DynamoDB client, one reusable
OpenAI client and shared limiter, and exactly four Discord clients. Startup and
runtime failures emit stable, content-free error codes; shutdown closes all
owned clients deterministically. `.env.example` documents names and generic
shapes only and contains no production identifiers or credentials. Real
subprocess tests verify graceful SIGTERM checkpoint/cleanup and SIGKILL
replacement-process recovery.

Production is fixed to Luna standard for every generation phase. Terra standard
and Luna pro remain evaluation-only policies and cannot be selected by runtime
configuration or Discord operations. The shadow assessment remains observable
with `executed=false` and never causes another model request. Response variety
comes from three private, versioned persona prompts with distinct practical,
verification/safety, and creative/alternative lenses while sharing the same
evidence, safety constraints, and structured-output schema.

The Discord interaction, lifecycle, outbox-recovery, production-composition,
and process-signal runtimes are implemented and offline-tested. STEP-08A adds a
digest-pinned multi-stage production container, a separately selectable
break-glass target, numeric non-root execution, and a content-free event-loop
heartbeat health check. The local amd64 security boundary has been validated
with a read-only root filesystem, a writable temporary mount, all Linux
capabilities dropped, and no-new-privileges. They have not been connected to
real Bot tokens or external services. Native ARM64 CI, container fault
injection, final-image SBOM, Discord Applications, CDK/AWS resources, and
production workflows remain for later slices. Responses API
Multi-agent beta is intentionally not used; Python application orchestration
remains the authority for persona concurrency, voting, checkpoints, and resume.
No production AWS or OpenAI service is contacted by the current tests.

Build the two local image targets with Docker-format metadata so the health
configuration is retained:

```sh
podman build --format docker --target production -t shittim-chest:production .
podman build --format docker --target break-glass -t shittim-chest:break-glass .
```

The optional paid evaluator requires both `--live` and `OPENAI_API_KEY`, writes
the scorer artifact and unblinding key to separate repository-external directory
trees, and is not run by CI. Its model prices must be supplied at execution
time rather than being hard-coded. After blind scoring, `tools/score_escalation.py`
supports the original five-axis rubric and the default operator-friendly
`--preference-only` workflow. Run `python -m tools.review_escalation` to review
one case at a time, save after every A/B/tie choice, and resume safely. In
preference-only mode the recommendation uses blind preference wins first, then
cost and p95 latency when preferences tie.

The 2026-07-17 STEP-05C.1B run completed 10 cases and 20 successful policy
runs. Blind preference review produced 4 wins for Luna pro, 2 wins for Terra
standard, and 4 ties. The operator subsequently chose the simpler production
policy: Luna standard only, with no escalation. The result is retained solely
as evaluation history.

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
