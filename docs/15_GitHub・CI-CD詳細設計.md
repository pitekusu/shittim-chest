---
aliases:
  - The Shittim Chest GitHub詳細設計
tags: [project, shittim-chest, github, ci-cd, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-08-10
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
- CI実装前は存在しないcheckをrequiredにしない。STEP-02のmain run成功後、`quality`、`tests`、`security`、`package`、`docs-public-safety`をGitHub Actions App由来に限定してrequired status checksへ追加し、strict checkを有効にした。`container-arm64`、`grype`、`cdk`も各実装stepのmain成功後に追加済みで、2026-07-22時点のrequired status checksはこの8件である。CodeQLとGrypeはstatus名ではなくcode scanning result ruleでHigh以上の新規alertを保護する。
- emergency時もrulesetをbypassせず、修正PRとProduction Environment承認を使用する。

## 3. Pull Request CI

`ci.yml`はPull Requestとmain pushで実行し、job既定権限を`contents: read`だけにする。PR単位の`concurrency`で古いcommitのCIだけをcancelし、jobごとに固定check名と`timeout-minutes`を定義する。

1. uv lock check、Ruff format/check、tyによる`src`・`tests`・`tools`全体検査、import-linter。STEP-03で`application -> domain`の一方向contractを`quality` jobへ追加済みとする。tyは同じ`quality` job内の唯一のtype-check gateとし、`missing-type-argument=error`と`possibly-unresolved-reference=warn`を維持する。
2. pytest unit/contract、domain/application coverage 90%以上。
3. pip-audit、Betterleaks full-history scan、生成fixture contract、public surface scan、Dependency Review、`uv export --frozen --all-groups --format cyclonedx1.5`で生成したCycloneDX SBOMのstrict schema、project、`uv.lock`完全一致検証。
4. wheel buildとinstall smoke test。
5. Markdown/frontmatter/fence/Wiki link/heading、license scope、public file、GitHub workflow syntaxを検証する。非公開Obsidian正本とのbyte一致はlocal pre-PRでのみ検証する。
6. STEP-09Aで`cdk` jobを追加し、最新Active LTSのNode.js 24.18.0と`package-lock.json`を使った`npm ci`、npm audit、TypeScript strict typecheck、Vitest CDK assertion、cdk-nag 3 Validation Plugin、`cdk synth --strict`をcredentialなしで実行する。Runtime/Operations assertionはSTEP-09B/09Cで同じjobへ追加する。
7. `container-arm64`は公開repositoryのnative `ubuntu-24.04-arm`でproduction/fault-test/break-glass targetをbuildする。full SHA固定のBuildx Actionsに加え、Buildx client versionとBuildKit image digestをCI／Releaseで同一値へ固定する。`SOURCE_DATE_EPOCH=0`、`type=docker,rewrite-timestamp=true,compression=gzip,compression-level=6,force-compression=true`もCIとReleaseで共通化する。risk-bound 2 imageでは`builder`と各最終stageを`no-cache-filters`で毎回再生成し、productionのvenvはruntime Python helperでpath順、content、symlink、numeric owner/group、mtimeとcanonical mode（directory `0755`、実行可能regular file `0755`、その他のregular file `0644`）を明示固定して取り込む。`COPY --from=builder`と`COPY --link`は使用しない。builderはlock済みdependencyとapplicationを順にinstallし、RECORD、`uv_cache.json`、全venv mtimeをcanonical化する。CIはproductionのRECORD、両imageのconfig digest、全RootFS diff ID、SBOM、VEXを同じ30日artifactへ保持し、risk gateとの対応を検証する。PR required gateはpolicy-levelの静的config digest baselineを要求しない。Releaseは自身がbuildした両config digestの形式、target分離、risk policy対応をpush前に検証し、push後にECR manifestが同じconfigを参照することを再検証する。別runであるCI artifactとのconfig完全一致はrequired gateにしない。secret、OIDC、registry push、paid/network integrationはCIで使用しない。
8. `grype`は`security`と`container-arm64`の成功後に走り、CycloneDX source SBOMとSPDX ARM64 image SBOMを全件・actionableの2系統でscanする。全件JSONは深刻度やfix有無で除外せず30日artifact保存し、trendとrisk reviewに使う。actionable JSON/SARIFは`--only-fixed`で修正版があるfindingに限定し、`--fail-on high`によりfixable High/Criticalをjobでfail closedにする。SARIF locationはupload前に実在file（sourceは`uv.lock`、imageは`Dockerfile`）へ付け替える。この分離により未修正findingを消さず、今すぐ対処可能なfindingだけをmerge gateにする。
9. DHI runtime/devのOpenVEXをpin済みDocker Scoutで取得し、Docker公開鍵でcosign signatureを検証する。DHI attestationはpublic Rekor logに必ずしも登録されないため公式手順の`--skip-tlog`を使うが、`--verify`と公開鍵signature検証は省略しない。Grype `--vex`適用結果でDockerが`not_affected`と判定したpackage/CVEだけをvendor suppressionとして扱う。それ以外の未修正High/Criticalはtarget別image config digest、owner、根拠、影響、exploitability、再評価条件、90日以内の期限を持つ個別entryがなければmergeを拒否する。policy rootへ静的image baselineは置かず、個別risk acceptanceだけを対象imageのexact config digestへ束縛する。manifest digestはregistryで確定する配布identity、config digestは同じDocker exporterで構築・検査した内容identityとして分離する。releaseはbuildした両configをpush前のtarget別risk gateへ渡し、push後にECRから再取得したconfigとの一致を要求する。署名・SBOM・VEX・attestation・canonical manifestにはECRで再取得したmanifest digestだけを使う。productionまたはbreak-glassのconfig digest変更で旧acceptanceは自動的に無効とする。一括dismiss、無期限例外、根拠なき`not_affected`は禁止する。
10. `container-arm64`はDHI・Docker Scout認証、build、native runtime、SPDX、VEX取得のいずれもfail closedとする。Actions secretsはread-only Docker PATの`DHI_USERNAME`/`DHI_TOKEN`を登録する。fork PRにはsecretが渡らないためDHI imageをpullできず、maintainerが同じcommitをtrusted branchで再現して全required checksを通すまでmergeしない。

Docker build cacheは性能最適化であり、依存関係の正本ではない。`uv.lock`、`--frozen`、digest固定base imageを再現性境界とし、cache missまたはcache evictionでも同一gateを通るimageを再構築できなければならない。`UV_NO_CACHE=1`は使用せず、uv cacheはbuild mountの寿命へ限定する。

risk-bound imageのvenv transferは通常の`COPY --from=builder`を使用しない。productionはruntime imageのPythonとbuild-timeだけmountするhelperで検証済みpathの内容を直接copyし、break-glassはGNU tar streamを使う。いずれもbuilderのread-only venvからpath順、`SOURCE_DATE_EPOCH`、numeric owner/groupを固定して最終stageへ展開する。production helperはprocess umaskやbuilder側の細かなpermission maskに依存させず、実行bitの有無だけを分類入力としてdirectory `0755`、実行可能regular file `0755`、その他のregular file `0644`へ正規化する。symlink targetを維持し、content-freeなcanonical tree digestをbuild logへ出力する。

Docker exporterはcache hitとcache missで既存の圧縮blobと新規圧縮blobを使い分けてはならない。CIのproduction、fault-test、break-glassとReleaseのproduction、break-glassは、`type=docker`と`rewrite-timestamp=true`に加え、`compression=gzip`、`compression-level=6`、`force-compression=true`を共通のexporter条件とする。`BUILDKIT_MULTI_PLATFORM=1`はmanifest listを生成してDocker exporterがloadできないため使用しない。risk-bound imageでは`builder`とdeterministic venv transferを行う最終stageをともにstage限定no-cacheへ含める。CI artifactとRelease buildのraw config digest差分は、保存した全RootFS diff ID、canonical tree digest、SBOM、build recordを使う再現性診断の対象とするが、Production Releaseを停止するrequired gateにはしない。

GHA cacheはGitHubのref access restrictionに従う。forkを含むPull Requestへsecret、OIDC、write permissionを追加せず、cache exportは`ignore-error=true`としてcache service障害やevictionをCI correctness failureへ変えない。build、`load`、container gate、SBOMは引き続きfail closedとする。scopeはjob名に依存しないtarget別固定値にし、別architectureとは共有しない。同じmain commitをreleaseするproduction/break-glass buildだけはCIと同じscopeを読み、同じDocker exporter resultを再利用する。cache miss時もpush前config gateは必須であり、不一致imageをregistryへ送らない。Buildx summaryと診断用`.dockerbuild` recordはSBOMと同じ30日保持とし、imageやcredentialの代替artifactとして扱わない。

fork由来を含む`pull_request` jobへsecret、OIDC、write permission、self-hosted runnerを渡さない。fork codeのcheckout・実行を伴う`pull_request_target`は禁止する。例外は`.github/workflows/discord-repository-events.yml`のmetadata通知だけとし、`contents: read`と`pull-requests: read`、default branchの通知code、head checkout・artifactーcache不使用を専用policy testで強制する。外部contributorのworkflowは毎回maintainer承認を要求する。

### Discord operations notifications

GitHub Actionsの運用通知は[[21_GitHub・Discord通知運用設計]]を正とする。`workflow_run`はrepository管理の`CI`、`Dependency Graph`、`Release Tool Versions`だけをworkflow名とpathの両方で識別し、GitHub管理の同名workflowを除外する。通知workflowは独立し、元workflowのstatus checkを上書きしない。`DISCORD_NOTIFICATIONS_ENABLED` が`true`になるまでは全送信をskipする。

Security Digestの`GITHUB_TOKEN` は`vulnerability-alerts: read`をDependabot Alerts、`security-events: read`をCode scanningに使用する。actionlint 1.7.12はGitHubの新しい`vulnerability-alerts`を未認識のため、この既知診断だけを除外する。代わりに専用policy checkerが`discord-security-digest.yml`の`read` 1件以外を拒否し、`read-all`への権限拡大で回避しない。

### Dependency graph・source SBOM

- GitHubのstatic parser一覧に`uv.lock`はないが、Python repositoryではDependabot graph jobがfull transitive snapshotを生成する。2026-07-17のlive SPDX 2.3 exportとDependency Review APIで、`uv.lock`の全42 external packageとRuff更新差分が認識されることを確認した。
- Pull RequestではlockからCycloneDX 1.5 JSONを生成し、公式strict schema、root name/version、全PyPI package name/version、dependency refを検証する。source SBOMは30日artifactとして保持する。
- GitHub managed graphが完全な間はcustom Dependency Submissionを行わない。user submissionはDependabot graph jobより優先され、重複、上書き、`contents: write`権限を増やすためである。managed inventoryに欠落・停滞が再現した場合だけADRでfallbackを再検討する。
- `dependency-graph.yml`をDependabot更新時刻と毎時開始時のActions混雑を避けた毎週火曜12:17 JSTと手動でmain上だけ実行し、GitHub SBOM export endpointのSPDX 2.3 PyPI package集合と、checkoutしたmainのCycloneDX/`uv.lock`集合をread-onlyで照合する。GitHub SPDX export自体にはcommit SHAがないため、比較前後にmain SHAが`GITHUB_SHA`から動いていないことを確認する。移動時は検証済みを示すgreenにせず明示失敗し、最新mainで再実行する。managed graph反映遅延は30秒間隔・最大10回のbounded pollingで吸収し、stable mainで収束しなければ失敗する。同じrefの重複runは非cancel型concurrencyで直列化し、pendingが複数なら最新確認を優先する。
- GitHub SBOM exportはrepository dependency inventoryの出力であり、container OS packageを網羅するrelease image SBOMの代替にはしない。
- STEP-08Bのimage SBOMはPR/test imageの検証artifactであり、release provenance/SBOM attestationではない。STEP-10ではECRへ一度だけpushしたdigestから再生成し、GitHub artifact attestationでdigestとrepository identityを結ぶ。
- STEP-09Aでnpm ecosystemをDependabot週次更新へ追加する。`package-lock.json`はGitHub Dependency Graph/SBOMのmanaged parserへ委ね、custom Dependency Submissionを追加しない。公開SPDX export上のnpm inventoryはmerge後に確認し、欠落が再現した場合だけ既存のmanaged-first方針に従ってfallbackを検討する。

## 4. Production release

Private Free向けの二つのrelease workflowは使用せず、`release.yml`へ統合する。

`release.yml`はproduction runを未知の統合試験にしない。image pushより前に、required checks、private handle、OIDC claimとaccount/role bindingを確認し、固定concurrency内で前runの未実行`release-*` change setを回収してから、stable stack、stale change set、SSM 11件のmetadata、Signer/ECR/Inspector、cost allocation tag/CAD monitor、CDK assemblyの全file asset、固定tool/VEX取得を順にfail closedで検証する。回収は4固定stack/Region、正規のSHA/run/attempt名、CLI全page、Stack/ChangeSet ARNの同一account/Region、non-nested、未実行stateだけへ限定し、describeで再確認して削除後に全件再列挙する。`release-*` change set名はこのworkflow専用とし、手動AWS操作や別workflowは作成・実行しない。GitHub concurrency外のoperator mutationは直列化されないため、必要時は本releaseを停止して別runbookとして扱う。各provider境界は実行可能helperとprovider response fixtureで回帰し、文字列markerだけを根拠にしない。

### Deployment guard・lock lifecycle

現在の`.github/workflows/production-deploy-guard.yml`は手動診断専用である。read-onlyのguard roleで9個のruntime activity record、1個のruntime state、1個のdeployment lockからなる固定11 record snapshotを取得し、STOPPED/IDLEであるか、lockがopenであるか、malformed/missing recordがないかをfail closedで報告する。このworkflowはlock取得、change set実行、deploy、rollback、lock解放を行わない。診断成功はdeploy許可やTOCTOU対策を意味しない。

実production releaseはEnvironment承認後、最初のlive mutation前にDynamoDB transactionでdeployment lockをUUIDv7 `guard_id`、owner/actor、run ID、commit SHA、fencing tokenとともに原子的にacquireする。lockはchange set検証・実行、ECS deploy、smoke test、必要なrollbackの全間保持し、workflowの`finally`相当で取得時と完全一致する`guard_id`、owner/actor、fencing tokenだけを条件付きでreleaseする。並行run、stale owner、別fencing tokenによるreleaseは拒否する。

lockはexpiry時刻を超えても自動reclaim・自動unlockせず`LOCKED`のままfail closedとする。workflow中断時は運用runbookでGitHub runとCloudFormation/ECSの完了を確認し、取得時の正確なmetadataを使ってidempotent releaseし、immutable release auditを残す。force unlockは設けない。metadataを復元できない場合は変更を禁止しincident扱いとする。

deployment admissionのbreak-glassはECS Exec用break-glass task revisionと別機能である。`incident-response`、`security-investigation`、`service-recovery`の理由enum、actor、run、commit、事前runtime stateを必須とし、STOPPED/IDLE以外のruntime条件だけを監査付きでoverrideできる。既存deployment lock、malformed/missing control record、不正identityはoverrideできない。

### Plan job

- `workflow_dispatch`かつmain上のcommit SHAだけを受け付ける。ref、immutable repository ID、対象commitのCI成功をfail closedで検証する。
- image mutationより前に4 stackのstable/clean状態、versioned runtime/personaを含むSSM SecureString 11件のmetadata、Signer/ECR/Inspector/Cost Explorer API、固定tool archive、Grype DB、署名済みvendor VEXを検証する。secret valueは取得しない。
- `Stateful.assets.json`、`Runtime.assets.json`、`Operations.assets.json`、`CostGovernance.assets.json`の全file asset closureを列挙し、Docker asset 0件、期待4 template、Runtime provider ZIPを必須とする。`CliCredentialsStackSynthesizer`で短命plan roleからbootstrap bucketの64桁content-addressed root keyへ直接`publish-assets --force`し、広いbootstrap file-publishing roleをassumeしない。S3 checksumとassembly/source hashをrelease manifestへ固定し、CloudFormationより前とEnvironment承認後に再検証する。
- release imageを1回だけbuild・試験・ECR pushし、一意なcommit SHA tagとmanifest digestを確定する。ECRは除外なしの完全immutableであり、deploy jobではtag再解決も再buildもしない。
- commit SHA、image digest、4種のOCI referrer artifact digest、SBOM hash、scan result、Signer profile ARN、CDK template hash、CloudFormation change set ARN、version付きSSM parameter名をrelease manifestへ保存する。
- push済みの最終image digestからOS packageとPython runtime dependencyを含むSPDX JSON SBOMを生成する。
- ECR Managed Signingのstatusをimage digest指定でbounded pollingし、期待profileが`COMPLETE`にならなければ停止する。`FAILED`、profile不一致、terminal scan、Inspector terminal/重複、AccessDenied/Validationはpollingせずcontent-freeに即時停止し、明示したthrottling/service transientと`ScanNotFound`だけを有限retryする。AWS公式NotationとSigner pluginを固定・検証して導入し、strict trust policyと期待profile ARNでdigest URIを暗号学的にverifyする。
- imageにはbuild provenanceとSBOMを別々のattestationとして、full SHAへpinした`actions/attest`で生成し、`push-to-registry`でECR OCI referrerへ保存する。deprecatedな`actions/attest-sbom`は新規利用しない。
- ECR scan完了後にfindingをseverity別に正規化したcontent-free vulnerability assessmentをOCI referrerへattachする。拡張スキャンのfinding取得では、repository限定の`ecr:DescribeImageScanFindings`に加えて、AWS公式の読み取り要件である`inspector2:ListFindings`、`inspector2:ListAccountPermissions`、`inspector2:ListCoverage`をresource `*`でplan/deploy roleへ許可し、Inspectorのenable/disable権限は与えない。認可失敗はpollingせず即時にcontent-freeな分類で停止する。critical/high findingは期限・owner付きrisk acceptanceがない限り停止する。
- ECR `list-image-referrers`でAWS Signer signature、SPDX SBOM、build provenance、vulnerability assessmentが全て同じimage digestへ`ACTIVE`で紐付くことを確認する。
- release manifestにもprovenance attestationを生成する。頻繁なtest buildやsource file単体にはattestationを生成しない。
- 初回は`ap-northeast-1/us-east-1 CDK bootstrap → Stateful/ECR/Signer change set実行 → image push/verify → Runtime → Operations → CostGovernance`とする。CostGovernanceはworkloadへ依存しないが、同一releaseでOperationsと同じoperator email、既存AWS managed service monitor ARN、BillingでActiveな`Project` cost allocation tagをfail closedに確認してから実行する。通常releaseは既存ECRへのpush・署名・referrer検証後に全stackのchange setをprepareする。

### Deploy job

- `production` Environmentを参照し、reviewer `pitekusu`の承認後だけ開始する。単独運用のためself-reviewは許可するが、独立した四眼承認ではないことを明記する。
- Environmentのdeployment branchは`main`だけ、administrator bypassは禁止、wait timerは0とする。
- plan jobと同一runのmanifestを取得し、GitHub artifact attestationのsubject digest、repository identity、workflow、commit、image digest、SBOM hash、scan result、Signer profile、OCI referrer artifact digest、change set ARNを再検証する。
- `notation verify`、GitHub attestation verify、ECR signing status、`list-image-referrers`をEnvironment承認後にも再実行する。ECR signing statusの`signingProfileArn`とNotation trust policyの`trustedIdentities`は、どちらもバージョンなしのSigner signing profile ARNへ完全一致させる。4種のreferrer不足、revoked/invalid signature、subject違い、artifact digest違いはfail closedとする。
- task definition template内の全application image URIが`repository@sha256:<digest>`でmanifestと一致し、tag形式が0件であることを確認する。
- live mutation前にdeployment lockをacquireし、change setを再生成せず実行する。READY/Discord/OpenAI/AWS connectivity smoke testと、失敗時のrollbackを含む全間でlockをholdし、完了成否にかかわらず正確なmetadataでreleaseを試みる。release失敗は成功扱いにしない。
- plan開始時のstale sweep、plan失敗、deploy finally、独立cleanup jobは同じchange set cleanup helperを使う。作成/削除中を有限pollし、未実行setを削除後`ChangeSetNotFound`と全page再列挙の両方で消失を確認する。partial createの一時的な未検出は3回連続確認し、AccessDeniedやprovider errorを不存在として扱わない。独立cleanupのartifact取得失敗は`steps.<id>.outcome`で判定し、plan成功時は`needs.plan.outputs.plan_attempt`を正規表現検証して旧attemptのexact nameだけを回収する。manifest/attempt modeは部分deploy済みstackの`EXECUTE_COMPLETE`/`EXECUTE_FAILED`/`OBSOLETE`を再実行不能な消費済みsetとしてskipし、後続の未実行setを回収する。`EXECUTE_IN_PROGRESS`だけは削除せず、manifest modeでは後続を回収後にcleanup全体を失敗させ、attempt modeでは即時停止する。plan出力不正やdownload成功後のmanifest不正はfallbackで隠さず停止する。failed-jobs-only rerunでは旧attempt artifactのdeployを禁止し、cleanup後に全job rerunを要求する。
- failure diagnosticsは新しい`DescribeEvents --filters FailedEvents=true`を使い、deploy roleは4固定stack ARNと`release-*` change-set ARNの両resource typeだけを許可する。実行後change setのoperation eventがstack resourceとして認可される経路を含め、旧`DescribeStackEvents`と広い`Resource: *`は許可しない。plan時のChange Set作成失敗は、削除前に`DescribeChangeSet`のStatus／ExecutionStatus／StatusReasonだけを取得し、account、email、URLをredactしたbounded artifactとjob errorへ保存する。raw応答は保持せず、証拠保存後に既存cleanupを行う。
- deploy roleの`ecs:DescribeTaskDefinition`はresource-level permission非対応のため、他Actionと分離した`Resource: "*"` statementとする。workflowはECS Serviceが返したtask definition ARNを入力し、deployed `application` imageがattested manifestのdigest URIへ一致することを検証する。failure diagnosticsは`continue-on-error`で元のdeploy failureと区別し、独立cleanupが成功しても`needs.deploy.result`がfailureならworkflowを成功扱いにしない。
- `DescribeChangeSet.Parameters`はparameterなしのchange setでAWS CLIがJSON `null`を返す。validatorはfield欠落または`null`だけを空collectionとして扱い、object/string等は拒否する。空collectionでも期待するexact parameterまたはNoEcho parameterが1件でもあれば従来どおりfail closedとし、parameter不要のStatefulだけを通す。
- result、digest、guard metadata、acquire/release audit IDを本文なしでdeployment summaryへ記録する。
- production専用`concurrency`は`cancel-in-progress=false`、job timeoutを設定する。

### Drift job

`drift.yml`は毎週火曜日12:17 JSTと手動で実行する。main subject限定のread-only roleを使用し、drift時は同一labelのIssueを更新して自動修復しない。roleは固定5 stackの`DetectStackDrift`／`DetectStackResourceDrift`、unscopedの`BatchDescribeTypeConfigurations`／status read、および現行templateのCloudFormation resource provider schemaにあるread handler権限だけを持つ。`iam:PassRole`、`kms:Decrypt`、`s3:GetObject`などtemplateが使用しないconditional／data-plane権限は付与せず、resource typeまたは対象property追加時はprovider schemaを再監査する。

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
- uv、GitHub Actions、npm/CDKは週次更新する。Docker ecosystemはDHIの修正を最短で取り込むため毎日09:00 JSTに確認し、Dependabot secretsの`DHI_USERNAME`/`DHI_TOKEN`で`dhi.io`を認証する。minor/patchとsecurity updateは安全な単位でgroup化し、major、OpenAI model、Python minor変更は個別PRとして自動mergeしない。
- Dependabot uv updaterがprojectの`required-version`を満たさない場合はversion update全体が`tool_version_not_supported`で停止する。開発・CIはuv 0.11.29へpinしたまま、projectの互換範囲はDependabot公式imageの0.11.8を含む`>=0.11.8,<0.12`とする。updater更新後に下限を上げる場合は公式Dockerfileとlock/update試験を再確認する。
- Dependency GraphのGitHub管理SBOM、PRのCycloneDX source SBOM、release imageのSPDX SBOMを用途別に併用する。互いを代替扱いせず、生成元、commit、image digestをrelease manifestへ記録する。

## 7. Image・artifact・rollback

- ECR tagは除外なしの完全immutableとし、`git-<full-sha>`等は追跡用に限定する。task definition、deploy、rollbackは常にdigest URIを参照する。
- ECRの同一image digestへAWS Signer signature、SPDX SBOM、build provenance、vulnerability assessmentをOCI reference artifactとして保存する。subjectと4 artifact digestをrelease manifestへ固定する。
- coverage/test resultは30日、production release manifest、SBOM、attestation、image digest、template/change set summaryは90日保存する。
- secret、OpenAI output、Discord message本文、private runtime configurationをartifactへ含めない。
- rollbackは直前の正常image digestとtask definition revisionを指定し、DynamoDB schema compatibilityを確認してから行う。
- rollback中もdeployment lockを保持し、smoke testまで完了した後にのみ正確なmetadataでreleaseする。

## 8. Deployment failure

- build/scan/synth/diff/attestation検証失敗: deployしない。
- Managed Signing失敗・timeout、Notation検証失敗、署名revocation、OCI referrer不足・不一致: deployしない。task起動前hookで検出した場合はservice deploymentをrollbackする。
- Runtime taskがREADYにならない: deployment lockをholdしたまcircuit breaker rollback後、直前digestへ戻し、rollback smoke後にexact releaseする。
- Deployment lockがexpiredまたはrelease失敗: 自動reclaimせず新規deployを停止し、[[17_運用保守・監視・障害対応設計]]の復旧手順を実施する。
- Stateful replacementが表示: deployを停止し、ADR、PITR、backup境界を確認する。
- Environment、ruleset、Secret scanningを設定できない: Actionsを無効化し、解消までimplementation/deployを開始しない。
- partial create、workflow interruption、deploy failure: canonical cleanup helperでmanifest ARNまたは当該runのexact nameだけを対象とし、実行済みsetは削除せず、未実行setの消失を確認する。stackが`ROLLBACK_COMPLETE`なら原因resourceを確認し、stack削除以外で再利用しない。
- 初回Runtime create rollback: 明示LogGroupは`RetainExceptOnCreate`により削除し、通常のstack delete/update replacementではretainする。旧template由来の空LogGroupが残った場合はstream 0、stored bytes 0、Runtime absentを再確認してからexact 7件だけを削除する。

## 9. 実装状態

Repository visibility、community metadata、ruleset、Environment、managed security setting、immutable OIDC subject、両RegionのCDK bootstrap、Stateful、ReleaseIdentity、versioned SSM metadataは構成済みである。Scale-to-Zero application/CDK、diagnostic-only `Production Deploy Guard`、実release workflowのplan/Environment deploy、deployment lock、asset/manifest/change set/admission/drift boundaryは実装済みである。初回Runtime attemptの未publish provider ZIPによるrollback後、原因と再発防止を実装し、hardening IAMのReleaseIdentity先行更新、失敗Runtime/空LogGroup cleanup、Runtime/Operations/CostGovernanceのdeploy、Discord Application endpoint切替と実受入まで完了した。local/CI合格とlive受入の証拠は引き続き区別する。

## 10. 公式資料確認記録

| 確認日 | 対象version/service | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-07-16 | GitHub REST API 2026-03-10 | https://docs.github.com/en/rest/repos/rules | Public Free ruleset、bypassなし |
| 2026-07-16 | Environments | https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments | reviewer、branch制限、self-review |
| 2026-07-16 | AWS OIDC | https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws | `sub`、`aud`、Environment subject |
| 2026-07-16 | OIDC reference | https://docs.github.com/en/actions/reference/security/oidc | immutable owner/repository ID subject |
| 2026-07-30 | ECR enhanced scanning IAM | https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-scanning-enhanced-iam.html | finding取得に必要なInspector read API 3種をplan/deploy roleへ追加し、enable/disableは拒否 |
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
| 2026-07-19 | ECR Managed Signing / status | https://docs.aws.amazon.com/AmazonECR/latest/userguide/managed-signing.html、https://docs.aws.amazon.com/cli/latest/reference/ecr/describe-image-signing-status.html | digest指定の自動署名待機と期待profile検証 |
| 2026-07-19 | ECR OCI v1.1 Referrers | https://docs.aws.amazon.com/AmazonECR/latest/userguide/images.html、https://docs.aws.amazon.com/AmazonECR/latest/APIReference/API_ListImageReferrers.html | 4種のreference artifactをrelease/deploy両jobで照合 |
| 2026-07-19 | AWS Signer Notation verification | https://docs.aws.amazon.com/signer/latest/developerguide/image-verification.html | strict trust policy、digest URI、revocation確認 |
| 2026-07-19 | GitHub registry attestations | https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations | image digestのprovenance/SBOMをECRへpushしてidentity検証 |
| 2026-07-20 | Grype CLI / configuration 0.116.0 | https://oss.anchore.com/docs/reference/grype/cli/、https://oss.anchore.com/docs/reference/grype/configuration/ | `sbom:`入力、`json=<path>`出力、`GRYPE_DB_AUTO_UPDATE`/`GRYPE_DB_CACHE_DIR`、DB max age 120h（1日cacheと整合） |
| 2026-07-20 | GitHub Actions code scanning SARIF upload | https://docs.github.com/en/code-security/code-scanning/integrating-with-code-scanning/sarif-support-for-code-scanning | `security-events: write`、`upload-sarif`によるSecurity tab連携 |
| 2026-07-22 | DHI scan・OpenVEX | https://docs.docker.com/dhi/how-to/scan/、https://docs.docker.com/dhi/core-concepts/vex/ | Scout署名検証済みVEXをGrype `--vex`へ適用し、`not_affected`だけをvendor suppressionに使用 |
| 2026-07-22 | DHI attestation verification | https://docs.docker.com/dhi/how-to/verify/ | `--verify --skip-tlog`でRekor非登録を許容しつつDocker公開鍵signature検証を維持 |
| 2026-07-22 | Dependabot private registries | https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/manage-your-dependency-security/remove-access-to-public-registries | `dhi.io`のDocker registry認証とDependabot secretsを構成 |
| 2026-07-31 | CDK assets / `publish-assets` / `CliCredentialsStackSynthesizer` | https://docs.aws.amazon.com/cdk/v2/guide/assets.html、https://docs.aws.amazon.com/cdk/v2/guide/ref-cli-cmd-publish-assets.html、https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.CliCredentialsStackSynthesizer.html | 全file assetをCloudFormation前に直接publishし、assemblyとS3 checksumへ固定 |
| 2026-07-31 | CloudFormation change set / operation events | https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-updating-stacks-changesets.html、https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_DescribeEvents.html、https://docs.aws.amazon.com/cli/latest/reference/cloudformation/list-change-sets.html、https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_DeleteChangeSet.html | exact ARN cleanup、全page列挙、nested拒否、operation-scoped失敗診断、provider error fail closed |
| 2026-08-02 | ECS Service Authorization Reference | https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonelasticcontainerservice.html | Release Deploy roleの`DescribeTaskDefinition`は独立した`Resource: "*"` statementとし、workflow入力とimage digestのapplication検証を維持 |
| 2026-07-31 | GitHub concurrency / steps context | https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency、https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#steps-context | 同一release workflowを直列化し、artifact downloadは`outcome`でfallback判定 |
| 2026-07-31 | ECR image signing / scan、Inspector ScanStatus | https://docs.aws.amazon.com/AmazonECR/latest/APIReference/API_ImageSigningStatus.html、https://docs.aws.amazon.com/AmazonECR/latest/APIReference/API_ImageScanStatus.html、https://docs.aws.amazon.com/inspector/v2/APIReference/API_ScanStatus.html | pending/terminalを列挙してAccessDeniedやFAILEDを再試行しない |
| 2026-07-31 | CloudFormation `RetainExceptOnCreate` | https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-attribute-deletionpolicy.html | 初回create rollbackの孤児LogGroupを防ぎ、通常delete/updateでは保持 |
| 2026-07-31 | Docker/BuildKit reproducible builds、Python `py_compile`、PyPA installed `RECORD`、Build Push Action v7 | https://docs.docker.com/build/ci/github-actions/reproducible-builds/、https://github.com/moby/buildkit/blob/master/docs/build-repro.md、https://docs.python.org/3/library/py_compile.html、https://packaging.python.org/en/latest/specifications/recording-installed-packages/、https://github.com/docker/build-push-action | `SOURCE_DATE_EPOCH=0`をbuilderへ露出してhash-based `.pyc`を生成し、installed RECORDの三列CSV/LF/全file listingを保持して行順を正規化、全image exporterの`rewrite-timestamp=true`でfile metadataを固定。`imageid`/pushed manifest configをtarget別risk identityへ使用 |
