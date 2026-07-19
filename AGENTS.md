# Repository Guidelines

## Project Overview

This repository contains the Discord multi-agent debate Bot project
`shittim_chest` (シッテムの箱; official English name: The Shittim Chest).
The planned system runs four Discord Bot
accounts: one orchestration Bot and three AI persona Bots. The persona Bots
produce initial opinions, revised proposals, and votes; the orchestration Bot
manages the workflow and publishes the mechanically calculated result.

The requirements, basic design, and detailed design are complete. The
Python/uv project foundation, initial domain state machine, and STEP-02 GitHub
quality/supply-chain gates are implemented. The STEP-02B Betterleaks migration
gate and STEP-02C Gitleaks retirement are complete. STEP-03 application core was
merged through PR `#15` with voting rules, Protocols,
accept/run/cancel/retry/resume use cases, deadlines, checkpoint-aware
cancellation, and fake-based async tests. STEP-04A persistence contracts were
merged through PR `#16`: Guild/channel and
operation identity preservation, fenced lease types, idempotent repository
operations, vertically partitioned DynamoDB-native records, schema v1-to-v2
up-conversion, and outbox/panel serialization. STEP-04B was squash-merged through
PR `#18` as commit `9aafe6e` with boto3 transactions, three lease slots, durable
operation results, outbox state changes, GSI pagination, DynamoDB Local, and SDK
stub tests. The PR checks and the merge commit's CI, CodeQL, and managed
Dependency Graph run all passed.
STEP-05A was merged through PR `#20` as commit `d6ea561`. STEP-05B was merged
through PR `#21` as commit `44a35fa`. It adds fail-safe
`question-router-v2`, hosted Responses API Web search, immutable source-backed
Evidence, optional/required failure semantics, and the schema v3 Evidence
migration. The Responses API Multi-agent beta is explicitly not used.
STEP-06A was squash-merged through PR `#27` as commit `47af41f` and adds
SDK-independent Discord Bot slots and fail-closed runtime
configuration, stable Discord error codes, deterministic message chunking,
UUIDv7 nonces, content hashes, a versioned panel custom-ID codec, and an
application-owned outbox Protocol. Starter message, thread, and control panel
message IDs are separate and can be bound only while `ACCEPTED`; identical
replay is idempotent and rebinding is rejected. DynamoDB schema v5 migrates the
immediately previous v4 record and maps old `bot_id` to generic `bot_slot`.
Local validation passed 221 tests with 5 opt-in skips and 92.70%
domain/application line/branch coverage. STEP-06B was squash-merged through PR
`#30` as commit `96a1ace` with discord.py 2.7.1, fenced outbox publication,
safe allowed mentions, enforced nonce delivery, SDK-owned rate-limit handling,
and history reconciliation.
Each client must use `max_ratelimit_timeout=30`; Discord delivery is bounded to
45 seconds, shorter than the shared 60-second outbox claim. STEP-06C was
squash-merged through PR `#31` as commit `9799cb9`, with four GUILDS-only
clients, a four-READY
acceptance gate, Guild-scoped `/shittim`, immediate ephemeral defer,
starter/Public Thread/control panel provisioning and reconciliation,
attempt-bound cancel/retry, and owned debate tasks. Live Discord operations,
restart recovery composition, and Discord Applications remain unimplemented.
STEP-08A implements the local production/break-glass container foundation and
event-loop heartbeat health check. STEP-08B implements native ARM64 CI,
container fault injection, and the image SPDX SBOM. AWS resources and Discord
Applications are not yet implemented.
Approved decisions are recorded in the project index and decision record; do
not silently promote historical options to requirements.

## Current Implementation Boundary

STEP-01, the Python domain foundation, was squash-merged through PR `#1` as
commit `7fa642e` on 2026-07-17. Treat this as a rules-and-state foundation, not
as a runnable Discord Bot or a production-ready service.

The implemented domain foundation is responsible for:

- reproducible Python 3.14.6 and uv project metadata, dependency locking,
  source/wheel packaging, and typed-package markers;
- UUIDv7 `DebateId` values for one logical debate and UUIDv7 `AttemptId` values
  for each immutable execution or retry attempt;
- the only valid debate phase order, with exactly 21 allowed normal,
  cancellation, and failure edges;
- rejection of phase skipping, reverse/self transitions, terminal-state
  transitions, invalid UTC timestamps, and invalid schema versions;
- Spot interruption checkpoints represented separately as
  `recovery_state=checkpointed`, with resume at the same phase;
- immutable failure history: a retry creates a new attempt in the same debate,
  links it with `retry_of`, and starts from the recorded `failed_from_phase`;
- stable domain errors for invalid phase, recovery, and retry operations; and
- deterministic unit/property tests that do not require Discord, OpenAI, AWS,
  or network access.

The domain foundation does not yet orchestrate a full debate, calculate votes,
post Discord messages, call OpenAI, persist DynamoDB records, acquire leases,
publish an outbox, handle real process signals, build a container, or provision
AWS resources. Those capabilities must be added in later isolated slices and
must depend on the domain rules rather than reimplementing them in adapters.

The STEP-01 acceptance snapshot is 66 passing tests, 100% domain line/branch
coverage, Ruff and type-check success, zero known locked-dependency
vulnerabilities, a clean public-surface scan, and successful GitHub-managed
CodeQL and GitGuardian checks.

STEP-02 was squash-merged through PR `#10` as commit `e2fdaad` on 2026-07-17.
Its Pull Request and first `main` run passed, the managed CycloneDX/SPDX comparison
passed, and the active main Ruleset now requires five strict GitHub Actions checks
(`quality`, `tests`, `security`, `package`, `docs-public-safety`) plus CodeQL results
with high-or-higher security alerts blocking merge. STEP-02 provides strict
CycloneDX 1.5 schema and `uv.lock` inventory validation, a 30-day source-SBOM
artifact, Dependency Review, pinned actionlint, isolated wheel
installation, and a weekly/manual comparison with GitHub's managed SPDX 2.3
export. GitHub's Python Dependabot graph job already supplies the complete uv
dependency snapshot, so STEP-02 intentionally does not submit a higher-priority
custom snapshot or grant `contents: write`.

STEP-02B established the Betterleaks migration gate while keeping the required
`security` check name stable. Betterleaks release
checksums are verified with a pinned Sigstore verifier, certificate identity,
OIDC issuer, and immutable digests. Betterleaks must pass generated positive
and negative contract histories with redaction enabled. Do not enable
Betterleaks provider validation, because detected candidates must not be sent to
external APIs. A weekly read-only workflow detects newer actionlint and
Betterleaks releases without applying them automatically. STEP-02C retired
Gitleaks after the parallel PR/main observation, generated contract, full-history
scan, Sigstore verification, and latest-release workflow all passed. Reintroduce
a second scanner only through a later ADR with a concrete coverage gap.

STEP-03 was squash-merged through PR `#15` as commit `34ccc54` on 2026-07-17.
STEP-04A was squash-merged through PR `#16` as commit `54948d7` on 2026-07-17.
Its serializer remains SDK-independent. STEP-04B adds boto3 1.43.50 at the
adapter boundary, marshals validated native values explicitly, executes all SDK
calls in worker threads, and uses a strongly readable `OPERATION#<id>/RESULT`
item rather than a GSI as the idempotency authority. Local validation passed
163 tests with 92.40% domain/application line/branch coverage using pinned
DynamoDB Local 3.3.0 plus SDK Stubber. STEP-04B was squash-merged through PR
`#18` as commit `9aafe6e` on 2026-07-17. The merge commit passed project CI run
`29553563948`, CodeQL run `29553563821`, and managed Dependency Graph run
`29553565579`; no production AWS resource or credential was used.

STEP-05A was merged through PR `#20` as commit `d6ea561` and uses
`openai` 2.46.0, `httpx` 0.28.1, and Pydantic 2.13.4; stable
`AsyncOpenAI.responses.parse()` calls have `store=False`, no tools, and no beta
Multi-agent field or header. Official-SDK mock transport tests cover all four
generation operations, refusal, incomplete and invalid output, rate limiting,
authentication failure, request privacy, usage telemetry, and domain
revalidation. STEP-05B adds `question-router-v2` without another model call.
Unknown or unmatched expressions default to optional search; only explicit
timeless/creative patterns use no search. Persist the router rules version and
stable routing reason with Evidence so misclassifications can become tests.
One hosted Web search Responses request is used only for optional/required
routes, with `tool_choice=required`, `max_tool_calls=4`, source inclusion,
`store=false`, and the same immutable Evidence for all participants. Optional
failure is persisted and continues; required failure becomes
`required_evidence_unavailable`. Evidence META is schema v3 and its reader
migrates the immediately previous v2. STEP-05C was merged through PR `#22` as
commit `a6f43cb` and adds shadow-only deterministic escalation signals, immutable Luna/Terra/pro generation policies,
schema v4 persistence, and an opt-in blind A/B evaluator. Production is fixed
to `PRODUCTION_POLICY=luna_standard`; Terra standard and Luna pro are
evaluation-only and must not be selectable from runtime configuration or
Discord operations. STEP-05C.1A was merged through PR `#24` as commit `1360411` and adds
separate scorer/key output trees, safe per-run failure capture, strict
human-score validation, and content-free policy aggregation.
STEP-05C.1B paid blind answer generation was explicitly approved and completed
on 2026-07-17 with 10 cases and 20 successful answers. Human review uses one
A/B/tie preference per case; do not require the impractical 100-value rubric
for this operator evaluation. `tools/review_escalation.py` must preserve blind
model identity, save after every choice, and support resume. Policy aggregation
uses preference wins first and cost then p95 latency only for a preference tie.
The completed blind review produced Luna pro 4 wins, Terra standard 2 wins, and
4 ties. The operator chose Luna standard only for production and no escalation.
Keep the result as evaluation history; do not implement thresholds, extra
token/deadline limits, or escalation UI. Discord integration and CloudWatch
emission remain out of scope.
STEP-06A relocates SDK-independent outbox/panel records from the DynamoDB
adapter to the application layer so future Discord and DynamoDB adapters do not
depend on each other. The `DiscordOutboxRepository` Protocol owns the delivery
boundary. `bind_discord_context` persists starter, thread, and control-panel
IDs as three distinct fields only before debate work begins. Schema v5 reads
only the immediately previous v4 and fails closed on unknown versions.
STEP-06B adds `DiscordPyPublisher`. It requires exactly four distinct clients,
publishes only a persisted and fenced operation, uses
`discord.AllowedMentions.none()` and a nonce, and relies on discord.py 2.7.1 to
emit `enforce_nonce=true` and honor `Retry-After`. It does not implement a
second request retry loop. Reclaimed deliveries scan at most 500 messages after
the outbox creation time and adopt the oldest exact Bot-author/nonce/content
match. A same-nonce content mismatch fails closed. Locked threads are never
unlocked automatically. Configure all clients with
`max_ratelimit_timeout=30`; the adapter's 45-second Discord-operation timeout
must remain shorter than `OUTBOX_CLAIM_SECONDS=60` so a blocked SDK wait cannot
outlive claim ownership. The publisher contract receives the expected leased
`DebateSnapshot` because an operation ID is scoped to its debate attempt.

STEP-06C adds `DiscordInteractionController`, `DiscordPyGateway`, and
`DiscordClientSupervisor`. Build all four clients with GUILDS-only Intent and
never read Bot tokens in the client builder. Defer every command/component
response before validation or persistence. Register `/shittim` only in the
configured Guild and sync only when the deploy-provided command schema hash
changes. Provision starter, Public Thread, and panel with mentions disabled;
reconcile an interrupted setup by Bot author, nonce, and exact content before
creating another resource. Bind control operations to the source AttemptId and
verify Application, Guild, thread, panel message, debate, attempt, and actor
before invoking a use case. The controller must own, cancel, and await all
background debate tasks. On Python 3.14 with discord.py 2.7.1, do not use
`Client.event()` for this listener because it reaches a deprecated asyncio API
while tests treat warnings as errors; use the dedicated moderator client's
explicit `on_interaction` dispatch.

STEP-07A was squash-merged through PR `#33` as commit `0f386f5` and adds
`RuntimeAdmissionGateway`, `RuntimeLifecycle`, and
`UnixSignalHandlers`. Construct `DebateApplication` with the process admission
gateway, not the physical `DiscordPyGateway`, so startup and shutdown remain
fail closed. Keep admission closed until all four clients are READY, command
schema sync has succeeded, and startup `resume_recoverable` is owned. A single
Bot disconnect closes admission immediately; after 60 continuous seconds,
cancel and await interaction/recovery tasks so the existing application
cancellation contract checkpoints them. Do not convert connectivity loss to
FAILED. Resume recoverable work and reopen admission only after all four clients
return READY. SIGINT/SIGTERM must synchronously close admission and interaction
dispatch, then perform bounded asynchronous cleanup. The application deadline
is 90 seconds, leaving 30 seconds below ECS `stopTimeout=120` for client, log,
and container-runtime exit.

STEP-07B adds `DiscordOutboxRecovery` and requires `DebateApplication` to drain
pending outbox operations before phase work resumes. Use a strongly consistent,
paginated base-table Query to list every unsent operation, including future
retry and unexpired-claim records. Wait for persisted availability without busy
looping while the 20-second lease heartbeat continues. Do not count outbox wait
against the 300-second active-processing deadline. Retryable Discord failures
must reuse the publisher's persisted reschedule; non-retryable failures preserve
the stable Discord code. A `RepositoryConflict` means this worker lost fencing
and must not terminalize the attempt. Cancellation must stop delivery and leave
the record for a later fenced owner. STEP-07B was merged in PR #34 at commit
`04bbda0`; main CI and CodeQL passed.

STEP-07C adds the only production composition root in `bootstrap.py`, the
`python -m shittim_chest` entry point, strict fail-closed runtime/persona
configuration, content-free telemetry, and process-scoped runtime primitives.
Validate configuration before creating any SDK client. Keep one reusable
DynamoDB client, one reusable `AsyncOpenAI` client with one shared limiter, and
exactly four Discord clients. Runtime configuration must not select a model;
production remains fixed to Luna standard. Close Discord, OpenAI, and DynamoDB
clients deterministically and idempotently. Real subprocess tests must keep
covering SIGTERM checkpoint/cleanup and SIGKILL replacement-process recovery.
STEP-07C was merged in PR #35 at commit `e863ae3`; PR and main CI/CodeQL passed.

STEP-08A adds the digest-pinned Python 3.14.6/uv multi-stage `Dockerfile`, a
numeric UID/GID 10001 production runtime, a separately selectable break-glass
target, and an event-loop heartbeat health command. Validate image security
locally with a read-only root filesystem, a writable `/tmp`, all capabilities
dropped, and no-new-privileges.
STEP-08A was merged in PR #37 at commit `7742f0b`; PR and main CI/CodeQL and
the initial Docker Dependabot update run passed.

STEP-08B adds the native `ubuntu-24.04-arm` `container-arm64` check. It builds
the production and CI-only fault targets, validates image configuration and
runtime security, injects SIGTERM at all seven non-terminal phases, and injects
SIGKILL before/after the transaction and Discord-post boundaries. Replacement
must leave exactly one transaction event, one content-hash-matched Discord
message, and a completed outbox. Syft is version/digest pinned, monitored by the
weekly release-tool workflow, and emits a 30-day SPDX JSON artifact containing
both Debian OS and production Python packages. The fault fixture is copied only
into the `fault-test` target; never push or deploy that target. Fargate
task-definition settings and release attestations remain STEP-09/10 work.
STEP-08B was merged in PR #39 at commit `2cca51a`; PR/main native ARM64 CI,
CodeQL, image SPDX validation, and the `container-arm64` Ruleset requirement
passed. The first PR run exposed a Python 3.12 host/Python 3.14 domain import;
keep the host-side gate standard-library-only and preserve the unit assertion
that its phase list matches the domain state machine.

The production builder must keep locked dependency installation in a layer
before application source is copied. Use `uv sync --frozen --no-dev
--no-install-project --no-editable` for that layer, then copy `README.md`,
`LICENSE`, and `src/` and run the final frozen non-editable sync. Mount
`/root/.cache/uv` as a BuildKit cache with `sharing=locked`; do not set
`UV_NO_CACHE=1` for the build. Keep `UV_PYTHON_DOWNLOADS=0` because the pinned
Python base image is the only permitted interpreter source. A cache miss must
change build time only, never the resulting dependency versions or gate result.
In GitHub Actions, set up one Buildx builder and use full-SHA-pinned official
`docker/build-push-action` steps with `load: true` so the later runtime and Syft
checks inspect the exact images that were built. Export only the production
`mode=max` cache to the `container-arm64-production` GHA scope, with cache
export failure ignored because it is an optimization. Do not push either the
production test image or the CI-only fault image from Pull Request CI. Keep
the workflow at `contents: read` with no secret or OIDC access. Retain the
Buildx summaries and diagnostic build records for 30 days, matching the test
image SBOM retention period.
Update this section and `20_実装・試験・検証記録.md` after each later slice so
the boundary does not become stale.

## GitHub Tooling Policy

For `pitekusu/shittim-chest`, use the connected GitHub App only for read-only
repository, Pull Request, review, comment, and status inspection. Its current
token can read this public repository but GitHub rejects Pull Request creation
and merge with `403 Resource not accessible by integration`; there is no
user-approvable write-permission update for the installed App.

Use the authenticated GitHub CLI (`gh`) as the default path for every GitHub
write operation in this repository, rather than first attempting the GitHub App
and falling back after a predictable 403. This repository-specific rule
overrides connector-first plugin guidance for write actions only.

- Treat GitHub authentication verification as a mandatory preflight for every
  write workflow. Run both `gh auth status` and
  `gh api user --jq '.login'` on the host, outside the restricted sandbox, and
  require both commands to succeed with the active account `pitekusu`. Never
  print or persist the token.
- Do not diagnose an expired GitHub credential from a sandboxed `gh` failure.
  Restricted network, DNS, keyring, or API access can make a valid login appear
  invalid. Retry the two-command preflight on the host and distinguish transport
  errors from authentication errors. Request reauthentication only when the
  host-side check reports an invalid credential, `401`, or `Bad credentials`.
- Use local `git` for branch creation, explicit staging, commit, and push. Use
  `gh` for Pull Request creation/update, ready state, comments, labels, checks,
  review metadata, and merge actions.
- Keep `main` protected: publish through a Pull Request, use squash merge, and
  verify required checks and unresolved review threads before merging.
- Bind merge operations to the inspected head with
  `gh pr merge --match-head-commit <full-sha>` and delete the remote feature
  branch after a successful merge.
- Continue to prefer GitHub-managed CodeQL, Secret scanning, Dependabot, and
  other managed services; this CLI policy changes only the control path used by
  Codex for GitHub mutations.

## Authoritative Documents

The 14 public-safe project notes in the operator's Obsidian Vault are the source
of truth. Set `SHITTIM_DOCS_SOURCE` to that directory; never commit its local
absolute path. Repository `docs/` is the public, read-only mirror.

Use the document responsibilities recorded in the project index: requirements
define what must be satisfied, decisions record rationale and accepted risk,
detailed designs define implementable interfaces, tests define pass/fail, and
traceability links them. There is no silent precedence rule. Stop on a conflict,
record it in an ADR, and update every affected note in the same change.

Do not edit a mirrored note directly. Run
`python tools/sync_docs.py --write --source "$SHITTIM_DOCS_SOURCE"` after
editing Obsidian and the matching `--check` command before commit. Production
identifiers and persona configuration belong in a separate, non-mirrored
operator note and versioned SSM parameters.

After meaningful architecture changes, implementation milestones, validation
results, or operational findings, update the project hub and relevant design
note so the external build memo stays current. Never put secrets in Obsidian.

After every implementation slice, append a record to
`20_実装・試験・検証記録.md`. Include the slice ID and scope, branch/base,
tool versions, exact validation commands, summarized results, coverage,
security/package evidence, exclusions, unresolved findings, and PR/CI evidence
when available. Record failed and superseded runs instead of silently replacing
them. Never include credentials, private runtime configuration, Discord user
content, or raw model output.

## Approved MVP Decisions

- Prioritize a fun, coherent Discord presentation; do not claim that three
  personas prove an answer is more accurate than one model.
- Run in one private Guild and only in configured channels. Any member of an
  allowed channel may start a debate.
- Model the four Discord identities as `moderator`, `participant-a`,
  `participant-b`, and `participant-c`. Runtime display names and prompts are
  private deployment configuration. The common final message must say that
  output is AI-generated and is not a guarantee of correctness or professional
  advice.
- Use `/shittim question:<question>` to start a public-thread debate. Show
  progress and provide cancel/retry actions through the thread control panel.
- Include OpenAI Web search in the MVP and give every persona the same immutable
  Evidence bundle. Required-search failure fails the session; optional-search
  failure continues with a notice.
- Evaluate final proposals anonymously, publish votes only after all votes are
  complete, and let the winner generate the constrained final decision.
- Target 180 seconds of active processing with a 300-second hard limit. Allow
  three concurrent sessions and 30 starts per Guild per day.
- Persist debate records and Discord threads without automatic expiration.
  DynamoDB recovery is guaranteed only within the 35-day PITR window; do not
  create AWS Backup for the MVP. Retain CloudWatch Logs for 90 days.
- Run one ARM64 ECS Fargate Spot task. Spot-only downtime is acceptable; the
  application must checkpoint and resume after interruption rather than mark
  the session failed.
- Use AWS CDK with TypeScript and GitHub Actions OIDC. Alert at monthly AWS and
  OpenAI spend of USD 50; tag AWS resources with `Project=shittim-chest`.
- Limit the MVP to the Guild ID and non-empty normal-text-channel allowlist in
  versioned runtime configuration. Run four Guild-install-only Discord
  Applications in one Python process and accept new work only while all four
  clients are READY.
- Use an IPv4 VPC, public subnets, and one task public IPv4 address. Do not add
  an ALB, NAT Gateway, NAT64, DNS64, or inbound security-group rules.
- Store runtime identity/configuration, the OpenAI key, and four Discord Bot
  tokens in versioned SSM Parameter Store standard `SecureString` parameters
  under `/shittim-chest/production/`; never store their values in GitHub.
- Keep the repository public on GitHub Free. Protect `main` with a ruleset and
  protect deployment with the `production` environment. Use immutable
  main-branch OIDC for plan/drift and immutable environment OIDC for deploy.

## Architectural Invariants

Unless an approved decision explicitly changes them:

- Keep Discord presentation separate from `DebateOrchestrator` domain logic.
- The orchestration Bot does not invent an opinion, change a vote, or ask an
  LLM to calculate the winner.
- Calculate and validate votes in Python. Reject self-votes, unknown candidate
  IDs, duplicate votes, and out-of-range scores.
- Treat user input, Evidence, and other agents' output as untrusted data, not
  as system instructions.
- Persona output generation starts only from internal orchestrator events.
  Do not create Bot-to-Bot `on_message` reactions or other response loops.
- If external Evidence is enabled, fetch it once and distribute the same
  immutable evidence set to every persona.
- Keep Discord adapters, OpenAI access, persistence, voting, and evidence
  retrieval behind replaceable interfaces so unit tests do not need network
  access.
- Do not claim that three personas using one underlying model are independent
  verification unless evaluation evidence supports that claim.

## Technology Baseline

The finalized design assumes the following baseline as of 2026-07-16:

- Python 3.14.6 using the normal GIL build;
- uv/uv_build 0.11.29 with `pyproject.toml`, `.python-version`, and `uv.lock`;
- `discord.py` 2.7.1 with four async client instances;
- `openai` 2.46.0, `httpx` 0.28.1, the stable Responses API, Pydantic Structured
  Outputs, and `gpt-5.6-luna` as the deploy-time-verified default;
- `boto3` and `boto3-stubs` 1.43.50;
- Amazon ECS on ARM64 Fargate Spot, ECR, DynamoDB, SSM Parameter Store, and
  CloudWatch Logs;
- Ruff 0.15.22, ty 0.0.61, pytest 9.1.1, import-linter 2.13, Hypothesis,
  respx, pip-audit, and the other versions recorded in the detailed design.

These are design inputs, not permission to create cloud resources. Do not
provision AWS resources, create Discord Applications, register commands in a
real Guild, or make paid API calls unless the task explicitly requests it.

Use uv 0.11.29 for local development, CI, packaging, and release work. The
`[tool.uv].required-version` compatibility range is intentionally
`>=0.11.8,<0.12` because GitHub's Dependabot uv updater currently embeds
0.11.8. Do not tighten that setting to the local exact version unless the
Dependabot image supports it; keep `uv_build>=0.11.29,<0.12` and the normal
tooling baseline at 0.11.29. Recheck the official Dependabot updater image
before changing the compatibility floor.

Use `requires-python = ">=3.14,<3.15"` and Ruff target `py314`. Declare direct
dependencies with compatible major ranges and lock every resolved version in
`uv.lock`. CI and container builds must use `uv sync --frozen` or
`uv run --frozen`. Do not invent commands that depend on tooling that has not
yet been implemented.

## Implemented Python Commands

Run these commands from the repository root. Keep uv at the version required by
`pyproject.toml` and use the locked environment for every validation command.

```sh
uv lock --check
uv sync --frozen --all-groups
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen ty check
uv run --frozen lint-imports
uv run --frozen pytest
uv run --frozen python tools/run_dynamodb_local.py
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

The current domain slice must retain 90% or greater line/branch coverage and
must exercise every phase pair, checkpoint/recovery boundary, retry attempt
boundary, UUIDv7 boundary, UTC timestamp rule, and immutable-state invariant.

## Expected Project Structure

Prefer this separation as the implementation is introduced:

```text
src/shittim_chest/
├── __main__.py
├── bootstrap.py      # composition root
├── config/
├── domain/           # standard library only
├── application/      # domain and Protocol dependencies only
├── adapters/
│   ├── discord/
│   ├── openai/
│   └── dynamodb/
└── observability/
tests/
├── unit/
├── contract/
├── integration/
└── fixtures/
tools/
```

Do not create empty placeholder modules merely to match this tree. Add the
smallest structure needed for the current, approved slice. Do not introduce a
DI framework, service locator, global mutable state, or generic `utils/`
package. External SDK imports belong in adapters, and `bootstrap.py` is the
only composition root.

## Python Style

- Use UTF-8, LF, four spaces, double quotes, and a 100-character line limit.
- Type every function, method, and attribute and pass `ty check` across `src`,
  `tests`, and `tools`. Keep `missing-type-argument=error` and
  `possibly-unresolved-reference=warn`; do not silence whole ty rule categories
  to manufacture a passing result.
- Prefer `@dataclass(frozen=True, slots=True)` models and `StrEnum` domain
  state. Use Pydantic only for settings, structured output, and external
  boundary validation.
- Do not pass `dict[str, Any]` into the application layer.
- Generate debate IDs with `uuid.uuid7()`, use timezone-aware UTC timestamps,
  and persist `schema_version` on every DynamoDB record.
- Change debate phases only through the domain state machine.
- The only `DebatePhase` sequence is `ACCEPTED`, `PREPARING_EVIDENCE`,
  `COLLECTING_INITIAL_OPINIONS`, `DISCUSSING`,
  `COLLECTING_FINAL_PROPOSALS`, `SELECTING_WINNER`,
  `GENERATING_DECISION`, `COMPLETED`; `CANCELLED` and `FAILED` are terminal.
  Spot interruption uses `recovery_state=checkpointed`, not another phase.
- Keep async code non-blocking. Reuse network clients instead of creating one
  per request.
- Use `asyncio.TaskGroup`, `asyncio.timeout()`, and owned semaphores. Do not
  create ownerless background tasks or swallow `CancelledError`.
- Limit OpenAI work to six concurrent calls per process. Isolate synchronous
  boto3 calls in a dedicated worker thread so they never block the event loop.
- Use explicit deadlines at request, phase, and session levels. Retry only
  retryable errors and stop when the remaining deadline cannot support another
  attempt.
- Keep state transitions explicit and conditional. Invalid transitions must
  fail without side effects.
- Use structured logs with stable event names, correlation IDs, and debate IDs.
- Keep functions focused; do not combine Discord posting, OpenAI invocation,
  vote calculation, and DynamoDB writes in one handler.
- Ruff is the only formatter/import sorter/linter; do not also add Black,
  isort, or flake8. Enable `E,F,I,UP,B,SIM,ASYNC,RUF,S` as the baseline and
  keep cyclomatic complexity at 10 or lower.
- Enforce domain/application/adapter dependency direction with import-linter.

## Discord Rules

- The orchestration Bot alone owns Slash Command registration and Interaction
  acknowledgement unless a later decision says otherwise.
- The four Applications are Guild-install-only, privately owned with 2FA,
  Public Bot disabled, and OAuth2 Code Grant disabled.
- Use only the `GUILDS` Gateway Intent. Keep all privileged intents disabled.
- The public command is `/shittim question:<question>`. Progress, cancellation,
  and retry are thread control-panel interactions rather than separate Slash
  Commands.
- Register it as a Guild Command only on the configured Guild; require a
  question of 1 to 1000 characters.
- Ephemerally defer within Discord's three-second deadline. After validation,
  create a public starter message and a Public Thread, then return the thread
  link via ephemeral follow-up.
- Accept new debates only when all four Bot clients are READY.
- Do not require Message Content Intent when Slash Commands and internal events
  are sufficient.
- Send with `allowed_mentions.parse=[]`. Split at 2,000 characters on paragraph
  boundaries and label chunks deterministically.
- Persist Discord `thread_id` and relevant `message_id` values when persistence
  is implemented.
- Publish only persisted outbox operations. Use a 22-character unpadded
  base64url UUIDv7 nonce and store Bot ID, content hash, chunk sequence, claim
  owner/expiry, attempt, next retry, message ID, and thread ID. Discord nonce
  deduplication lasts only a few minutes; reconcile long outages against thread
  history and content hashes.
- Keep archived threads; never auto-unlock locked threads.
- Sync the Guild Command only when its canonical schema hash changes.
- Respect discord.py rate-limit handling and Discord `Retry-After`; do not add
  a second application-layer retry loop for the same request.

## OpenAI Rules

- Read current official OpenAI documentation before choosing a model, API
  parameter, structured-output schema, or retry behavior; model availability
  and API capabilities may change.
- Reuse one `AsyncOpenAI` per process and use the Responses API.
- Do not use Responses API Multi-agent beta, `client.beta.responses`, the
  `multi_agent` request field, or its beta header. Keep persona concurrency,
  voting, checkpointing, and resume in the typed Python application layer.
- Use `store=false`, but disclose that default abuse-monitoring logs may retain
  user content for up to 30 days. Do not send raw Discord user IDs to OpenAI.
- Enable Web search only when the question router marks external evidence
  optional or required. Request `web_search_call.action.sources` and persist
  tool-returned source metadata rather than trusting model-generated URLs.
- Use `responses.parse()` with Pydantic and distinguish refusal, incomplete
  output, parsed output, and transport failure before returning domain models.
- Confirm the configured model is available to the actual OpenAI project at
  deployment time.
- Use `PRODUCTION_POLICY=luna_standard` for every production generation phase.
  Do not expose Terra standard or Luna pro through bootstrap configuration,
  runtime parameters, or Discord controls. STEP-05C may record only
  `escalation-shadow-v1` with `executed=false`. Paid evaluation requires
  the explicit local `--live` gate and must write raw output outside the
  repository. Never use another policy to bypass a refusal or policy block.
- Version the model identifier, persona prompts, and structured-output schema
  used by each debate session.
- Load display names and prompts from versioned private runtime configuration;
  public source contains only schemas and generic examples.
- Create persona variety through distinct decision lenses, priorities,
  disagreement methods, proposal styles, and tone. Keep the same Evidence,
  safety constraints, and structured-output schema for all personas, and never
  claim independent verification merely because several prompts use one model.
- Validate all structured output at the application boundary. A syntactically
  valid model response is not automatically a valid domain action.
- Separate safety refusals and schema failures from retryable transport or
  rate-limit errors.
- Bound input and output size by phase and measure tokens, latency, cache use,
  and cost before changing concurrency.
- Do not duplicate retry behavior already provided by OpenAI SDK, boto3, or
  discord.py in the application layer.

## AWS Defaults

- Use `ap-northeast-1` unless an existing resource explicitly requires another
  Region.
- Use IAM Identity Center SSO for interactive work. Do not create new long-lived
  access keys for normal development.
- Fargate task definitions must use `awsvpc` networking and a valid CPU/memory
  combination.
- Use an IPv4 VPC and public subnets with `AssignPublicIp=ENABLED`. Route
  `0.0.0.0/0` to an Internet Gateway, permit no ingress, and permit required
  TCP 443 egress. Do not add an ALB, NAT Gateway, NAT instance, DNS64, or NAT64
  for the MVP.
- Discord REST/Gateway and OpenAI API are IPv4 dependencies as of 2026-07-16.
  Do not design an IPv6-only task until all migration gates in the detailed
  design pass, including a 24-hour connectivity canary and an explicit ECS
  Exec decision.
- Use the `FARGATE_SPOT` capacity provider only, `desiredCount=1`, Linux ARM64,
  platform version 1.4.0 or later, and container `stopTimeout=120` seconds.
- If the deployment-time combined Tokyo Spot rate for ARM64 is higher than
  x86_64, or ARM64 compatibility checks fail, use x86_64; otherwise keep ARM64.
- On SIGINT/SIGTERM, synchronously stop acceptance and interaction dispatch,
  cancel and await owned work so the current phase is checkpointed, and finish
  application cleanup within 90 seconds. Keep container `stopTimeout=120` and
  reserve its last 30 seconds for Discord/log/runtime exit. A replacement task
  must acquire a fenced DynamoDB lease and resume only unfinished work.
- Do not add automatic on-demand Fargate fallback. The Bot may remain offline
  until Spot capacity becomes available again.
- Deploy stop-before-start so only one task normally holds the four Bot tokens
  and one public IPv4 address. Planned downtime during deployment is accepted.
- The normal task uses an init process, non-root user, read-only root filesystem,
  privileged mode off, all Linux capabilities dropped, and ECS Exec disabled.
- ECS Exec is break-glass only. Stop the normal task, deploy a dedicated writable
  task revision with the required shell utilities and limited IAM, retain
  session logs for 90 days, audit API calls through CloudTrail Event History,
  and restore the read-only Exec-disabled revision after investigation.
- The container health check may verify only the process and event-loop
  heartbeat. Monitor Discord and OpenAI connectivity with separate metrics and
  alarms to avoid restart loops during external outages.
- Keep the ECS execution role separate from the application task role.
- Scope the task role to the required DynamoDB table and scope the execution
  role to the required ECR, SSM Parameter Store, and logging actions.
- Use DynamoDB on-demand mode, deletion protection, a `RETAIN` removal policy,
  and 35-day PITR. Enforce three concurrent sessions with three transactionally
  acquired fenced lease slots. Cross-item writes require a META `ConditionCheck`
  in the same `TransactWriteItems`; never use a GSI as the lock authority.
- Secrets injected at task launch are not hot-reloaded; document deployment and
  token-rotation behavior.
- Do not depend on DynamoDB TTL for immediate lock release or security-critical
  deletion.
- Treat deployment overlap carefully: two tasks using the same Discord Bot
  tokens must not both accept the same work.
- Restrict plan and drift OIDC roles with `StringEquals` to the immutable main
  subject and the deploy role to the immutable `production` environment
  subject, always with `aud=sts.amazonaws.com`. One manual release workflow
  builds and tests once, attests a manifest, waits for environment approval,
  and executes only that manifest and change set with non-cancelling
  production concurrency.
- Configure monthly AWS budgets of USD 50 for project-tagged spend and USD 50
  for the account, plus an OpenAI project budget of USD 50. Activate the
  `Project` cost-allocation tag and enable Cost Anomaly Detection. Use Container
  Insights and a composite alarm so Spot downtime is warning-only while a
  running task with fewer than four READY Bots is critical. Apply CloudWatch
  Logs data protection and validate IAM policies with Access Analyzer.

## Current Official Documentation Policy

For every detailed-design, implementation, review, and incident-response task,
access current official internet documentation during that same task. Do not
rely only on links or facts verified in an earlier task.

- Prefer AWS Developer Guides, API References, and Pricing; Discord Developer
  Docs; OpenAI Developer Docs and API schemas; Python docs; PyPI; and official
  repositories.
- Treat blogs, Stack Overflow, and generated answers as secondary evidence.
- Record the source URL, verification date, applicable service/API/library
  version, and the design consequence in the design note or an ADR.
- Recheck pricing, model availability, Fargate Spot behavior, package versions,
  Discord constraints, and DNS records both at implementation and deployment.
- Verify API shapes against an API Reference, OpenAPI schema, or SDK types in
  addition to guides.
- If official documentation and observed behavior differ, reproduce the issue
  in an integration test and record it in an ADR.
- If current official documentation cannot be reached, do not guess or finalize
  the external behavior; report the work as unverified and pause that portion.
- Pull requests must list sources, dates, versions, and compatibility results,
  including ARM64 container tests.

## Secrets and Local Configuration

Never commit or print:

- Discord Bot tokens;
- OpenAI API keys;
- AWS credentials or SSO cache data;
- real user questions or full AI output captured during private testing.

Use a committed `.env.example` with placeholders only if local environment
variables are introduced. Keep the real `.env` ignored and permission-restricted.
Do not place secrets in source, tests, fixtures, screenshots, logs, commit
messages, AGENTS.md, README files, or Obsidian notes.

## Testing Requirements

Add tests in proportion to each implemented slice. At minimum, cover:

- valid and invalid state transitions;
- self-vote, unknown candidate, duplicate vote, score range, ties, and candidate
  order randomization;
- phase concurrency, timeout, cancellation, and sibling-task failure;
- repeated events and conditional-write conflicts;
- Discord-send-success/database-write-failure and the reverse ordering;
- OpenAI refusal, invalid structured output, 429, timeout, and non-retryable
  authentication/model errors;
- one Discord Bot disconnecting while others remain connected;
- 1,001-character questions, the wrong Guild, disallowed or unsupported
  channels, archived and locked threads, and permission removal;
- SIGTERM and forced-restart recovery at every debate phase, including lease
  fencing, outbox reconciliation, and resume without duplicate Discord posts;
- secret and full-content redaction in logs.

CI verifies the uv lock, Ruff, ty across source/tests/tools,
import-linter, pytest, pip-audit, Betterleaks
full-history and generated-fixture contracts, wheel
build/install, CycloneDX source SBOM, public repository surface, Markdown
structure, Wiki links, and workflow syntax. STEP-03 makes import-linter
enforceable in the existing `quality` check; the native ARM64 container gate is
added in STEP-08B.

The GitHub-hosted runner cannot access the private Obsidian Vault. Run
`python tools/sync_docs.py --check` locally against `SHITTIM_DOCS_SOURCE` before
publishing documentation changes; CI runs `python -m tools.check_docs` against
the public mirror instead. Never give a public PR Vault credentials or mount the
Vault on a self-hosted runner.
Keep domain/application coverage at 90% or higher. Use DynamoDB Local for
transactions, conditional writes, GSIs, migrations, and outbox tests, with
separate contract tests for behavior it cannot emulate.

Use `tools/run_dynamodb_local.py` for the local full persistence suite. It
chooses Podman before Docker, maps DynamoDB Local to a random loopback-only
port, injects `DYNAMODB_ENDPOINT_URL` only into the child test process, waits
for a signed `ListTables` request, and stops only its PID-derived temporary
container. It uses the same digest-pinned DynamoDB Local 3.3.0 image as CI and
does not need valid AWS credentials. A Codex restricted sandbox can reject
rootless Podman before it contacts the service because `/run/user/1000/libpod`
is read-only; execute the helper on the host in that case rather than changing
the image, endpoint, or test skips.

Use fakes for Discord, OpenAI, the clock, IDs, Evidence, and repositories in
unit tests. Network-backed integration tests must be opt-in and must not incur
paid usage unexpectedly.

Before handing off a change, run every available formatter, linter, type check,
and test command documented by the repository. State clearly when a command
does not yet exist or could not run.

## Git and Change Discipline

- The default branch is `main`.
- All changes to `main` use Pull Requests. The public single-maintainer ruleset
  requires zero independent approvals but requires configured checks, blocks
  force-push/delete, and has no bypass actor.
- Public pull-request jobs are untrusted: use read-only `GITHUB_TOKEN`, no
  secrets or OIDC, no self-hosted runner, and never execute fork code through
  `pull_request_target`.
- Use concise imperative commit messages.
- Keep generated caches, virtual environments, coverage data, `.env`, runtime
  state, and credentials out of Git.
- Preserve unrelated user changes in a dirty worktree.
- Do not commit unless the user explicitly requests a commit.
- A change handoff should summarize changed files, validation commands and
  results, unresolved decisions, and any AWS, Discord, cost, or data-retention
  impact.

## License Scope

Source code, infrastructure as code, tools, and public samples are licensed
under the MIT License. Design documents under `docs/` and this `AGENTS.md` are
not covered by MIT and remain all rights reserved. Submit design proposals as
Issues rather than direct documentation Pull Requests unless the maintainer
explicitly requests one.
