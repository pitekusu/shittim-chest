---
aliases:
  - The Shittim Chest GitHub詳細設計
tags: [project, shittim-chest, github, ci-cd, detailed-design]
status: production-1.0
created: 2026-07-16
updated: 2026-09-05
---

# GitHub・CI-CD詳細設計

## 1. Repository policy

- public repositoryのdefault branchは`main`。
- source／IaC／tool／sampleはMIT、`docs/`と`AGENTS.md`はreserved documentとする。
- production secret、private Discord identifier、personaをrepository、artifact名、workflow summaryへ
  含めない。
- normal PR、required checks、CodeQL、squash mergeを用い、mainへ直接pushしない。
- Actionsとthird-party toolはversion／digest／full commit SHAでpinする。
- 依存更新は脆弱性・互換性・CI結果から採否を決める。Nodeの型定義は実行環境のmajorに揃え、
  WebのVite+／core alias／Vitest overrideは一組で移行する。対応を見送る更新は理由を記録し、
  現行構成と両立しない自動更新をDependabot設定で抑制する。
- OpenAI SDKはCore／Recordsとも3系を使用し、HTTPX2のclient・timeout・例外に関する契約試験を
  維持する。3系のDependabot更新保留は解除し、両projectのlockとHTTP境界を揃えて更新する。
  モデル・persona設定の変更とは分ける。
- Nodeの正式toolchainはAWS CDKのLTSサポート対象である24系とし、現在のpinは24.20.0とする。
  `.node-version`をローカル用pinとし、固定ツール検査でCI／Releaseのpinとの一致を確認する。
  root／Webのenginesは`>=24.20.0 <25`、Node型定義も24系に揃える。Node 26は互換性評価の対象に
  留め、採用時にはCDKの対応、Corepackの別途導入、jsdomとNode標準Web APIの競合を確認する。
- Core／Recordsのuvとbuild backendは検証済みの0.12系列とし、Dependabotが使用する0.12.7も許可する。
  系列変更時は両projectの`required-version`／`uv_build`、workflow、Docker stage／container policy、
  Dependabotのignore境界、固定ツール監視の系列を同じPRで揃える。lock／build／SBOMの互換性を確認し、
  main反映後は両projectのDependabot更新jobが成功することを確認する。

### 固定ツールの更新検知

- DependabotのGitHub Actions更新は`uses:`参照を扱う。`with.version`のBuildx、
  `driver-opts`のBuildKit、workflow環境変数、直接downloadするCLIの固定値は別途監視する。
- `.github/tool-versions.json`の`tools`はarchive／checksum／署名identityの正本、`sources`は
  実際のworkflow／package manager設定からversionを読み取る参照mapとする。versionをmapへ複製しない。
  通常CIではnetworkを使わず、固定値の形式と複数workflowの一致を確認する。
- `Release Tool Versions`が毎週水曜13:29 JSTとmainの手動実行で公式releaseを確認する。
  Python、Node、uv、pnpmは明示した対応系列のstable tagを比較し、系列変更を自動採用しない。
  AWS Signer installerはmutableな`latest` URLの配布物・署名・公開鍵をdownloadし、固定checksumとの
  相違だけを検知する。取得物の実行、install、署名鍵の自動差替えは行わない。
- 結果をActions summaryとbot所有の単一open Issueへ集約する。候補が不変ならIssueを再投稿せず、
  全項目が確認済み・最新の場合だけ閉じる。個別の取得失敗は他項目の結果を維持して未確認と表示する。
  CLI終了値は最新`0`、更新候補あり`1`、取得／設定不正`2`。失敗を最新扱いにせず、既存のworkflow通知も維持する。
- 監視jobはmain限定、`contents: read`と`issues: write`だけを使用する。自動merge、version／digestの
  書換え、AWS操作、release dispatchは行わない。候補は通常PRで署名・互換性・必須CIを確認して採用する。

## 2. Continuous Integration

`ci.yml`はPR、main push、manual dispatchで次の8 jobを実行する。

| Job | Scope |
|---|---|
| quality | lock、format、Ruff、ty、import contract、tool policy |
| tests | full pytest、DynamoDB Local |
| security | secret scan、dependency audit、source SBOM |
| package | deterministic wheel／sdist |
| cdk | TypeScript、Vitest、cdk-nag、synth、npm audit |
| container-arm64 | production build、policy、SBOM、config digest |
| grype | raw scan、VEX、fixable／residual risk gate |
| docs-public-safety | mirror、links、public surface、license boundary |

root lockfileのnpm audit、TypeScript、全infra Vitest（Records 3 stackを含む）は、同じtested SHAの
CI `cdk` jobへ集約する。Records CIの`records-infra`はRecords専用synthだけを追加し、共通検証を
再実行しない。`cdk`と`records-gate`の必須check、およびRelease境界の検証は維持する。

CodeQL default setupはPython、JavaScript／TypeScript、Actionsを解析する。branch rulesetのcheck名を
workflow名と一致させ、rerunでflaky failureを隠さない。

`npm audit`／`pnpm audit`は通常どおり必須とする。ただしnpm公式StatuspageがSecurity Audit componentの
劣化と、そのcomponentに紐づくactive incidentを同時に示す間だけ、Node系の外部auditを当該runで
skipしてwarningを残す。Statuspageの取得失敗、schema不正、componentだけが劣化してincidentを確認
できない場合はfail closedとし、audit commandのfindingや通常の非0終了をskip理由にしない。build、test、
CodeQL、Grype、dependency reviewなど他のgateは継続する。

## 3. Container evidence

- canonical ARM64 path contextとDocker exporterでproduction imageを固定SHAからbuildする。
- production targetのconfig digest、SBOM、VEX、Grype、risk resultを同一SHA／runに紐づくimage／scan
  artifact間で照合する。artifactごとの保持fieldはworkflow schemaを正とする。
- PR required gateは静的policy baselineや別runとのconfig digest完全一致を要求しない。
- risk acceptanceが必要なfindingだけ、production image kind、承認済みbuild contextごとの
  実測config digest、finding key、期限へ束縛する。同kindの独立したCI／Release buildは
  複数digestを一acceptanceに有限数登録できるが、未登録digestは拒否する。
- 通常PR／main pushではproduction imageの基本検証を行い、`fault-test` imageのbuildと強制停止・復旧訓練は
  runtimeを検証する手動CIだけで実行する。`fault-test`はproductionのbaselineやrisk acceptance対象外とする。
- manifest digestからconfig digestを推測せず、別exporterや過去runの値を流用しない。

## 4. Production Release

manual workflowは`main`の固定SHAとRuntimeConfig versionを入力にする。

```text
plan
→ image build／push／sign／attestation
→ Lambda bundle／immutable Change Set
→ release evidence
→ production Environment approval
→ deploy
→ structural smoke
→ cleanup／lock release
```

- plan前にrepository identity、OIDC claims、named main checks、CodeQL、private handle metadata、stack、
  active Change Set、deployment lock、runtime activityを確認する。
- Release内でproduction imageを再buildし、各runのconfig digest、fixable High／Critical、
  VEX後residual、production targetのrisk acceptanceを検証する。
- push前に既存referrer一覧を保存し、差分から今回追加したprovenance、SBOM、vulnerability
  attestationを各1件特定する。過去referrerを削除しない。
- Environment承認後にdeployment lockを原子的に取得し、attested Change Setだけを固定順で実行する。
- control record preflightはcurrentまたはimmediately previousの均一な11件だけを受理する。previousの
  場合はlock取得transaction内で全件をcurrentへ移行し、schema before／afterと移行有無をcontent-free
  evidenceへ残す。preflightだけでproduction recordを書き換えない。
- Runtime Change Set実行後はRuntime Stackのstable stateとECS serviceが参照するexact candidate imageを
  照合する。candidateが有効ならcurrent schemaを維持してlockを開き、stableな旧Runtimeのままなら
  全controlをpreviousへ原子的に戻してlockを開く。stackまたはimageの組合せが曖昧ならlockを保持する。
- structural smokeはstack state、ECS desired／running、task image、Image Admission Lambdaを
  検証する。Discord／OpenAIのlive討論はRelease後の別受入である。
- cleanup成功で元のdeploy failureを成功扱いにしない。未実行Change Setを削除し、schema判断が確定した
  場合だけlockを解放する。

ReleaseIdentity自体の更新、failed runのrerun、manual CloudFormation deployは通常Releaseへ暗黙に
含めない。

Records Releaseはexact Runtime Stackから`RuntimeConfigVersion`を読み、`vNNNN`形式を検証して
`LegacyRuntimeConfigVersion`としてRecordsApplication Change Setへ渡す。structural smokeでは匿名の
`GET /api/v1/admin/prompts`が401と`private, no-store`を返すことを確認する。Releaseはruntime promptの
active pointerや初回管理revisionを自動作成しない。

メモリアルロビーPR1とPR2はRelease済みである。PR2はRecords-only Releaseとしてtemporary upload bucket、
generation queue／DLQ、専用API／Worker Lambda、5 routeを配信し、Release中に生成、upload、reset、
active prompt切替などのlive writeを行っていない。

PR3はまずRecords ReleaseでWeb lobby、sidebar／mobile導線、ranking王冠とEdge CSPを配信し、その後の
Core Production ReleaseでDiscord解放通知と`RecordsPublicHostname`から構成したcanonical `/memorial` URLを
taskへ配信する。Records ReleaseはMemorial upload bucketのexact regional S3 domainを
`RecordsMemorialUploadOriginDomain`としてEdge Change Setへ渡す。Memorial upload bucketのCORS originは
`https://shittim.pitekusu.dev`へ固定しているため、Records Releaseは公開hostnameがこのoriginのhostnameと完全一致しない
場合に計画を拒否する。Records Release manifest schema v4も同じ完全一致を検証して公開hostnameを同一SHAの署名済み
evidenceへ固定し、配信後にApplication／Edge parameterとEdge public originが一致することを確認する。Core Releaseは
同一SHAで最新のcompleted Records Releaseだけを選び、そのlatest attemptが成功していない
場合は過去の成功runへfallbackしない。exact run／attemptのartifact、signer／source digest、manifest SHAを検証する。
検証済みRecords manifest自体をCore evidence artifactへ同梱し、deploy jobでもRecords Releaseのsignerと同一SHAを
再検証してから、必要な各stepがhostnameを直接抽出する。secret maskingで抑止され得るGitHub job outputやmutableな
repository variable、Records Stackの追加参照へhostnameを依存させない。PR3はPR2のAPI、storage、generation、IAM契約を
変更せず、新しいAWS resourceを作らない。
各Releaseは既存の承認境界に従い、生成やresetのlive writeを自動実行しない。
Records Lambda bundleのnative dependencyはPython 3.14の`aarch64-manylinux_2_28`に対応するbinary wheelだけを
hash検証付きで解決する。x86_64 runner上の現在architectureでPillow等をinstallせず、ARM64 Lambdaで
loadできないnative extensionをartifactへ混入させない。

## 5. Scheduled workflows

- Infrastructure Drift: 毎週火曜、5 stackをread-only検査し、単一Issueを更新する。
- Dependency Graph: 毎週火曜、managed dependency inventoryを確認する。
- Tool Versions: 毎週水曜、pinの更新候補を検出する。更新は別の通常PRで行う。
- Discord Security Digest: 毎日、Security情報の補助digestを送る。
- repository／workflow notifications: GitHub eventをDiscord Forumへ補助通知する。

scheduleのexact時刻、対象workflow、権限は`.github/workflows/`を正とする。

## 6. Artifact and secret safety

- checkoutは`persist-credentials=false`、workflow permissionはjob単位の最小権限とする。
- untrusted PR codeへAWS credential、production Environment secret、DHI credentialを渡さない。
- artifactはcontent-free名を使い、retentionを設定し、download後にdigest／schema／SHAを再検証する。
- GitHub summaryへattestation URLを出す場合はpublic repository ownerを意図せずmaskしない。

## 7. 公式資料確認記録

| 確認日 | 対象 | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-08-14 | GitHub Actions security | https://docs.github.com/en/actions/reference/security/secure-use | pin、permission、untrusted PR境界 |
| 2026-08-14 | GitHub Environments | https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments | production approval boundary |
| 2026-08-14 | GitHub OIDC for AWS | https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws | short-lived role identity |
| 2026-08-14 | Artifact attestations | https://docs.github.com/en/actions/concepts/security/artifact-attestations | provenance／SBOM evidence |
| 2026-08-14 | CodeQL | https://docs.github.com/en/code-security/concepts/code-scanning/codeql-code-scanning | 3-language analysis |
