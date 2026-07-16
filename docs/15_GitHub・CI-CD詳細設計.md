---
aliases:
  - The Shittim Chest GitHub詳細設計
tags: [project, shittim-chest, github, ci-cd, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-07-17
---

# GitHub・CI-CD詳細設計

## 1. Repository・license・community

- Public repository `pitekusu/shittim-chest`、default branchは`main`とする。
- source code、IaC、tool、public sampleはMIT Licenseとし、`docs/`と`AGENTS.md`は対象外とする。
- Issues、Pull Requests、private vulnerability reportingを有効にする。Discussions、Wiki、ProjectsはMVPでは無効とする。
- implementation資産への外部PRを受け付ける。設計文書の変更提案はIssueを入口とし、権利範囲が曖昧な文書PRを直接mergeしない。
- runtimeのGuild/channel/Application ID、display name、persona prompt、secret値をrepositoryへ保存しない。

## 2. Main ruleset

Public GitHub Freeのrepository rulesetを`main`へ適用する。

- Pull Requestを必須とし、単独管理のためrequired approvalは0とする。
- conversation resolutionとlinear historyを必須とし、force pushとbranch削除を禁止する。
- bypass actorは設けない。merge methodはsquashだけを許可し、merge後にhead branchを自動削除する。
- CI実装前は存在しないcheckをrequiredにしない。各checkがmainで1回成功した後、`quality`、`tests`、`security`、`package`、`cdk`、`container-arm64`、`docs-public-safety`をrequired status checksへ追加する。
- emergency時もrulesetをbypassせず、修正PRとProduction Environment承認を使用する。

## 3. Pull Request CI

`ci.yml`はPull Requestとmain pushで実行し、job既定権限を`contents: read`だけにする。PR単位の`concurrency`で古いcommitのCIだけをcancelし、jobごとに固定check名と`timeout-minutes`を定義する。

1. uv lock check、Ruff format/check、mypy strict、import-linter。
2. pytest unit/contract、domain/application coverage 90%以上。
3. pip-audit、gitleaks、public surface scan、`uv export --frozen --format cyclonedx1.5`で生成したCycloneDX SBOMのschema検証。
4. wheel buildとinstall smoke test。
5. TypeScript typecheck/test、CDK assertion、`cdk synth --strict`、cdk-nag。
6. ARM64 container buildとhealth command test。paid/network integrationは実行しない。
7. Obsidian mirror、Markdown、Wiki link、license scopeを検証する。

fork由来を含む`pull_request` jobへsecret、OIDC、write permission、self-hosted runnerを渡さない。fork codeを扱う`pull_request_target`は禁止する。外部contributorのworkflowは毎回maintainer承認を要求する。

### Dependency graph・source SBOM

- GitHubの対応package manager一覧に`uv.lock`が含まれない間は、repository scanだけでPython dependencyを網羅できると仮定しない。
- Pull RequestではlockからCycloneDX 1.5 JSONを生成・検証するだけとし、Dependency Submission APIへのwriteは行わない。
- trustedな`main` push jobで、試験済みの同一`uv.lock`から解決済みdependency snapshotをDependency Submission APIへ送信する。job permissionは当該jobだけ`contents: write`とし、fork codeや未試験artifactを入力にしない。
- GitHub Dependency Graphをrepository単位のmanaged dependency inventoryとし、GitHubのSBOM export endpointからSPDX 2.3 JSONを取得できることを定期確認する。lock更新時は送信snapshot、Graph、CycloneDXの直接dependencyとversion差分を検査する。
- GitHub SBOM exportはrepository dependency inventoryの出力であり、container OS packageを網羅するrelease image SBOMの代替にはしない。

## 4. Production release

Private Free向けの二つのrelease workflowは使用せず、`release.yml`へ統合する。

### Plan job

- `workflow_dispatch`かつmain上のcommit SHAだけを受け付ける。ref、immutable repository ID、対象commitのCI成功をfail closedで検証する。
- release imageを1回だけbuild・試験・ECR pushし、commit SHA tagとdigestを確定する。deploy jobでは再buildしない。
- commit SHA、image digest、SBOM hash、scan result、CDK template hash、CloudFormation change set ARN、version付きSSM parameter名をrelease manifestへ保存する。
- push済みの最終image digestからOS packageとPython runtime dependencyを含むSPDX JSON SBOMを生成する。
- imageにはbuild provenanceとSBOMを別々のattestationとして、full SHAへpinした`actions/attest`で生成する。deprecatedな`actions/attest-sbom`は新規利用しない。
- release manifestにもprovenance attestationを生成する。頻繁なtest buildやsource file単体にはattestationを生成しない。
- 初回は`CDK bootstrap → Stateful/ECR change set実行 → image push → Runtime → Operations`、通常releaseは既存ECRへのpush後に全stackのchange setをprepareする。

### Deploy job

- `production` Environmentを参照し、reviewer `pitekusu`の承認後だけ開始する。単独運用のためself-reviewは許可するが、独立した四眼承認ではないことを明記する。
- Environmentのdeployment branchは`main`だけ、administrator bypassは禁止、wait timerは0とする。
- plan jobと同一runのmanifestを取得し、GitHub artifact attestationのsubject digest、repository identity、commit、image digest、SBOM hash、change set ARNを再検証する。
- ECRへattestationをregistry referrerとしてpushできることをintegration testする。利用中の組合せで未対応ならGitHub artifact attestationへ保持したままdeployを停止し、格納先やverificationをADRで再設計する。
- change setを再生成せず実行し、READY/Discord/OpenAI/AWS connectivity smoke test後にresultとdigestをdeployment summaryへ記録する。
- production専用`concurrency`は`cancel-in-progress=false`、job timeoutを設定する。

### Drift job

`drift.yml`は毎週と手動で実行する。main subject限定のread-only roleを使用し、drift時は同一labelのIssueを更新して自動修復しない。

## 5. OIDC

repositoryはimmutable subject claimを使用する。`aud=sts.amazonaws.com`と`sub`を必ず`StringEquals`で評価し、wildcard、static AWS access key、repository secretのAWS credentialを禁止する。

| Role | Expected subject |
|---|---|
| plan | `repo:pitekusu@12059348/shittim-chest@1302516701:ref:refs/heads/main` |
| drift | `repo:pitekusu@12059348/shittim-chest@1302516701:ref:refs/heads/main` |
| deploy | `repo:pitekusu@12059348/shittim-chest@1302516701:environment:production` |

AWS role作成前にGitHub-hosted runnerの診断jobで実際の`sub`、`aud`、repository IDを表示し、secretを含めず期待値と照合する。不一致時はIAM trustを推測で作らない。plan、deploy、driftは別role・別permission policyとし、`iam:PassRole`は対象ECS role ARNと`ecs-tasks.amazonaws.com`へ限定する。

## 6. Actions・supply chain settings

- repository既定`GITHUB_TOKEN`はread-only、Pull Request approval権限なしとする。
- GitHub-owned Actionと明示allowlistしたActionだけを許可し、全Actionをfull commit SHAへpinする。DependabotにSHA更新を行わせ、version tagだけのpinは禁止する。
- Secret scanning、Push protection、CodeQL default setup APIの`query_suite=extended`、Dependency graph、Dependabot alerts/security updatesを有効にする。
- CodeQLは現在Pythonを対象とし、CDK実装時にJavaScript/TypeScriptを追加する。
- uv、Docker、GitHub Actions、npm/CDKを週次更新する。minor/patchとsecurity updateは安全な単位でgroup化し、major、OpenAI model、Python minor変更は個別PRとして自動mergeしない。
- Dependabot uv updaterがprojectの`required-version`を満たさない場合はversion update全体が`tool_version_not_supported`で停止する。開発・CIはuv 0.11.29へpinしたまま、projectの互換範囲はDependabot公式imageの0.11.8を含む`>=0.11.8,<0.12`とする。updater更新後に下限を上げる場合は公式Dockerfileとlock/update試験を再確認する。
- Dependency GraphのGitHub管理SBOM、PRのCycloneDX source SBOM、release imageのSPDX SBOMを用途別に併用する。互いを代替扱いせず、生成元、commit、image digestをrelease manifestへ記録する。

## 7. Image・artifact・rollback

- ECR tagは`git-<full-sha>`、task definitionはdigestを参照する。
- coverage/test resultは30日、production release manifest、SBOM、attestation、image digest、template/change set summaryは90日保存する。
- secret、OpenAI output、Discord message本文、private runtime configurationをartifactへ含めない。
- rollbackは直前の正常image digestとtask definition revisionを指定し、DynamoDB schema compatibilityを確認してから行う。

## 8. Deployment failure

- build/scan/synth/diff/attestation検証失敗: deployしない。
- Runtime taskがREADYにならない: circuit breaker rollback後、直前digestへ戻す。
- Stateful replacementが表示: deployを停止し、ADR、PITR、backup境界を確認する。
- Environment、ruleset、Secret scanningを設定できない: Actionsを無効化し、解消までimplementation/deployを開始しない。

## 9. 実装状態

Repository visibility、community metadata、ruleset、Environment、managed security settingは公開化時に構成済みである。Dependabot manifestのuv週次更新は実装済みで、Docker、GitHub Actions、npm/CDKは対応manifest導入時に追加する。application workflow、AWS OIDC role、AWS resourceは未実装であり、それぞれの実装工程で本書に従う。

## 10. 公式資料確認記録

| 確認日 | 対象version/service | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-07-16 | GitHub REST API 2026-03-10 | https://docs.github.com/en/rest/repos/rules | Public Free ruleset、bypassなし |
| 2026-07-16 | Environments | https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments | reviewer、branch制限、self-review |
| 2026-07-16 | AWS OIDC | https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws | `sub`、`aud`、Environment subject |
| 2026-07-16 | OIDC reference | https://docs.github.com/en/actions/reference/security/oidc | immutable owner/repository ID subject |
| 2026-07-16 | Secret scanning | https://docs.github.com/en/code-security/concepts/secret-security/secret-scanning | public repositoryのautomatic scan |
| 2026-07-16 | CodeQL | https://docs.github.com/en/code-security/concepts/code-scanning/codeql/codeql-code-scanning | Python default setup |
| 2026-07-16 | Artifact attestations | https://docs.github.com/en/actions/concepts/security/artifact-attestations | release provenance、verify必須 |
| 2026-07-16 | Dependency graph SBOM export | https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/export-dependencies-as-sbom | GitHub管理inventoryをSPDXでexport |
| 2026-07-16 | Supported package ecosystems | https://docs.github.com/en/code-security/reference/supply-chain-security/dependency-graph-supported-package-ecosystems | `uv.lock`はsubmissionで補完 |
| 2026-07-16 | Dependency submission API | https://docs.github.com/en/rest/dependency-graph/dependency-submission | trusted mainだけがsnapshotを送信 |
| 2026-07-16 | Artifact attestations action v4 | https://github.com/actions/attest | provenanceとSBOM attestationを生成 |
| 2026-07-16 | uv 0.11.29 export | https://docs.astral.sh/uv/concepts/projects/export/ | CycloneDX 1.5 exportはpreviewとしてschema検証を必須化 |
| 2026-07-16 | Secure Actions use | https://docs.github.com/en/actions/reference/security/secure-use | fork PR、最小権限、full SHA pin |
| 2026-07-17 | Dependabot uv updater 0.11.8 | https://github.com/dependabot/dependabot-core/blob/main/uv/Dockerfile | 公式updaterの実uv versionをproject互換範囲と照合 |
| 2026-07-17 | uv required version・versioning | https://docs.astral.sh/uv/reference/settings/#required-version、https://docs.astral.sh/uv/reference/policies/versioning/ | PEP 440範囲と同一minor patch互換を採用 |
