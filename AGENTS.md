# Repository Guidelines

Agent and maintainer instructions for `shittim_chest` (シッテムの箱 / The Shittim Chest).
Design detail lives in `docs/`; this file is the working contract for implementation.

## Project

Four Discord Bot identities in one Python process: one **moderator** and three
**participant** personas. Personas produce opinions, revised proposals, and
votes; the moderator runs the workflow and publishes a **Python-calculated**
result (not an LLM “winner”).

Public surface only: generic slots, schemas, and design mirrors. Production
Guild/channel/Application IDs, display names, persona prompts, tokens, and API
keys stay in private operator notes and versioned SSM—not in Git.

## Status (STEP-10-A zero-count image admission fix branch, 2026-07-31)

The merged `main` baseline includes PR `#123` reproducible dual-image release
hardening on top of signed HTTP ingress, On-Demand scale-to-zero, and
STEP-09C-A/B/C monitoring and cost governance. Production Release run
`30599324993` completed every plan gate, built and pushed both images once,
verified signing/scanning and four OCI referrers per image, attested the canonical
manifest, reverified it after Environment approval, and acquired the deployment
fence. Stateful remained stable, but initial Runtime creation rolled back before
task launch because a `desiredCount=0` ECS service revision omits
`containerImages`; the admission Lambda incorrectly required that optional
field. A task-free live reproduction confirmed one valid service revision with
an immutable `taskDefinition` and zero `containerImages`. This branch verifies
the exact normal task definition referenced by the revision instead, grants only
that task definition's `DescribeTaskDefinition`, and makes failure diagnostics
query the surviving stack rather than an executed change set. The deployment
fence and unexecuted change sets were cleaned. Runtime is `ROLLBACK_COMPLETE`;
Operations and CostGovernance remain resource-free `REVIEW_IN_PROGRESS`.
Discord and the first successful production deployment remain unchanged. The
authoritative Obsidian progress/evidence notes and public mirror track this state.

| Area | Current implementation |
|---|---|
| Domain / application | Phases, voting, Protocols, accept/run/cancel/retry/resume, deadlines |
| Persistence | DynamoDB adapter, schema **v7**, control manifest **v2**, 3 fenced leases, outbox |
| OpenAI | Responses API, structured outputs, question router, Web search, Luna standard only |
| Discord | Signed HTTP ingress, `/shittim`, 4 runtime clients, publisher + outbox reconcile, panel |
| Runtime | Admission gate, signals, outbox drain before phase work, `python -m shittim_chest` |
| Container | Digest-pinned multi-stage image, ARM64 CI, SIGTERM/SIGKILL fault gates |
| Scale-to-zero | Durable FIFO 20, 3/15/30-minute semantics, wake/drain/reconcile, race fences |
| Infra | Stateful + ReleaseIdentity deployed; Runtime/Operations/CostGovernance synth-only |
| Ops notifications | GitHub → Discord Forum (STEP-02D); friend server; no alert role |
| Ops metrics | `ShittimChest/Prod`, fixed `Service` dimension, 10-metric allowlist |
| Ops monitoring | 9 underlying alarms, 2 actionable composites, dashboard, abnormal ECS stops |
| Cost governance | Project $20/account $30 Budget, existing-monitor CAD total-impact $10 |
| Release | immutable OIDC roles, complete CDK asset closure, signed normal/break-glass images, OCI attestations, manifest, fenced change sets, convergent cleanup, drift |

**Not done**

- First successful Runtime/Operations/CostGovernance deployment
- Real Discord Application endpoint switch and live Discord/OpenAI acceptance
- Paid OpenAI in CI
- Successful ECS admission and workload-stack drift acceptance remain unexecuted

Slices ship as isolated PRs (squash merge). After each slice, update `docs/20_…`
and the plan/progress notes so this boundary does not go stale.

### Hard constraints

- Discord clients: `max_ratelimit_timeout=30`; adapter Discord op timeout **45s**
  must stay under `OUTBOX_CLAIM_SECONDS=60`.
- Python 3.14 + discord.py 2.7.1: do **not** use `Client.event()` for the
  interaction listener (deprecated asyncio path; tests treat warnings as
  errors). Use the moderator client’s explicit `on_interaction` dispatch.
- DynamoDB readers migrate only **previous → current** schema; fail closed on
  unknowns. Current schema version is **7** and the control-record manifest is
  **v2**.
- Discord HTTP ingress must validate the Ed25519 signature and timestamp over
  the untouched raw body before JSON parsing. Never persist Interaction tokens
  or log raw bodies, signatures, questions, or credentials.
- Scale-to-zero is one ARM64 On-Demand Fargate task at most (`512` CPU units /
  `1024` MiB): `desiredCount=0` when idle, converge to 1 only after durable
  ingress acceptance. Fargate Spot and the old always-on `desiredCount=1`
  baseline are superseded.
- STEP-09C-A custom metrics use namespace `ShittimChest/Prod`, one fixed
  `Service` dimension (`runtime` or `reconciler`), standard 60-second
  resolution, and the 10-name allowlist in `runtime/operational_metrics.py`.
  Never add IDs, user content, phase/model strings, or dynamic dimensions.
  EMF goes through existing Logs; do not grant `cloudwatch:PutMetricData`.
- Runtime alarms, AWS Budgets, and Cost Anomaly Detection share one private
  operator email supplied at deploy time. Never commit the address. Image
  admission belongs to STEP-10-A with release signing/referrer verification,
  not STEP-09C Operations monitoring.
- STEP-09C-B must keep underlying alarms actionless: only the critical and
  warning composite alarms publish to SNS. Bot/heartbeat missing data is
  breaching only behind the active-runtime gate. EventBridge notifies only
  abnormal stop codes and sends a bounded, content-free payload; planned
  user/scheduler stops must remain silent.
- STEP-09C-C cost resources live in independent `us-east-1`
  `ShittimChest-Prod-CostGovernance`; Tokyo remains the workload Region. Never
  create another AWS managed SERVICE anomaly monitor: require its existing ARN.
  The `Project` cost-allocation tag must be Active before deploy, and both
  stacks must receive the same operator email without committing its value.
- The ingress FIFO contains at most 20 PENDING/CLAIMED/RETRYING requests.
  Accepted debates leave the FIFO and continue to consume the existing three
  fenced global slots.
- Startup warning at 3 minutes is non-terminal; the request remains recoverable
  until terminal failure at 15 minutes. Scale-down is eligible 30 minutes after
  the last debate is fully complete, not 30 minutes after the last request.
- Drain outbox before phase resume; outbox wait does **not** count toward the
  300s active-processing deadline. `RepositoryConflict` ⇒ lost fencing; do not
  terminalize the attempt.
- Production generation policy is **Luna standard only** (no Terra/Luna pro at
  runtime). Shadow escalation stays `executed=false`.
- Betterleaks: never enable provider validation. Second secret scanner only via
  ADR. Do not custom-submit GitHub dependency snapshots (`contents: write` not
  for that).
- Required merge checks: `quality`, `tests`, `security`, `package`,
  `docs-public-safety`, `container-arm64`, `grype`, plus CodeQL high+ blocking.
  Grype: actionable `--only-fixed` SARIF; fixable High/Critical fail the job;
  unfixable need DHI `not_affected` VEX or digest-bound acceptance ≤90 days.
  Acceptance dates are UTC; preflight with the validator's default clock against
  the exact CI report/VEX/digest artifacts, never an operator-local `--today`.
  Do not bulk-dismiss base-image findings.
- Container risk acceptances bind separately to the production and break-glass
  **image config digests**, not exporter-specific manifest digests. CI must build,
  SBOM, VEX, and gate both targets with `SOURCE_DATE_EPOCH=0`; the Dockerfile must
  expose that value in the builder so uv/Python emit hash-based bytecode, and every
  image exporter must set `rewrite-timestamp=true`. Release must derive both config
  digests again from the pushed manifests. A digest update requires exact
  report/VEX/config-digest evidence for both targets before merge.
- After the final frozen uv sync, canonicalize every installed `.dist-info/RECORD`
  as valid three-column CSV sorted by row. Remove uv's checkout-time-dependent
  `uv_cache.json` file and its same-dist-info RECORD row; reject another path,
  symlink, or non-regular file. Cold and cache-reused uv installs may otherwise
  contain the same application but change the final image config digest.
- Production image: digest-pinned DHI Community Python **3.14.6** Debian 13,
  `nonroot` **65532:65532**, policy in `container-policy.json` (Dockerfile, CI,
  ECS user, `/tmp/shittim-chest` tmpfs). No shell/package manager in production;
  break-glass from matching DHI `-dev`; its apt layer must remove apt/dpkg logs
  before export so config digests remain reproducible. CI-only `fault-test`
  target: never push or deploy. DHI needs `DHI_USERNAME` / `DHI_TOKEN` in
  Actions **and** Dependabot secrets (never log/commit/Obsidian).

## Authoritative documents

| Role | Location |
|---|---|
| Source of truth | Operator Obsidian public-safe notes (`SHITTIM_DOCS_SOURCE`) |
| Public mirror | `docs/` (read-only relative to Vault) |
| Responsibilities | Index in `docs/00_…`: requirements / decisions / detailed design / tests / traceability |

The supplemental specification directory named exactly
`100_Ondemand Fargate/` contains the canonical goal, commit plan, and completion
checklist for scale-to-zero. `tools/sync_docs.py` validates and copies these
three files to the same relative path under public `docs/`; together with the
15 root notes, the mirror contains 18 canonical documents. Do not publish the
source's local filesystem location.

There is **no** silent precedence: on conflict, stop, ADR, update every affected
note in the same change.

```sh
# After editing Obsidian (never edit docs/ mirrors by hand):
python tools/sync_docs.py --write --source "$SHITTIM_DOCS_SOURCE"
python tools/sync_docs.py --check --source "$SHITTIM_DOCS_SOURCE"
# CI uses: python -m tools.check_docs  (mirror only; no Vault)
```

Never put secrets, absolute home paths, or production IDs in Obsidian or `docs/`.
The Vault source directory must contain **only** the expected public notes
(extra folders such as private operator packs break `sync_docs.py`).

## GitHub writes (this repo)

Connected GitHub App is **read-only** for mutations (PR create/merge → 403).
Use authenticated **`gh` as pitekusu** for all GitHub writes.

1. Preflight on the host (not a restricted sandbox):
   `gh auth status` and `gh api user --jq '.login'` → must be `pitekusu`.
   Never print or store the token. Sandbox `gh` failures are not proof of
   expired credentials.
2. Local `git` for branch, stage, commit, push.
3. `gh` for PR create/update, checks, comments, merge.
4. Protect `main`: PR + **squash** merge + required checks green + no open review
   threads. Merge with
   `gh pr merge --match-head-commit <full-sha>` and delete the remote branch.

Prefer managed CodeQL, Secret scanning, Dependabot for inventory/alerts; this
policy only changes the **mutation** control path.

## MVP decisions (approved)

- Fun, coherent Discord presentation; do not claim three personas are stronger
  verification than one model.
- One private Guild; allowlisted channels; any channel member may start.
- Identities: `moderator`, `participant-a|b|c`. Display names/prompts are private
  config. Final text must state AI-generated, not professional advice.
- `/shittim question:<1–1000 chars>` → public thread + control panel (cancel/retry).
- Shared immutable Evidence; required search failure fails the session; optional
  failure continues with notice. Anonymous votes; winner writes constrained
  final decision.
- Active target 180s, hard 300s; 3 concurrent sessions; 30 starts/Guild/day.
- Persist debates/threads (no auto-expiry). DynamoDB PITR 35 days; no AWS Backup
  MVP. Logs 90 days.
- Signed Discord HTTP ingress with three responsibility-separated Lambdas:
  ingress, status publisher, and one-minute runtime reconciler.
- One ARM64 **On-Demand Fargate** task at most, `512` CPU units / `1024` MiB;
  normally scaled to zero and woken only after the request is durably stored.
- FIFO waiting limit 20 and active-debate limit 3 are independent boundaries.
- 3-minute startup warning is non-terminal; 15 minutes is terminal; automatic
  stop is 30 minutes after the last fully completed debate.
- CDK TypeScript + GHA OIDC. Budgets: project $20 / account $30 / OpenAI $50;
  CAD total-impact $10. Tag `Project=shittim-chest`. Activate cost-allocation tag
  before tag budgets. Verify new CDK notifications before removing legacy $10.
- IPv4 public subnets, task public IP, no ALB/NAT/inbound. SSM SecureString under
  `/shittim-chest/production/`. Public repo, ruleset on `main`, `production`
  environment for deploy.

## Architectural invariants

- Discord presentation ≠ `DebateOrchestrator` domain logic.
- Moderator never invents opinions, alters votes, or asks an LLM to pick the winner.
- Votes validated in Python (no self-vote, unknown id, duplicate, out-of-range).
- User input, Evidence, and other agents’ text are untrusted data, not instructions.
- Persona generation only from internal orchestrator events (no Bot-to-Bot loops).
- One Evidence fetch per session when enabled; same bundle for all personas.
- Adapters behind Protocols; unit tests need no network.
- No “independent verification” claim without evaluation evidence.

## Technology baseline (locked / CI)

| Component | Baseline |
|---|---|
| Python | 3.14.6 (GIL), `requires-python = ">=3.14,<3.15"` |
| uv | local/CI **0.11.x**; `[tool.uv] required-version = ">=0.11.8,<0.12"` (Dependabot embed); `uv_build>=0.11.32,<0.12` |
| Runtime deps | discord.py 2.7.1, openai 2.48.x, httpx 0.28.1, boto3 1.43.x, pydantic 2.13.x |
| Dev | Ruff **0.16.x** (`<0.17`), ty 0.0.63, pytest 9.1.x, import-linter 2.13, Hypothesis, pip-audit |
| Cloud design | HTTP API + 3 Lambdas, ECS ARM64 On-Demand Fargate 0↔1, ECR, DynamoDB, SSM, CloudWatch, CDK TS (Node 24 LTS) |

Versions move via Dependabot / dedicated tooling PRs; always trust `uv.lock`.
Do not provision AWS, create Discord apps, or make paid API calls unless the
task explicitly asks.

**Ruff minors** (e.g. 0.17): dedicated PR after format/lint impact check. Do not
mix with app dependency groups. Markdown Python fences are formatted by default
since 0.16—fix via Obsidian → `sync_docs`, not by hand-editing `docs/`. Keep
explicit lint `select` (`E,F,I,UP,B,SIM,ASYNC,RUF,S`); do not adopt Ruff’s full
default rule set without a separate decision.

## Commands

From repo root, always frozen:

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

Domain tests must cover phase pairs, checkpoint/retry boundaries, UUIDv7, UTC
timestamps, and immutability. Handoff: run every available gate above; say when
something could not run.

## Layout

```text
src/shittim_chest/
├── __main__.py
├── bootstrap.py          # only composition root
├── config/
├── domain/               # stdlib only
├── application/          # domain + Protocols
├── adapters/
│   ├── discord/
│   ├── openai/
│   └── dynamodb/
└── runtime/              # lifecycle, health
tests/{unit,contract,integration,fixtures}/
tools/
infra/                    # CDK TypeScript (synth-only)
docs/                     # public design mirror
```

No DI framework, service locator, global mutable app state, or generic `utils/`.
SDK imports stay in adapters. Do not add empty placeholder packages.

## Python style

- UTF-8, LF, 4 spaces, double quotes, line length 100.
- Full types; `ty check` on `src`, `tests`, `tools`.
  `missing-type-argument=error`, `possibly-unresolved-reference=warn`.
- Prefer `@dataclass(frozen=True, slots=True)`, `StrEnum`. Pydantic only at
  settings / structured output / external boundaries.
- No `dict[str, Any]` into the application layer.
- IDs: `uuid.uuid7()`; timestamps timezone-aware UTC; `schema_version` on records.
- Phases only via state machine:
  `ACCEPTED` → `PREPARING_EVIDENCE` → `COLLECTING_INITIAL_OPINIONS` →
  `DISCUSSING` → `COLLECTING_FINAL_PROPOSALS` → `SELECTING_WINNER` →
  `GENERATING_DECISION` → `COMPLETED`; terminals `CANCELLED` / `FAILED`.
  Task termination ⇒ `recovery_state=checkpointed`, not a new phase.
- Async: `TaskGroup`, `asyncio.timeout()`, owned semaphores; never swallow
  `CancelledError`. OpenAI concurrency ≤ 6/process. boto3 off the event loop.
- Explicit deadlines; retry only retryable errors if deadline allows.
- Structured logs: stable events, correlation + debate IDs; no secret/content dumps.
- Ruff only (no Black/isort/flake8). Complexity ≤ 10. import-linter enforces
  domain ← application ← adapters.

## Discord

- Moderator owns command registration. API Gateway + the ingress Lambda own the
  initial Interaction response; the ECS Gateway runtime must not also accept
  the same command/component.
- Four Guild-install-only apps; Public Bot off; OAuth2 Code Grant off; 2FA.
- Runtime intent: `GUILDS` only. Command: Guild-scoped `/shittim`. Signed HTTP
  ingress responds ephemerally within Discord's deadline, persists the request
  before wake, and requests a public status message. ECS later creates the
  starter message + Public Thread + panel after READY/recovery.
- Claim/drain persisted work only when all four clients are READY. HTTP durable
  acceptance remains available while the runtime is stopped or not READY.
- `allowed_mentions.parse=[]`; chunk at 2000 on paragraphs; deterministic labels.
- Publish **only** persisted outbox ops (22-char unpadded base64url UUIDv7 nonce,
  content hash, claim owner/expiry). Long outages: reconcile thread history +
  hash. No second app-layer retry on top of discord.py for the same request.
- Keep archived threads; never auto-unlock locked threads. Sync Guild command
  only when schema hash changes.

## OpenAI

- Read current official docs before changing model/API/schema/retry choices.
- One `AsyncOpenAI`/process; stable Responses API; `store=false`.
  **No** Multi-agent beta / `client.beta.responses` / `multi_agent`.
- Web search only when router says optional/required; persist tool source
  metadata (`web_search_call.action.sources`), not model-invented URLs.
- `responses.parse()` + Pydantic; distinguish refusal / incomplete / parsed /
  transport failure. No raw Discord user IDs to OpenAI.
- `PRODUCTION_POLICY=luna_standard` only. Paid eval: local `--live` + write
  outside the repo. Version model, prompts, and schemas per session.
- Do not reimplement SDK retries for OpenAI, boto3, or discord.py.

## Container

- Builder: frozen deps layer (`uv sync --frozen --no-dev --no-install-project
  --no-editable`) **before** copying app source; then final frozen project sync.
- BuildKit cache on `/root/.cache/uv` (`sharing=locked`); no `UV_NO_CACHE=1` for
  image builds; `UV_PYTHON_DOWNLOADS=0`.
- PR CI: one Buildx builder, digest-pinned `build-push-action`,
  `outputs: type=docker,rewrite-timestamp=true`, no image push, `contents: read`,
  no deploy secrets/OIDC. Production cache export
  may fail without failing the gate.

## AWS (implemented templates; synth/local tests only)

- Region `ap-northeast-1`. SSO for interactive work; no new long-lived keys.
- CDK: Node 24 LTS, exact npm pins, cdk-nag, strict TS, Vitest. **No bootstrap/
  deploy** unless the task explicitly requests it.
- Runtime design: public IPv4 VPC, On-Demand FARGATE only, `desiredCount=0`,
  maximum/running task count 1, ARM64 `512/1024`, `stopTimeout=120`, app cleanup
  ≤90s, non-root, read-only root, Exec off (break-glass separate revision).
- Control plane: API Gateway HTTP API, exactly three application Lambdas outside
  the VPC, and a one-minute EventBridge reconciler. No ALB, NAT Gateway, new
  DynamoDB table, ECS Service Auto Scaling, or FARGATE_SPOT.
- Runtime wake follows durable ingress acceptance. Scale-down requires a stable
  generation and zero pending ingress/tasks/leases/outbox/status work after the
  30-minute fully-idle boundary.
- DynamoDB: on-demand, deletion protection, RETAIN, 35d PITR; 3 lease slots;
  cross-item writes need META `ConditionCheck` in the same transaction.
- ECR: fully immutable tags; deploy by `repository@sha256:<digest>` only.
  Release verification is fail-closed (Signer + Notation + attestations +
  referrers)—STEP-10.
- OIDC: plan/drift = immutable main subject; deploy = `production` environment;
  `aud=sts.amazonaws.com`. The deployment guard currently performs read-only
  diagnostic evaluation and is not connected to a production deploy job.
- Release cleanup treats an exact CloudFormation `ValidationError` naming a
  missing stack as already clean, because a change set cannot outlive its parent
  stack. Generic `does not exist`, `ChangeSetNotFound`, and access failures remain
  distinct fail-closed paths.

## Official docs policy

For design, implementation, review, and incidents: consult **current** official
docs in the same task (AWS, Discord, OpenAI, Python, PyPI, upstream repos).
Blogs/SO are secondary. Record URL, date, version, design consequence. If docs
are unreachable, mark unverified and pause that part—do not invent external
behavior.

## Secrets

Never commit or print: Discord tokens, OpenAI keys, AWS credentials/SSO cache,
private user questions, or full model dumps from private tests. Use `.env.example`
placeholders only; keep real `.env` ignored. No secrets in logs, fixtures,
screenshots, commit messages, README, AGENTS, or Obsidian.

## Testing

Add tests with each slice. Minimum themes: transitions; vote edge cases; phase
concurrency/timeout/cancel; conditional-write conflicts; Discord/DB ordering
failures; OpenAI refusal/invalid/429/auth; single-Bot disconnect; bad Guild/
channel/length/thread states; SIGTERM/restart at every phase with lease + outbox
dedup; log redaction.

- Domain/application coverage ≥ 90%.
- DynamoDB Local via `tools/run_dynamodb_local.py` (Podman preferred; loopback
  random port; no real AWS). Host terminal if sandbox blocks rootless Podman.
- Unit: fakes. Network/paid integration: opt-in only.

## Git discipline

- Default branch `main`; all changes via PR; squash merge; no force-push.
- Public PR jobs: read-only token, no secrets/OIDC, no self-hosted runners.
  Never run fork code via `pull_request_target` except the dedicated
  `discord-repository-events.yml` (metadata only; no PR checkout/artifacts/
  caches/write). Keep negative policy tests.
- Discord notifications: convenience, not SoT. Webhook in Actions Secrets only;
  thread IDs in repo variables; `DISCORD_NOTIFICATIONS_ENABLED=true` in prod;
  no `DISCORD_ALERT_ROLE_ID` (always `allowed_mentions.parse=[]`). Notification
  code must stay Python 3.12-parseable on the runner system interpreter.
- Imperative commit subjects. Do not commit unless the user asks. Preserve
  unrelated dirty worktree changes. Handoff: files, validation, open decisions,
  AWS/Discord/cost/retention impact.

## License

MIT for source, IaC, tools, and public samples. `docs/` and this `AGENTS.md` are
**all rights reserved**. Design proposals → Issues unless the maintainer asks
for a docs PR.
