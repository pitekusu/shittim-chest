# The Shittim Chest

[![CI](https://github.com/pitekusu/shittim-chest/actions/workflows/ci.yml/badge.svg)](https://github.com/pitekusu/shittim-chest/actions/workflows/ci.yml)
[![Production Release](https://github.com/pitekusu/shittim-chest/actions/workflows/release.yml/badge.svg?branch=main)](https://github.com/pitekusu/shittim-chest/actions/workflows/release.yml)
[![Infrastructure Drift](https://github.com/pitekusu/shittim-chest/actions/workflows/drift.yml/badge.svg?branch=main)](https://github.com/pitekusu/shittim-chest/actions/workflows/drift.yml)
[![License: MIT](https://img.shields.io/badge/Source-MIT-blue.svg)](LICENSE-SCOPE.md)

Production Discord multi-agent deliberation bot. One moderator Bot coordinates
three private-configured personas, shared web evidence, initial opinions,
revised proposals, anonymous voting, and a **Python-calculated** result via
`/shittim`. The winning persona then presents the final decision in its own
voice.

Japanese name: **シッテムの箱** (`shittim_chest`).

## Status

The service is deployed and live in its single private production Guild.
Stateful, Runtime, Operations, and CostGovernance stacks are maintained through
the attested Production Release workflow. Signed Discord HTTP Interactions are
accepted durably while the ARM64 On-Demand Fargate service is at
`desiredCount=0`; the runtime converges `0 → 1 → 0` around queued work.

| Area | Current state |
|---|---|
| Discord | Signed HTTP ingress, `/shittim`, public thread, control panel, status convergence, and four Bot identities are live |
| Deliberation | Initial opinions, revised proposals, anonymous ballots, moderator vote tally, and winner-persona final presentation are live |
| OpenAI | Responses API with Structured Outputs and optional web evidence; production is fixed to Luna standard |
| Persistence | DynamoDB schema v7, fenced leases, durable FIFO ingress, checkpoints, and ordered Outbox v2 |
| Runtime | ARM64 On-Demand Fargate scale-to-zero, normally `0/0/0`, with at most one task and a best-effort participant farewell before normal idle shutdown |
| Supply chain | DHI images, SBOM, VEX, Grype, signatures, attestations, image admission, and immutable CloudFormation Change Sets |
| Operations | CloudWatch alarms/dashboard, EventBridge notifications, SNS email, cost budgets, drift detection, and GitHub → Discord notifications |

Production generation is fixed to **Luna standard** (no runtime escalation).
Responses API Multi-agent beta is intentionally unused; Python owns orchestration.

Slice evidence and PR links: [`docs/20_実装・試験・検証記録.md`](docs/20_実装・試験・検証記録.md),
[`docs/19_実装計画・トレーサビリティ.md`](docs/19_実装計画・トレーサビリティ.md).
The scale-to-zero requirements, commit checkpoints, and completion criteria are
published under [`docs/100_Ondemand Fargate/`](docs/100_Ondemand%20Fargate/).
Contributor/agent rules: [`AGENTS.md`](AGENTS.md).

## Private production setup

The operator runs one guided command. It asks only for missing private values,
hides all input, validates the complete configuration, and writes directly to
GitHub Actions secrets and SSM Parameter Store after confirmation:

```sh
uv run --frozen python tools/configure_production_inputs.py
```

For the v0003 migration, the command validates and reuses the existing v0002
RuntimeConfig and four PersonaConfig values without displaying them, then asks only
for the allowlisted farewell channel. The local-only pointer and private values stay
ignored and are never copied into the repository.

Outside the bounded v0002-to-v0003 migration read, it does not retrieve existing
secret values. It never overwrites, prints, or saves them.
Readiness can be checked without entering values:

```sh
uv run --frozen python tools/configure_production_inputs.py --check
```

## Scale-to-zero runtime

- Discord calls an API Gateway HTTP API. The ingress Lambda verifies the
  Ed25519 signature and timestamp against the untouched raw body before JSON
  parsing or durable acceptance.
- Three application Lambdas have separate responsibilities: Interaction
  ingress, public status publication, and one-minute runtime reconciliation.
- The ECS service is ARM64 **On-Demand Fargate**, `512` CPU units / `1024` MiB,
  with `desiredCount=0` while idle and at most one task while active. The former
  Fargate Spot `desiredCount=1` baseline is superseded.
- The durable ingress FIFO holds at most 20 waiting requests; accepted debates
  continue to use the existing three fenced global slots and are not counted in
  that waiting limit.
- A request still waiting after 3 minutes gets a non-terminal public warning;
  recovery continues until the 15-minute terminal deadline. Scale-down becomes
  eligible 30 minutes after the last debate is *fully* complete, including
  required outbox/status work.
- The deployment guard retains its read-only diagnostic workflow. The
  production deploy job additionally acquires and releases the exact fenced
  DynamoDB lock around attested CloudFormation change-set execution and smoke
  checks.

## Production stack

- **Python 3.14.6** / **uv** (locked), discord.py, OpenAI Responses API, boto3
- **DynamoDB** (on-demand, PITR), **ECS On-Demand Fargate** ARM64 zero-to-one
  singleton (Tokyo)
- **CDK** TypeScript (Stateful, Runtime, Operations, CostGovernance, and ReleaseIdentity deployed)
- **CloudWatch** metric/composite alarms and dashboard, **EventBridge** abnormal
  ECS task-stop notifications, and one TLS-only **SNS** operator topic
- `us-east-1` **CostGovernance** CDK stack with Project/account Budgets and an
  existing-monitor **Cost Anomaly Detection** subscription
- Digest-pinned **DHI** Community images; identity and tmpfs in `container-policy.json`
- **AWS Signer / Notation**, ECR OCI referrers, GitHub artifact attestations,
  immutable OIDC plan/deploy/drift roles, and fail-closed ECS image admission

## Local validation

Install a current **uv 0.11.x**, then from the repo root:

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

`tools/run_dynamodb_local.py` starts digest-pinned DynamoDB Local (Podman or
Docker) on a random loopback port and runs the full locked suite—no AWS
credentials. Prefer the host terminal if a restricted sandbox blocks rootless
Podman.

### Optional images (local)

```sh
podman build --format docker --target production -t shittim-chest:production .
podman build --format docker --target break-glass -t shittim-chest:break-glass .
```

DHI pulls need a read-only registry account (`DHI_USERNAME` / `DHI_TOKEN` as
Actions and Dependabot secrets—not in Git).

### Optional paid evaluation

`tools/evaluate_escalation.py` and related scorers require `--live` and
`OPENAI_API_KEY`, write artifacts **outside** the repository, and are not run by
CI. Production remains Luna standard regardless of historical A/B results.

## Public vs private

This repository holds generic slots and public-safe design only. Production
Guild/channel/Application IDs, display names, persona prompts, Bot tokens, and
API keys are loaded from versioned SSM at deploy time—not stored here.

Report security issues via GitHub private vulnerability reporting:
[SECURITY.md](SECURITY.md).

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md). Source, IaC, tools, and samples are MIT.
Design documents under `docs/` and `AGENTS.md` are all rights reserved—see
[LICENSE-SCOPE.md](LICENSE-SCOPE.md).
