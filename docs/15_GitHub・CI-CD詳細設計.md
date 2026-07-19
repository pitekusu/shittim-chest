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
- CI実装前は存在しないcheckをrequiredにしない。STEP-02のmain run成功後、`quality`、`tests`、`security`、`package`、`docs-public-safety`をGitHub Actions App由来に限定してrequired status checksへ追加し、strict checkを有効にする。`cdk`と`container-arm64`は各実装stepのmain成功後に追加する。CodeQLはstatus名ではなくcode scanning result ruleでHigh以上を保護する。
- emergency時もrulesetをbypassせず、修正PRとProduction Environment承認を使用する。

## 3. Pull Request CI

`ci.yml`はPull Requestとmain pushで実行し、job既定権限を`contents: read`だけにする。PR単位の`concurrency`で古いcommitのCIだけをcancelし、jobごとに固定check名と`timeout-minutes`を定義する。

1. uv lock check、Ruff format/check、mypy strict、tyによる`src`・`tests`・`tools`全体検査、import-linter。STEP-03で`application -> domain`の一方向contractを`quality` jobへ追加済みとする。tyは同じ`quality` job内で必須実行し、beta期間中はmypyを置換しない。
2. pytest unit/contract、domain/application coverage 90%以上。
3. pip-audit、Betterleaks full-history scan、生成fixture contract、public surface scan、Dependency Review、`uv export --frozen --all-groups --format cyclonedx1.5`で生成したCycloneDX SBOMのstrict schema、project、`uv.lock`完全一致検証。
4. wheel buildとinstall smoke test。
5. Markdown/frontmatter/fence/Wiki link/heading、license scope、public file、GitHub workflow syntaxを検証する。非公開Obsidian正本とのbyte一致はlocal pre-PRでのみ検証する。
6. TypeScript typecheck/test、CDK assertion、`cdk synth --strict`、cdk-nagはSTEP-09で追加する。
7. `container-arm64`は公開repositoryのnative `ubuntu-24.04-arm`でproduction/fault-test targetをbuildする。builderは`pyproject.toml`と`uv.lock`だけをcopyして`uv sync --frozen --no-dev --no-install-project --no-editable`を実行した後、application sourceをcopyしてprojectをinstallする。`/root/.cache/uv`は`sharing=locked`のBuildKit cache mountとし、runtime imageへcopyしない。既定Python image以外の取得を`UV_PYTHON_DOWNLOADS=0`で禁止する。image config、read-only/non-root/capability、health、SIGTERM/SIGKILL recoveryを検査し、Syft v1.48.0でOS/runtime dependencyを含むSPDX JSONを生成して30日保持する。secret、OIDC、registry push、paid/network integrationは使用しない。

Docker build cacheは性能最適化であり、依存関係の正本ではない。`uv.lock`、`--frozen`、digest固定base imageを再現性境界とし、cache missまたはcache evictionでも同一gateを通るimageを再構築できなければならない。`UV_NO_CACHE=1`は使用せず、uv cacheはbuild mountの寿命へ限定する。

fork由来を含む`pull_request` jobへsecret、OIDC、write permission、self-hosted runnerを渡さない。fork codeを扱う`pull_request_target`は禁止する。外部contributorのworkflowは毎回maintainer承認を要求する。

### Dependency graph・source SBOM

- GitHubのstatic parser一覧に`uv.lock`はないが、Python repositoryではDependabot graph jobがfull transitive snapshotを生成する。2026-07-17のlive SPDX 2.3 exportとDependency Review APIで、`uv.lock`の全42 external packageとRuff更新差分が認識されることを確認した。
- Pull RequestではlockからCycloneDX 1.5 JSONを生成し、公式strict schema、root name/version、全PyPI package name/version、dependency refを検証する。source SBOMは30日artifactとして保持する。
- GitHub managed graphが完全な間はcustom Dependency Submissionを行わない。user submissionはDependabot graph jobより優先され、重複、上書き、`contents: write`権限を増やすためである。managed inventoryに欠落・停滞が再現した場合だけADRでfallbackを再検討する。
- `dependency-graph.yml`をDependabot更新時刻と毎時開始時のActions混雑を避けた毎週火曜12:17 JSTと手動でmain上だけ実行し、GitHub SBOM export endpointのSPDX 2.3 PyPI package集合と、checkoutしたmainのCycloneDX/`uv.lock`集合をread-onlyで照合する。GitHub SPDX export自体にはcommit SHAがないため、比較前後にmain SHAが`GITHUB_SHA`から動いていないことを確認する。移動時は検証済みを示すgreenにせず明示失敗し、最新mainで再実行する。managed graph反映遅延は30秒間隔・最大10回のbounded pollingで吸収し、stable mainで収束しなければ失敗する。同じrefの重複runは非cancel型concurrencyで直列化し、pendingが複数なら最新確認を優先する。
- GitHub SBOM exportはrepository dependency inventoryの出力であり、container OS packageを網羅するrelease image SBOMの代替にはしない。
- STEP-08Bのimage SBOMはPR/test imageの検証artifactであり、release provenance/SBOM attestationではない。STEP-10ではECRへ一度だけpushしたdigestから再生成し、GitHub artifact attestationでdigestとrepository identityを結ぶ。

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
- GitHub-owned Actionと明示allowlistしたActionだけを許可し、全Actionをfull commit SHAへpinする。Dependabotに同一行のversion commentを使ってSHA更新させ、version tagだけのpinは禁止する。Betterleaksとactionlintは`.github/tool-versions.json`へversion、release archive名、SHA-256を固定し、実行時latestを本番gateへ直接取り込まない。
- Betterleaksはofficial releaseの`checksums.txt`、Sigstore bundle、archiveをすべて固定SHA-256で検証し、`cosign verify-blob`でrelease workflow identityとGitHub Actions OIDC issuerを照合する。署名済みchecksum内のarchive digestとrepository pinも一致させる。Sigstore installer Actionはfull commit SHAとcosign versionを固定し、selected Actions allowlistへ限定追加する。
- `security` check名を維持したままBetterleaks 1.6.1をfull historyへ実行する。redactionを有効化し、provider validation optionは使用しない。CIで毎回、sourceやworktreeへcredential文字列を保存せずtemporary Git objectとして生成するinvalid GitHub token形式と安全なplaceholderを別Git repositoryへcommitし、Betterleaksがpositiveを拒否しnegativeを許可することをcontract testする。
- `tool-versions.yml`は毎週水曜13:29 JSTと手動でGitHub Releases latest APIをread-only照合し、差分時に失敗してoperatorへ更新を促す。自動更新・自動mergeは行わず、新versionはarchive digest、署名identity、full-history scan、false positiveを別PRで確認する。
- STEP-02Bの複数PR head、main run、full-history、generated contract、Sigstore、latest-release workflowが全合格したため、STEP-02CのPR `#13`でGitleaksを撤去した。二重scannerによる継続的な実行時間・更新負担を避け、GitHub managed Secret scanning、Push protection、Betterleaksを防御層とする。検出coverageの具体的な欠落が再現した場合だけ別ADRで第二scannerを再検討する。
- Secret scanning、Push protection、CodeQL default setup APIの`query_suite=extended`、Dependency graph、Dependabot alerts/security updatesを有効にする。
- CodeQLは現在Pythonを対象とし、CDK実装時にJavaScript/TypeScriptを追加する。
- uvとGitHub Actionsを週次更新する。Docker、npm/CDKはmanifest導入時に追加する。minor/patchとsecurity updateは安全な単位でgroup化し、major、OpenAI model、Python minor変更は個別PRとして自動mergeしない。
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

Repository visibility、community metadata、ruleset、Environment、managed security settingは公開化時に構成済みである。Dependabotのuv/GitHub Actions更新、read-only CI、managed SBOM照合、5 strict check、CodeQL ruleは運用済みである。STEP-02BでBetterleaksの段階移行gateとrelease-tool version監視を追加し、STEP-02CでGitleaksを撤去して単独運用へ移行済みである。既存`security` required check名は変更していない。application workflow、AWS OIDC role、AWS resourceは未実装である。

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
| 2026-07-17 | Supported package ecosystems | https://docs.github.com/en/code-security/reference/supply-chain-security/dependency-graph-supported-package-ecosystems | static parser一覧に`uv.lock`はないためmanaged graphを実測検証 |
| 2026-07-17 | Dependency submission API | https://docs.github.com/en/rest/dependency-graph/dependency-submission | user submissionがmanaged graphより優先されるため、現状はfallbackに限定 |
| 2026-07-16 | Artifact attestations action v4 | https://github.com/actions/attest | provenanceとSBOM attestationを生成 |
| 2026-07-16 | uv 0.11.29 export | https://docs.astral.sh/uv/concepts/projects/export/ | CycloneDX 1.5 exportはpreviewとしてschema検証を必須化 |
| 2026-07-16 | Secure Actions use | https://docs.github.com/en/actions/reference/security/secure-use | fork PR、最小権限、full SHA pin |
| 2026-07-17 | Dependabot uv updater 0.11.8 | https://github.com/dependabot/dependabot-core/blob/main/uv/Dockerfile | 公式updaterの実uv versionをproject互換範囲と照合 |
| 2026-07-17 | uv required version・versioning | https://docs.astral.sh/uv/reference/settings/#required-version、https://docs.astral.sh/uv/reference/policies/versioning/ | PEP 440範囲と同一minor patch互換を採用 |
| 2026-07-17 | Python Dependabot graph job | https://docs.github.com/en/code-security/concepts/supply-chain-security/dependency-graph-data | full transitive managed snapshotをcustom submissionより優先 |
| 2026-07-17 | Betterleaks 1.6.1 | https://github.com/betterleaks/betterleaks | Git/full-history scan、redaction、Gitleaks config互換、validation opt-in、release assetを確認 |
| 2026-07-17 | Betterleaks scanning | https://github.com/betterleaks/betterleaks/blob/main/docs/scanning.md | `git`、JSON report、redaction、validation無効の実行契約へ反映 |
| 2026-07-17 | Betterleaks security policy | https://github.com/betterleaks/betterleaks/blob/main/.github/SECURITY.md | latest releaseのみsupportされるため週次version検知を追加 |
| 2026-07-17 | Gitleaks maintenance policy | https://github.com/gitleaks/gitleaks | feature complete/security patchのみとBetterleaks移行案を確認し並行期間を採用 |
| 2026-07-17 | cosign blob verification 3.0.6 | https://docs.sigstore.dev/cosign/verifying/verify/ | release checksumのcertificate identity・OIDC issuer・bundle検証 |
| 2026-07-17 | Dependency Review API 2026-03-10 | https://docs.github.com/en/rest/dependency-graph/dependency-review | `uv.lock`全packageと更新差分をlive APIで確認 |
| 2026-07-17 | uv CycloneDX 1.5 preview | https://docs.astral.sh/uv/concepts/projects/export/ | strict schemaとlock inventory gateを追加 |
| 2026-07-17 | setup-uv v8.3.2 | https://github.com/astral-sh/setup-uv/releases/tag/v8.3.2 | uv 0.11.29、Python 3.14.6をfull SHA固定Actionで導入 |
| 2026-07-17 | Scheduled workflow | https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule | 毎時開始を避け火曜12:17 JST、default branch、遅延/dropを監視 |
