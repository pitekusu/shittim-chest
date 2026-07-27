# The Shittim Chest

[![CI](https://github.com/pitekusu/shittim-chest/actions/workflows/ci.yml/badge.svg)](https://github.com/pitekusu/shittim-chest/actions/workflows/ci.yml)
[![Release Tool Versions](https://github.com/pitekusu/shittim-chest/actions/workflows/tool-versions.yml/badge.svg)](https://github.com/pitekusu/shittim-chest/actions/workflows/tool-versions.yml)
[![Dependency Graph](https://img.shields.io/badge/GitHub-Dependency%20Graph-181717?logo=github)](https://github.com/pitekusu/shittim-chest/network/dependencies)

Discord multi-agent debate bot. One moderator Bot coordinates three participant
personas, shared evidence, revised proposals, anonymous voting, and a
**mechanically calculated** result via `/shittim`.

Japanese name: **シッテムの箱** (`shittim_chest`).

## Status

Design is complete under [`docs/`](docs/). Implementation on `main` covers the
application core through production composition, Discord interaction runtime,
OpenAI + Web search, DynamoDB persistence (schema v6), ARM64 container gates,
and **synth-only** CDK stacks (Stateful + Runtime).

| Done | Not done |
|---|---|
| Domain, voting, Protocols, use cases | STEP-09C ops/budgets/alarms |
| DynamoDB adapter, leases, outbox | STEP-10 release signing / deploy workflows |
| OpenAI Responses API, router, Evidence | Real Discord Applications / live tokens |
| Discord publisher + `/shittim` + panel | Paid OpenAI in CI |
| Lifecycle, SIGTERM/SIGKILL recovery tests | AWS bootstrap or stack deploy |
| Container + native ARM64 CI | |
| GitHub → Discord Forum notifications (STEP-02D) | |

Production generation is fixed to **Luna standard** (no runtime escalation).
Responses API Multi-agent beta is intentionally unused; Python owns orchestration.

Slice evidence and PR links: [`docs/20_実装・試験・検証記録.md`](docs/20_実装・試験・検証記録.md),
[`docs/19_実装計画・トレーサビリティ.md`](docs/19_実装計画・トレーサビリティ.md).
Contributor/agent rules: [`AGENTS.md`](AGENTS.md).

## Stack (design)

- **Python 3.14.6** / **uv** (locked), discord.py, OpenAI Responses API, boto3
- **DynamoDB** (on-demand, PITR), **ECS Fargate Spot** ARM64 singleton (Tokyo)
- **CDK** TypeScript (local synth; not deployed from this repo yet)
- Digest-pinned **DHI** Community images; identity and tmpfs in `container-policy.json`

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
