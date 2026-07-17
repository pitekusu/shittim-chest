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
gate and STEP-02C Gitleaks retirement are complete. STEP-03 application core is
implemented on draft PR `#15` with voting rules, Protocols,
accept/run/cancel/retry/resume use cases, deadlines, checkpoint-aware
cancellation, and fake-based async tests. External adapters,
containers, AWS resources, and Discord Applications are not yet implemented.
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
coverage, Ruff and mypy strict success, zero known locked-dependency
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

The current slice is STEP-03 application core. Treat it as SDK-independent until
its Pull Request merges. The next slice after merge is STEP-04 persistence:
DynamoDB serialization, conditional transactions, lease slots, outbox, schema
migration, and DynamoDB Local tests.
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

- Run `gh auth status` before a write workflow and stop if the active account is
  not `pitekusu` or authentication is invalid. Never print or persist its token.
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
- `openai` 2.45.0, `httpx` 0.28.1, the Responses API, Pydantic Structured
  Outputs, and `gpt-5.6-luna` as the deploy-time-verified default;
- `boto3` and `boto3-stubs` 1.43.49;
- Amazon ECS on ARM64 Fargate Spot, ECR, DynamoDB, SSM Parameter Store, and
  CloudWatch Logs;
- Ruff 0.15.22, mypy 2.3.0, pytest 9.1.1, import-linter 2.13, Hypothesis,
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
- Type every function, method, and attribute and pass `mypy --strict`.
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
- Use `store=false`, but disclose that default abuse-monitoring logs may retain
  user content for up to 30 days. Do not send raw Discord user IDs to OpenAI.
- Enable Web search only when the question router marks external evidence
  optional or required. Request `web_search_call.action.sources` and persist
  tool-returned source metadata rather than trusting model-generated URLs.
- Use `responses.parse()` with Pydantic and distinguish refusal, incomplete
  output, parsed output, and transport failure before returning domain models.
- Confirm the configured model is available to the actual OpenAI project at
  deployment time.
- Version the model identifier, persona prompts, and structured-output schema
  used by each debate session.
- Load display names and prompts from versioned private runtime configuration;
  public source contains only schemas and generic examples.
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
- On SIGTERM, stop accepting work, checkpoint completed operations and the
  current phase, flush state, and exit within the stop timeout. A replacement
  task must acquire a fenced DynamoDB lease and resume only unfinished work.
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

CI verifies the uv lock, Ruff, mypy strict, import-linter, pytest, pip-audit, Betterleaks
full-history and generated-fixture contracts, wheel
build/install, CycloneDX source SBOM, public repository surface, Markdown
structure, Wiki links, and workflow syntax. STEP-03 makes import-linter
enforceable in the existing `quality` check; the ARM64 container gate is added
in STEP-08.

The GitHub-hosted runner cannot access the private Obsidian Vault. Run
`python tools/sync_docs.py --check` locally against `SHITTIM_DOCS_SOURCE` before
publishing documentation changes; CI runs `python -m tools.check_docs` against
the public mirror instead. Never give a public PR Vault credentials or mount the
Vault on a self-hosted runner.
Keep domain/application coverage at 90% or higher. Use DynamoDB Local for
transactions, conditional writes, GSIs, migrations, and outbox tests, with
separate contract tests for behavior it cannot emulate.

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
