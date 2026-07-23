# Repository Guidelines

## Project Overview

This repository contains the Discord multi-agent debate Bot project
`shittim_chest` (シッテムの箱; official English name: The Shittim Chest).
The system runs four Discord Bot accounts: one orchestration Bot and three AI
persona Bots. The persona Bots produce initial opinions, revised proposals,
and votes; the orchestration Bot manages the workflow and publishes the
mechanically calculated result.

Requirements, basic design, and detailed design are complete. Implementation
proceeds in isolated slices, each squash-merged through a Pull Request;
per-slice validation evidence lives in `docs/20_実装・試験・検証記録.md`.
Approved decisions are recorded in the project index and decision record; do
not silently promote historical options to requirements.

## Implementation Progress

All slices through STEP-09B are merged to `main`:

| Slice | PR | Merge commit | Scope |
|---|---|---|---|
| STEP-01 | #1 | `7fa642e` | Python 3.14.6/uv foundation, UUIDv7 debate/attempt IDs, 21-edge phase state machine, checkpoint/retry rules |
| STEP-02/02B/02C | #10/#12/#13 | `e2fdaad` | GitHub CI quality/supply-chain gates, CycloneDX source SBOM, Sigstore-verified Betterleaks, Gitleaks retired |
| STEP-03 | #15 | `34ccc54` | Application core: voting rules, Protocols, accept/run/cancel/retry/resume use cases, deadlines |
| STEP-04A | #16 | `54948d7` | SDK-independent persistence contracts, schema v1-to-v2 up-conversion, outbox/panel serialization |
| STEP-04B | #18 | `9aafe6e` | boto3 DynamoDB adapter: transactions, three fenced lease slots, idempotent operation results, outbox |
| STEP-05A | #20 | `d6ea561` | OpenAI adapter: stable Responses API, Pydantic structured outputs, `store=false` |
| STEP-05B | #21 | `44a35fa` | Fail-safe `question-router-v2`, hosted Web search, immutable Evidence, schema v3 |
| STEP-05C/.1 | #22/#24/#26 | `a6f43cb` | Shadow escalation signals, Luna/Terra/pro policies, schema v4, blind A/B evaluator |
| STEP-06A | #27 | `47af41f` | SDK-independent Discord contracts: four Bot slots, message chunking, nonces, panel codec, outbox Protocol, schema v5 |
| STEP-06B | #30 | `96a1ace` | discord.py 2.7.1 publisher: fenced outbox publication, nonce/content-hash reconciliation |
| STEP-06C | #31 | `9799cb9` | Interaction runtime: four GUILDS-only clients, Guild-scoped `/shittim`, thread/panel provisioning |
| STEP-07A | #33 | `0f386f5` | Runtime lifecycle: fail-closed admission gate, signal handling, 90-second cleanup deadline |
| STEP-07B | #34 | `04bbda0` | Outbox recovery drained before phase work resumes |
| STEP-07C | #35 | `e863ae3` | Production composition root (`bootstrap.py`), `python -m shittim_chest`, fail-closed config |
| STEP-08A | #37 | `7742f0b` | Digest-pinned multi-stage Dockerfile, non-root production/break-glass targets, heartbeat health check |
| STEP-08B | #39 | `2cca51a` | Native ARM64 CI gate, SIGTERM/SIGKILL fault injection, Syft SPDX image SBOM |
| STEP-09A | #47 | `e2e9e3f` | CDK TypeScript foundation, retained Stateful stack (DynamoDB, ECR, Signer profile/Managed Signing) |
| STEP-09B | #53 | `3c5ccc9` | Runtime stack: public-only VPC, least-privilege IAM, ARM64 Fargate Spot singleton, digest-only task definitions |

Not yet implemented:

- STEP-02D notification code is implemented as an ordered A/B/C PR stack, but
  live Discord Forum threads, webhook/thread values, GitHub notification
  settings, activation, and smoke tests remain operator work. Keep
  `DISCORD_NOTIFICATIONS_ENABLED` false or unset until all three slices are on
  `main` and the four fixed Forum threads exist. The friend-only server exposes
  the Forum to `@everyone`; no notification role is configured.
- STEP-09C: operations and monitoring (Budgets, Cost Anomaly Detection,
  metrics/alarms) and pre-scale image admission.
- STEP-10: real signing/referrer verification and release workflows.
- Discord Applications are not created; there are no live Discord or paid
  OpenAI calls.
- No AWS resource has been bootstrapped or deployed; both CDK stacks are
  synth-only.

### Standing constraints from merged slices

- Configure every Discord client with `max_ratelimit_timeout=30`; the adapter's
  45-second Discord-operation timeout must remain shorter than
  `OUTBOX_CLAIM_SECONDS=60` so a blocked SDK wait cannot outlive claim
  ownership.
- On Python 3.14 with discord.py 2.7.1, do not use `Client.event()` for the
  interaction listener because it reaches a deprecated asyncio API while tests
  treat warnings as errors; use the dedicated moderator client's explicit
  `on_interaction` dispatch.
- Each DynamoDB schema reader migrates only the immediately previous version
  and fails closed on unknown versions.
- Drain pending outbox operations before phase work resumes; do not count
  outbox waiting against the 300-second active-processing deadline. A
  `RepositoryConflict` means this worker lost fencing and must not terminalize
  the attempt.
- Never enable Betterleaks provider validation, because detected candidates
  must not be sent to external APIs. Reintroduce a second secret scanner only
  through a later ADR with a concrete coverage gap. The weekly release-tool
  workflow detects newer actionlint, Betterleaks, Syft, and Grype releases
  without applying them automatically.
- Do not submit a custom GitHub dependency snapshot or grant `contents: write`;
  GitHub's Python Dependabot graph job already supplies the complete uv
  dependency inventory.
- The main Ruleset requires the strict checks `quality`, `tests`, `security`,
  `package`, `docs-public-safety`, `container-arm64`, and `grype`, plus CodeQL
  results with high-or-higher security alerts blocking merge. The `grype` job
  scans the CycloneDX source SBOM and the arm64 SPDX image SBOM and uploads
  SARIF to code scanning (`security-events: write`); new high-or-above Grype
  alerts block merge through the ruleset code scanning rule. The job caches its
  vulnerability database for one day and stores unfiltered full JSON results
  as artifacts. Do not bulk-dismiss base-image findings. Actionable SARIF uses
  `--only-fixed`; fixable High/Critical findings fail the job. Unfixable
  High/Critical findings require verified DHI `not_affected` VEX or a
  digest-bound, evidence-backed local acceptance expiring within 90 days.
- Production uses the digest-pinned DHI Community Python 3.14.6 Debian 13
  runtime and DHI-defined `nonroot` identity `65532:65532`. Keep Dockerfile,
  native container checks, ECS `user`, and the `/tmp/shittim-chest` tmpfs in
  sync through `container-policy.json`. Do not add a shell or package manager
  to production; build the separate break-glass target from the matching DHI
  `-dev` image.
- DHI requires authentication even for Community images. `DHI_USERNAME` and
  read-only `DHI_TOKEN` must exist separately in Actions secrets and Dependabot
  secrets; never log, commit, or place them in Obsidian. Docker Dependabot runs
  daily. Fork PRs cannot receive these secrets and must be reproduced on a
  trusted maintainer branch before merge.
- Production remains fixed to Luna standard. The completed blind A/B evaluation
  (Luna pro 4 wins, Terra standard 2 wins, 4 ties; operator chose Luna standard
  with no escalation) is evaluation history only; do not implement thresholds,
  extra token/deadline limits, or escalation UI.
- The container fault fixture is copied only into the CI-only `fault-test`
  target; never push or deploy that target. Keep the host-side container gate
  standard-library-only (the host may run Python 3.12) and preserve the unit
  assertion that its phase list matches the domain state machine.

Update the progress table and `20_実装・試験・検証記録.md` after each later
slice so the boundary does not become stale.

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

The 15 public-safe project notes in the operator's Obsidian Vault are the source
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
- Use AWS CDK with TypeScript and GitHub Actions OIDC. Use monthly Budgets of
  USD 20 for project-tagged AWS spend, USD 30 for the whole AWS account, and
  USD 50 for OpenAI; set the Cost Anomaly Detection total-impact notification
  threshold to USD 10. Activate the `Project` cost-allocation tag before the
  tag budget deploy. Create and verify new CDK-managed notifications before
  removing legacy manual USD 10 Budget/CAD notifications; reuse the existing
  AWS managed service monitor. Tag AWS resources with `Project=shittim-chest`.
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

Domain tests must exercise every phase pair, checkpoint/recovery boundary,
retry attempt boundary, UUIDv7 boundary, UTC timestamp rule, and
immutable-state invariant.

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

## Container Build Rules

- The production builder must keep locked dependency installation in a layer
  before application source is copied. Use `uv sync --frozen --no-dev
  --no-install-project --no-editable` for that layer, then copy `README.md`,
  `LICENSE`, and `src/` and run the final frozen non-editable sync.
- Mount `/root/.cache/uv` as a BuildKit cache with `sharing=locked`; do not set
  `UV_NO_CACHE=1` for the build. Keep `UV_PYTHON_DOWNLOADS=0` because the pinned
  Python base image is the only permitted interpreter source. A cache miss must
  change build time only, never the resulting dependency versions or gate
  result.
- In GitHub Actions, set up one Buildx builder and use full-SHA-pinned official
  `docker/build-push-action` steps with `load: true` so the later runtime and
  Syft checks inspect the exact images that were built. Export only the
  production `mode=max` cache to the `container-arm64-production` GHA scope,
  with cache export failure ignored because it is an optimization. Do not push
  either the production test image or the CI-only fault image from Pull Request
  CI. Keep the workflow at `contents: read` with no secret or OIDC access.
  Retain the Buildx summaries and diagnostic build records for 30 days,
  matching the test image SBOM retention period.

## AWS Defaults

- Use `ap-northeast-1` unless an existing resource explicitly requires another
  Region.
- Use IAM Identity Center SSO for interactive work. Do not create new long-lived
  access keys for normal development.
- The CDK project uses Node.js 24.18.0 Active LTS, exact npm dependency pins,
  recommended CDK feature flags, cdk-nag 3 validation plugins, TypeScript
  strict mode, and Vitest assertions. Keep both CDK stacks synth-only in
  local/PR validation; do not bootstrap or deploy them. The break-glass task
  definition and role remain detached from the service until an approved later
  workflow switches revisions.
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
- Keep ECR tag mutability at exclusion-free `IMMUTABLE`; never introduce
  `latest`, mutable exclusions, or tag-based task definitions. Tags are
  traceability labels only. ECS deploy and rollback must use
  `repository@sha256:<digest>` and must not resolve a tag after approval.
- ECR uses its default server-side encryption. Managed Signing must use the
  retained `shittim_chest_ecr` `Notation-OCI-SHA384-ECDSA` profile. For every
  release digest, require an AWS Signer signature, SPDX SBOM, build provenance,
  and vulnerability assessment as ECR OCI reference artifacts.
- Release verification is fail closed: poll signing status by digest, run
  Notation strict verification against the expected profile including
  revocation, verify GitHub attestation identity, and use ECR
  `ListImageReferrers` to match all artifact digests before executing a change
  set. Signing status alone is not cryptographic verification.
- Restrict plan and drift OIDC roles with `StringEquals` to the immutable main
  subject and the deploy role to the immutable `production` environment
  subject, always with `aud=sts.amazonaws.com`. One manual release workflow
  builds and tests once, attests a manifest, waits for environment approval,
  and executes only that manifest and change set with non-cancelling
  production concurrency.
- Configure a monthly USD 20 Budget for project-tagged spend, a USD 30 Budget
  for the whole account, and a USD 50 OpenAI project budget. Activate the
  `Project` cost-allocation tag and set the Cost Anomaly Detection total-impact
  notification threshold to USD 10. Do not enable Container Insights for the
  singleton MVP. Create and verify new CDK-managed notifications before
  removing the legacy manual USD 10 Budget/CAD notifications; reuse the
  existing AWS managed service monitor.
  Monitor free ECS service CPU/memory metrics; notify on EventBridge task-stop
  and Spot-interruption events; and implement low-cardinality, content-free
  `BotReady`, heartbeat-age, outbox-backlog, and failure-count metrics with
  their alarms. Apply CloudWatch Logs data protection and validate IAM policies
  with Access Analyzer.

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
structure, Wiki links, workflow syntax, and a Grype scan of the source and
arm64 image SBOMs uploaded as SARIF to code scanning. STEP-03 makes
import-linter
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
  secrets or OIDC, and no self-hosted runner. Never execute fork code through
  `pull_request_target`. The only permitted target workflow is
  `.github/workflows/discord-repository-events.yml`: it may read PR metadata
  with default-branch notification code, but must not checkout the PR head,
  download artifacts, use caches, or request write permission. Keep the
  dedicated negative policy tests as a merge gate.
- GitHub-to-Discord operational notifications are an at-least-once convenience,
  not an authoritative status store. Keep `DISCORD_WEBHOOK_URL` in Actions
  Secrets only, keep actual thread IDs in repository variables only, do
  not duplicate the webhook into Dependabot Secrets, and leave
  `DISCORD_NOTIFICATIONS_ENABLED` false until the four Forum threads
  and smoke-test procedure in `docs/21_GitHub・Discord通知運用設計.md`
  are ready.
- Do not configure `DISCORD_ALERT_ROLE_ID`. A missing or blank value must keep
  `allowed_mentions.parse=[]` for failures, High/Critical alerts, and monitor
  failures as well as normal notifications.
- GitHub currently documents `vulnerability-alerts: read` as the dedicated
  `GITHUB_TOKEN` permission for Dependabot Alerts. Pinned actionlint 1.7.12
  predates that scope, so CI ignores only its exact unknown-scope diagnostic;
  `tools/check_notification_workflows.py` must continue to enforce exactly one
  read-only use in `discord-security-digest.yml`. Do not replace it with
  `read-all` or widen the ignore pattern.
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
