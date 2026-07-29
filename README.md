# The Shittim Chest

[![CI](https://github.com/pitekusu/shittim-chest/actions/workflows/ci.yml/badge.svg)](https://github.com/pitekusu/shittim-chest/actions/workflows/ci.yml)
[![Release Tool Versions](https://github.com/pitekusu/shittim-chest/actions/workflows/tool-versions.yml/badge.svg)](https://github.com/pitekusu/shittim-chest/actions/workflows/tool-versions.yml)
[![Dependency Graph](https://img.shields.io/badge/GitHub-Dependency%20Graph-181717?logo=github)](https://github.com/pitekusu/shittim-chest/network/dependencies)

Discord multi-agent debate bot. One moderator Bot coordinates three participant
personas, shared evidence, revised proposals, anonymous voting, and a
**mechanically calculated** result via `/shittim`.

Japanese name: **シッテムの箱** (`shittim_chest`).

## Status

Design is complete under [`docs/`](docs/). The merged `main` baseline includes
the application core, OpenAI + Web search, signed Discord HTTP Interaction
ingress, DynamoDB **schema v7** / control-record **manifest v2**, ARM64 container
gates, and Stateful/Runtime CDK with On-Demand scale-to-zero. The
current STEP-10-A slice adds immutable GitHub OIDC roles, a
plan/Environment-deploy release workflow, signed normal and break-glass images,
OCI attestations, a canonical release manifest, fenced change-set execution,
read-only drift detection, and ECS `PRE_SCALE_UP` image admission. Nothing in
this repository is connected to a real Discord Application endpoint. AWS is
bootstrapped in both target Regions, and the protected Stateful and
ReleaseIdentity foundations are deployed; Runtime, Operations, CostGovernance,
and the first release remain pending.

| Implemented locally / on merged main | Not done |
|---|---|
| Domain, voting, Protocols, use cases | Private runtime/notification values and first release |
| DynamoDB adapter, leases, outbox | Real Discord Applications / live tokens |
| OpenAI Responses API, router, Evidence | Paid OpenAI in CI |
| Signed HTTP ingress + `/shittim` + panel | Runtime/Operations/CostGovernance deploy |
| Lifecycle, SIGTERM/SIGKILL recovery tests | |
| Container + native ARM64 CI | |
| Scale-to-zero control plane + 3 Lambda boundaries | |
| STEP-09C-A EMF metrics foundation | |
| STEP-09C-B alarms/dashboard/EventBridge | |
| STEP-09C-C Budget/CAD templates | |
| STEP-10-A release supply chain + image admission | |
| GitHub → Discord Forum notifications (STEP-02D) | |

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

It does not read, decrypt, overwrite, print, or save existing secret values.
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
- The deployment guard retains its read-only diagnostic workflow. STEP-10-A's
  production deploy job additionally acquires and releases the exact fenced
  DynamoDB lock around attested CloudFormation change-set execution and smoke
  checks.

## Stack (design and locally verified templates)

- **Python 3.14.6** / **uv** (locked), discord.py, OpenAI Responses API, boto3
- **DynamoDB** (on-demand, PITR), **ECS On-Demand Fargate** ARM64 zero-to-one
  singleton (Tokyo)
- **CDK** TypeScript (Stateful + ReleaseIdentity deployed; remaining stacks synth-only)
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
