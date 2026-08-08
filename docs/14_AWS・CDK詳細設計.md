---
aliases:
  - The Shittim Chest AWS詳細設計
tags: [project, shittim-chest, aws, cdk, ecs, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-07-30
---

# AWS・CDK詳細設計

## 1. Environment

- 本番workloadは単一AWS accountの`ap-northeast-1`とし、別のAWS開発環境を作らない。AWS BudgetsとCost Explorer APIは東京endpointを提供しないため、account-globalなcost governance stackだけを`us-east-1`へ配置する。
- localとCIはfake、DynamoDB Local、SDK contract testを使用し、AWS credentialをCIへ渡さない。外部接続確認は本番deploy後の限定smoke testで行う。
- 全resourceへ`Project=shittim-chest`、`Environment=production`、`ManagedBy=cdk`を付ける。

## 2. CDK app・stack

| Stack | Resource | Policy |
|---|---|---|
| `ShittimChest-Prod-Stateful` | DynamoDB、ECR、AWS Signer profile、ECR Managed Signing configuration | termination protection、DynamoDB/ECR/Signer profile `RETAIN` |
| `ShittimChest-Prod-Runtime` | VPC、SG、ECS cluster/service/task、HTTP API、Ingress/Status Publisher/Reconciler Lambda、IAM、app/break-glass Exec log group | Statefulへ一方向依存 |
| `ShittimChest-Prod-Operations` | dashboard、alarm、SNS、EventBridge | `ap-northeast-1`でRuntime/Statefulを監視。Container Insightsは作成・有効化しない |
| `ShittimChest-Prod-CostGovernance` | Budget、Cost Anomaly Detection subscription | Cost Management endpointに合わせ`us-east-1`。workload stackへ依存しない |

L2 constructを優先し、L1/escape hatchはADRで理由を残す。construct IDとlogical IDは初回deploy後に変更しない。`cdk.context.json`をcommitし、`cdk-nag`の`AwsSolutionsChecks`と`cdk synth --strict`を必須とする。

STEP-09Aでは最新Active LTSのNode.js 24.18.0、TypeScript 7.0.2、CDK CLI 2.1132.0、`aws-cdk-lib` 2.261.0、constructs 10.7.0、cdk-nag 3.0.1、Vitest 4.1.10を完全固定し、`package-lock.json`を正本とする。Node.js 26はCurrentでありLTS化前のため採用しない。CDK推奨feature flagを`cdk.json`へ全て明示し、将来のdefault変更でtemplateが暗黙変化しないようにする。cdk-nag 3は旧Aspect APIではなくCDK `Validations` pluginとして登録する。

STEP-09Aの`StatefulStack`は次を実装する。

- stack名は`ShittimChest-Prod-Stateful`、termination protectionを有効にする。
- DynamoDB table名は`shittim-chest-production`。`PK`/`SK`、ALL projectionの`gsi1`/`gsi2`、on-demand、AWS-managed encryption、deletion protection、PITR 35日、`RETAIN`とする。TTLとmaximum throughputは設定しない。
- ECR repository名は`shittim-chest`。暗号化propertyは指定せずECR既定暗号化を使用し、除外filterなしの完全`IMMUTABLE`、`RETAIN`とする。repository側の基本scan-on-pushは無効とし、registry scanning configurationで`shittim-chest`だけを対象とするAmazon Inspector拡張スキャン（`ENHANCED`、`CONTINUOUS_SCAN`）を有効にする。lifecycleはuntagged imageと全tag付きimageをそれぞれ直近3世代だけ保持し、それより古いimageを自動削除する。
- AWS Signer profile `shittim_chest_ecr`を`Notation-OCI-SHA384-ECDSA`、署名有効期間135か月、`RETAIN`で作成する。registry全体で一意な`AWS::ECR::SigningConfiguration`はrepository filter `shittim-chest`だけへ同profileを適用し、push時のECR Managed Signingを有効にする。別repositoryのruleを追加するときは同resourceへ集約する。
- `Project`、`Environment`、`ManagedBy` tagをapp rootから付与する。
- local/PRではdummy accountを使うassertionとcredentialなしのstrict synthだけを行い、bootstrap、deploy、AWS resource作成は行わない。

旧STEP-09BのSpot・`desiredCount=1` RuntimeStackは、PR `#85`のscale-to-zero sliceで置き換えた。現行の`RuntimeStack`はVPC、SG、ECS cluster/service、平常・break-glass task definition、HTTP API、3 Lambda、1分周期EventBridge rule、IAM role、SSM SecureString参照、CloudWatch Logsを実装する。image digestとruntime config versionはCloudFormation parameterとし、形式を`^sha256:[0-9a-f]{64}$`と`^v[0-9]{4}$`でfail closedに検証する。image digestにdefaultを設けず、releaseは平常とbreak-glassの検証済みdigestを必ず明示する。local/PRでは構成assertion、cdk-nag、credentialなしのstrict synthまでとし、AWS resourceはdeployしない。

Runtimeの明示CloudWatch LogGroup 7件は`DeletionPolicy=RetainExceptOnCreate`、`UpdateReplacePolicy=Retain`とする。初回stack createがrollbackしたときだけ削除して同名resourceを孤児化させず、成功後の通常stack deleteまたはreplacementでは90日logを保持する。旧`Retain` templateの初回rollbackで残った空LogGroupは、対象stack不在、log stream 0、stored bytes 0を再確認してからexact nameだけを運用cleanupする。

Scale-to-ZeroのCDK/application、両Regionのbootstrap、Stateful、ReleaseIdentityは実環境へ導入済みである。初回RuntimeはCDK生成provider ZIPのasset publish漏れでrollbackし、Operations/CostGovernanceはresourceを作成していない。再実行前に失敗Runtimeと旧template由来の空LogGroupをcleanupし、全CDK file assetを先行publishする。Discord ApplicationとInteractions Endpoint URLは未変更である。

release対象のStateful、Runtime、Operations、CostGovernanceは`CliCredentialsStackSynthesizer`を使い、assemblyの4 templateとRuntime provider ZIPを短命plan roleの現在credentialからbootstrap S3へ直接publishする。destinationは各Regionの固定asset bucketと64桁content hash root keyの`.json`/`.zip`だけに限定し、`GetBucketLocation`とexact objectの`GetObject`/`PutObject`だけを許可する。fileは5 MiB未満、ZIP sourceは1 MiB・1,000 files・path合計512 KiB以下にfail closedで制限し、S3 `ChecksumSHA256`がsingle-part全体hashである境界を維持する。標準bootstrap file-publishing roleはasset削除を含む広い権限を持つためrelease planからassumeしない。ReleaseIdentity自身のoperator更新は標準CDK synthesizerを維持する。

STEP-09C-Bでは`OperationsStack`を追加し、Runtime/Statefulの後に一方向依存させる。deploy時必須・defaultなし・`NoEcho`の`OperatorNotificationEmail` parameter、TLS必須の単一SNS topic、email subscription、9 metric alarm、critical/warningの2 composite alarm、1 dashboard、異常ECS task stop用EventBridge ruleだけを作成する。Container Insights、helper Lambda、CloudWatch Logs event capture、KMS customer keyは追加しない。SNSは本文やuser contentではなくalarmと絞り込んだlifecycle metadataだけを扱うため、費用とkey-policy運用を増やすcustomer managed KMS keyを使用しない。subscriptionはdeploy後にoperatorが受信emailから確認するまで`PendingConfirmation`であり、未確認状態を運用開始扱いにしない。確認URLは直接開かず、topic ownerのAWS認証済みCLIへtokenを非表示入力し、`AuthenticateOnUnsubscribe=true`で確認する。URL/tokenをchat、shell history、log、文書へ保存しない。有効なsubscription ARN、`PendingConfirmation=false`、`ConfirmationWasAuthenticated=true`を確認するまで通知経路を有効とみなさない。外部解除で`Deleted`になったCloudFormation管理subscriptionは手動のunmanaged subscription追加で隠さず、単独のCloudFormation replacementとして復旧する。

EventBridgeは`ECS Task State Change`、対象cluster/service、`lastStatus=STOPPED`に加え、AWS公式の異常系`stopCode`である`TaskFailedToStart`、`EssentialContainerExited`、`SpotInterruption`、`TerminationNotice`だけをSNSへ送る。`UserInitiated`と`ServiceSchedulerInitiated`は計画scale-down/deploy通知ノイズを避けるため除外する。target payloadはtask ARN、cluster ARN、stop code/reason、exit code、時刻だけに絞り、元event全体を転送しない。

STEP-09C-Cでは`us-east-1`へ独立`CostGovernanceStack`を追加する。Project 20 USDとaccount 30 USDの月次`NetUnblendedCost` Budgetを作成し、各Budgetはactual 80%、actual 100%、forecasted 100%を`GREATER_THAN`で通知する。自動停止やBudget Actionは作成しない。Project Budgetはdeprecatedな`CostFilters`ではなく`FilterExpression`を使い、activeなuser-defined tag `user:Project=shittim-chest`だけを対象にする。

Cost Anomaly Detectionは`AWS::CE::AnomalyMonitor`を作成せず、deploy時必須の`ExistingServiceAnomalyMonitorArn`でaccount既存のAWS managed `SERVICE` monitorを参照する。subscriptionはDAILY email、`ANOMALY_TOTAL_IMPACT_ABSOLUTE >= 10 USD`の`ThresholdExpression`を使用する。Cost ManagementとOperationsはそれぞれdefaultなし`NoEcho`の`OperatorNotificationEmail`を持ち、release manifestが同じ値を両stackへ渡す。実addressとmonitor ARN実値はGit、Obsidian、template outputへ保存しない。

## 3. Network

- IPv4 CIDRは`10.42.0.0/24`、2 AZに各1つの`/26` Public Subnetだけ、NAT Gatewayなし、Internet Gatewayあり。
- Fargateは`awsvpc`、`AssignPublicIp=ENABLED`、routeは`0.0.0.0/0 -> IGW`。
- Security Groupはingress ruleなし。egressはTCP 443を許可する。
- ALB、NAT instance、DNS64、NAT64、Service Connectは作成しない。
- Discordからの公開ingressはAPI Gateway HTTP APIで受け、DiscordIngress Lambdaの固定`live` aliasへ統合する。IngressだけにSnapStartを適用し、実測したcontent-addressed Lambda ZIPのSHA-256、Lambda bundle key、versioned Runtime Config path、moderator Public Key pathへ束縛したpublished versionをaliasから参照する。Runtime ConfigとPublic KeyはCloudFormationのSSM dynamic referenceでLambda環境へ解決し、request中のSSM readを行わない。Public Key rotation時はRuntime Config versionをbumpして新versionをreleaseする。`$LATEST`、Provisioned Concurrency、EFSは使用しない。3 LambdaはVPC外に配置し、LambdaのためのNAT GatewayやVPC endpointを追加しない。
- ECS taskのSecurity Groupはinboundを持たず、HTTP APIからECSへの直接routeも作成しない。Ingress Request、status更新要求、runtime control recordは既存DynamoDB tableを介して連携する。
- VPC Flow Logsはno-ingress・TCP 443 outboundのみの単一task MVPでは費用対効果が低いため作成しない。セキュリティincidentの調査でnetwork visibility不足が実証された場合はADRで再評価する。
- Discord/OpenAIがAAAAを公式supportし、24時間canaryを満たし、IPv6-only移行時にbreak-glass ECS Execを廃止する判断が完了するまでIPv6-onlyへ移行しない。

## 4. ECS・Fargate

| 項目 | 値 |
|---|---|
| Capacity Provider | On-Demand `FARGATE`のみ。通常Serviceに`FARGATE_SPOT`を含めない |
| desired count | 平常0、未処理request/recoverable workがあるときのみ1 |
| maximum running task | 1 |
| CPU / Memory | 512 CPU units / 1,024 MiB |
| Architecture | ARM64固定。互換性変更は別ADRとimage/CDK再検証を必須とする |
| Platform | Linux Fargate 1.4.0以上 |
| Deployment | minimum healthy 0%、maximum 100%、stop-before-start |
| AZ rebalancing | 無効。maximum 100%と両立させ、二重Bot接続を防ぐ |
| stop timeout | 120秒 |
| Circuit breaker | enable + rollback |
| Container Insights | 無効。account defaultを変更せず、このclusterでも有効化しない |

1分周期のRuntime ReconcilerがDynamoDBのruntime stateと未処理workを正本とし、必要時だけ`desiredCount=1`へ収束させる。未処理Ingress、recovery、lease、outbox、status/panel更新がなく、すべての討論が完全終了した後にIDLE開始時刻を固定する。その30分後に、generationと空状態を再検証して`desiredCount=0`へ収束させる。通常停止中はtask 0が正常状態である。

application側のgraceful shutdown deadlineは90秒とし、`stopTimeout=120`の残り30秒をDiscord client close、log driver、container runtimeの終了余裕にする。Reconcilerによる通常scale-down、deploy、ホスト異常のいずれでもSIGTERMとconfigured `stopTimeout`後のSIGKILLが実行され得るため、container実装時は値の省略を禁止する。

## 5. Container definition

- 平常taskはinit process有効、application userはnon-root、read-only root filesystem、privileged無効、Linux capability全削除、ECS Exec無効とする。
- app health checkはtask固有tmpfs上のevent-loop heartbeat鮮度だけを確認し、Discord/OpenAI障害をrestart理由にしない。
- `awslogs` modeは`blocking`を明示し、secret・質問・回答全文をstdoutへ出さない。
- applicationとbreak-glass Execは専用log groupに分け、各90日保持、`RETAIN`、AWS-managed encryption、CloudWatch Logs data protectionによるcredential・個人識別情報のmaskを適用する。
- 一時書込みはheartbeat用の`/tmp/shittim-chest` tmpfs mount（1 MiB、`nosuid,nodev,noexec,uid=65532,gid=65532,mode=0700`）だけに許可する。Fargate既定20 GiB ephemeral storageは引き続き追加容量なしとする。平常imageはECS Execだけのためにshell utilityを追加しない。

runtime identityはDHI runtimeに定義済みの`nonroot` (`65532:65532`、home `/home/nonroot`)を使用する。Dockerfile、native container gate、CDK task definition、tmpfs mount optionはrepository rootの`container-policy.json`を共通契約とし、どれか一つだけのUID/GID変更をCIで拒否する。`/tmp/shittim-chest`はFargate起動時に同一UID/GIDとmode `0700`でmountし、non-root applicationがheartbeatを書込む。

production containerはDHI Communityの`dhi.io/python:3.14.6-debian13`、builderは対応する`-dev`を採用し、tagとOCI image index digestの両方を固定する。2026-07-22にindexがARM64 manifestを含むこと、runtimeの`User=65532`、`nonroot` passwd/group/home、runtime variant、shell/package manager非搭載をregistry manifestとfilesystemで実測した。productionはexec形式entrypoint、`SIGTERM`を使用し、uv、build cache、raw source、testを含めない。DHIから継承したlabelとruntime variantもnative gateで検査する。event loop ownerが5秒ごとにheartbeatを`/tmp/shittim-chest/heartbeat`へatomic更新する。health commandはstdlib-onlyの独立moduleとして20秒以内の更新だけを本文出力なしで検査し、Runtime packageやDiscord/OpenAI SDKをimportしない。taskごとに隔離され停止時に消えるtmpfsであるため、PID形式とprocess生存の重複検査は行わない。

DHI CommunityはApache-2.0の無償catalogを使い、Select/Enterpriseの購入とSLAは前提にしない。ただし`dhi.io`のpullにはDocker IDとread-only PAT/OATでの認証が必要である。CIとDependabotは同名の`DHI_USERNAME`/`DHI_TOKEN`をActions secretsとDependabot secretsにそれぞれ登録し、値をartifactやlogに出力しない。DHI CommunityにはHigh/Critical修正SLAがないため、daily Dependabot、署名済みVEX、期限付きの個別risk acceptanceで補う。

2026-01-06の公式発表でFargateがtmpfs mountをsupportしたため、production task definitionは`linuxParameters.tmpfs`で1 MiBの`/tmp/shittim-chest`を宣言し、以前のtask bind mountは廃止する。memory上に置かれtask停止で残存しないため、ephemeral storageを消費しない。CDKの`LinuxParameters`は`uid=`/`gid=`/`mode=`のparameter付きmount optionを表現できないため、L1 `CfnTaskDefinition`のproperty overrideで宣言する。2026-07-20時点で開発者ガイド`fargate-tasks-services.html`は`tmpfs`非対応と記載したままだが、What's New発表とAPI reference `LinuxParameters`/`Tmpfs`（Fargate非対応の注記なし）を優先し、deploy時にtask起動で実動作を確認する。local container試験では同等のtmpfsを使う。`initProcessEnabled=true`もtask definition側のSTEP-09で設定し、STEP-08A imageだけで設定済みとは扱わない。

### 5.1 Break-glass task definition

通常のlog/metric調査で不足し承認されたincidentだけ、stop-before-startでbreak-glass revisionへ切り替える。break-glass版はroot filesystemを書込み可能にし、ECS Exec、`/bin/sh`、`script`、`cat`、4つの`ssmmessages` action、専用CloudWatch Logs書込権限を有効にする。sessionはroot実行であることを前提に、`logging=OVERRIDE`、専用90日log group、開始理由・操作者・開始終了時刻を記録する。調査終了後は平常revisionへ戻し、Exec agentがないtaskへ置換されたことを確認する。

`break-glass` targetはproduction distroless runtimeにpackage managerを追加せず、同一digest固定のDHI `-dev`から独立構築する。`/bin/sh`、`cat`、`script`、`ps`と同一application venvを含めるが、実行userは引き続き`65532:65532`とする。production imageとは別digestで試験・署名・承認し、平常serviceへ混入させない。root filesystem書込み可否、Exec agent、IAM、log group、承認workflowはSTEP-09/10のbreak-glass task revisionで制御する。

## 6. ECR

- tag mutabilityは除外filterなしの`IMMUTABLE`とし、`IMMUTABLE_WITH_EXCLUSION`、mutable tag、`latest`を禁止する。`git-<full-sha>`、`candidate-<full-sha>-<run-id>`、`release-<version>`は追跡用labelにすぎず、deploy入力へ使用しない。
- task definitionのimage URIは常に`<account>.dkr.ecr.ap-northeast-1.amazonaws.com/shittim-chest@sha256:<64-hex>`とする。release manifest、change set、rollbackも同じdigestを正とし、tagからdeploy時に再解決しない。
- repository暗号化はECR既定のserver-side encryptionを使用し、CDK/CloudFormationで`EncryptionConfiguration`を指定しない。customer managed KMS keyは作成しない。
- registry scanning configurationで対象を`shittim-chest`だけに限定したAmazon Inspector拡張スキャン（`ENHANCED`、push時スキャンと継続的な再スキャンを含む`CONTINUOUS_SCAN`）を有効にし、repository側の基本scan-on-pushは無効とする。ECR Managed SigningはAWS Signer profile `shittim_chest_ecr`でpush時に自動署名する。push principalへ対象repositoryのupload権限と対象profileの`signer:SignPayload`だけを許可する。
- untagged imageと全tag付きimage（`release-*`、`git-<full-sha>`、`candidate-*`を含む）をそれぞれ直近3世代だけ保持し、それより古いimageは自動削除する。rollback対象は直近3世代内の検証済みdigestに限定される。現行・直前正常digestをdeploy manifestとともに保護し、lifecycle preview後に適用する。
- ARM64 imageを必須、x86_64はcompatibility fallbackとして同一sourceからbuildする。

### 6.1 OCI reference artifact

ECR OCI v1.1 Referrers APIを使い、release対象image manifest digestをsubjectとして次を同一repositoryへ保存する。artifact自身のdigestもrelease manifestへ記録する。

```text
sha256:<image-digest>
├── AWS Signer / Notation signature
├── SPDX 2.3 JSON SBOM
├── SLSA build provenance / GitHub artifact attestation
└── ECR scan由来 vulnerability assessment
```

- Managed Signingが生成するsignatureはimage digestへ自動で関連付けられる。
- Syftでpush済みdigestから生成・検証したSPDX 2.3 JSONを、`actions/attest`のSBOM predicateとOCI registry referrerとして保存する。
- build provenanceはGitHub-hosted runner、workflow path、immutable repository ID、commit SHA、image digestを含むSLSA predicateとし、`push-to-registry`でECR referrerへ保存する。
- vulnerability assessmentはECR enhanced scanのfindingをseverity別に正規化し、Amazon Inspector coverageの同一image digestに対する`ACTIVE` / `SUCCESSFUL`と`lastScannedAt`で初回scan完了を確認する。ECRがscan timestampを返す場合はそれを優先し、ゼロfindingで返さない場合だけcoverage timestampを使う。scan timestamp、scanner、image digest、finding countを含むJSONとしてOCI artifactへattachする。critical/highの未承認findingがある場合はattach後もreleaseを不合格とする。質問、secret、private runtime値を含めない。
- subject imageを削除するとECRがreference artifactを24時間以内にcleanupする。署名やSBOMだけをrollback証跡として扱わず、使用中・直前正常image digest自体を保持する。

### 6.2 自動検証

release planとEnvironment承認後のdeploy jobは同じdigestへ次を順番に実行し、1件でも失敗したらchange setを実行しない。

1. `describe-images`でtagではなくmanifest digestの存在とmedia typeを確認する。
2. `describe-image-signing-status --image-id imageDigest=...`をbounded pollingし、期待するSigner profileが`COMPLETE`であることを確認する。
3. AWS公式Notation installerをinstaller `2.2.0-1`、同梱Notation CLI `1.3.2`、AWS Signer plugin `1.0.2292`として個別にversion固定し、installer・signature・公開鍵のdigestとPGP fingerprintも検証して導入する。AWS Signer trust store、strict policy、期待profile ARNを使って`notation verify <repository>@sha256:<digest>`を実行し、署名の暗号学的検証・revocation確認とする。signing statusだけで代替しない。
4. `list-image-referrers --subject-id imageDigest=...`でsignature、SPDX SBOM、build provenance、vulnerability assessmentの4種が`ACTIVE`であることを確認し、artifact digestをmanifestと一致させる。
5. GitHub artifact attestationはrepository identity、workflow、commit、subject digestを検証し、SBOM hashとscan gateを再確認する。

Signer `FAILED`/profile不一致、ECR `FAILED`/`UNSUPPORTED_IMAGE`/`SCAN_ELIGIBILITY_EXPIRED`/`FINDINGS_UNAVAILABLE`/`LIMIT_EXCEEDED`/`IMAGE_ARCHIVED`、Inspector terminal/ambiguous coverage、AccessDenied/Validationは即時停止する。`IN_PROGRESS`/`PENDING`、Inspector `PENDING_INITIAL_SCAN`/`SCAN_IN_PROGRESS`/`INTERNAL_ERROR`/`PENDING_REVIVAL_SCAN`、`ScanNotFound`、明示的なthrottling/service transientだけを有限retryする。provider stderr、failure reason、private resource metadataはworkflow logへ転送しない。

STEP-09Bではtask definitionがdigest URI以外を拒否するassertionを追加する。ECS `PRE_SCALE_UP` Lambda lifecycle hookによるserver-side signing status/referrer admissionはOperations監視から分離し、STEP-10-Aでrelease supply-chain gateと同時に実装する。timeoutや不一致は`FAILED`としてrollbackする。暗号学的Notation verificationはrelease workflowを正とし、hookは防御層として用いる。CloudFormation `DeploymentLifecycleHook.HookDetails`はproperty typeが`Json`でもresource providerが「JSON objectを表す文字列」を要求するため、escape hatchでは`JSON.stringify`した文字列を出力し、synthesized template testでstring型と内容を固定する。

## 7. IAM

- Execution role: 対象ECR repositoryのpull、application CloudWatch Logs、task definitionが参照する各Parameterの`ssm:GetParameters`だけ。ECRの`GetAuthorizationToken`以外はresourceを限定し、AWS-managed encryptionのためKMS decryptは付与しない。
- 平常Task role: 実装が使用する対象DynamoDB table/indexの`ConditionCheckItem`、`GetItem`、`PutItem`、`UpdateItem`、`Query`だけ。EMFはstdoutの`awslogs`経由であり`cloudwatch:PutMetricData`は付与しない。secret読取、`ssmmessages`、Exec log group書込権限を持たない。
- Break-glass Task role: 平常権限に加え4つの`ssmmessages` actionと専用Exec log group書込だけを一時的に許可する。
- DiscordIngress Lambda role: 既存tableのIngress Request、idempotency、queue counter、公開Status publicationのdurable受付に必要な読取とtransactionだけを許可する。SSM read、`lambda:InvokeFunction`、Runtime State参照、ECS更新、Discord Bot token、参加者Bot token、OpenAI API keyへの権限は持たない。
- DiscordStatusPublisher Lambda role: status更新recordの読取・条件付き完了とmoderator Bot tokenの取得だけを許可する。参加者Bot token、OpenAI API key、ECS更新権限は持たない。
- RuntimeReconciler Lambda role: runtime control recordの読取・条件付き更新と、対象ECS Serviceの`DescribeServices`、`UpdateService`だけを許可する。secret取得権限は持たない。
- Image Admission Lambda role: `DescribeServiceRevisions`は固定service/revision ARNへ限定する。`ecs:DescribeTaskDefinition`はAWS公式Service Authorization Referenceでresource-level permissionをsupportしないため独立statementの`Resource: "*"`とし、取得対象はrevisionが返したexact task definition ARN、応答は同ARN・`application` container・固定repository・release image digestへapplication側で限定する。
- GitHub plan roleはimmutable main subject、deploy roleはimmutable `production` Environment subjectに限定する。planはchange set作成、ECR push、対象Signer profileの`signer:SignPayload`、署名状態・scan・referrer取得を許可し、deployはEnvironment承認済みchange set実行と検証用readだけ、drift roleはread-onlyとする。
- `iam:PassRole`は対象execution/task role ARNと`iam:PassedToService=ecs-tasks.amazonaws.com`へ限定する。
- ECS task trustは`ecs-tasks.amazonaws.com`。`aws:SourceAccount`を実accountへ一致させ、`aws:SourceArn=arn:<partition>:ecs:ap-northeast-1:<account>:*`の`ArnLike`を付ける。ECS公式の制約によりclusterまでは限定できない。

## 8. Parameter Store

SecureString値はCloudFormation/CDKで作成せず、operatorが事前登録しCDKはversion付きの名前だけを参照する。GitHub Actions、CloudFormation output、deploy manifestはparameter値を取得しない。

```text
/shittim-chest/production/openai/api-key
/shittim-chest/production/discord/moderator/public-key
/shittim-chest/production/discord/moderator/token
/shittim-chest/production/discord/participant-a/token
/shittim-chest/production/discord/participant-b/token
/shittim-chest/production/discord/participant-c/token
/shittim-chest/production/runtime/v0002
/shittim-chest/production/personas/v0002/moderator
/shittim-chest/production/personas/v0002/participant-a
/shittim-chest/production/personas/v0002/participant-b
/shittim-chest/production/personas/v0002/participant-c
```

operatorはAWS Consoleで11件を個別作成せず、repository rootから次の1 commandを実行する。

```sh
uv run --frozen python tools/configure_production_inputs.py
```

toolはGitHubのrelease role ARNとactive AWS identityのaccountを値を表示せず照合し、不足値だけを順に非表示入力する。local-onlyの`SHITTIM_PRIVATE_CONFIG_SOURCE` pointerが設定済みなら、保存済み`PersonaConfig v0002`の4 slot、display name、prompt、SSM pathをlocalでfail closedに検証して再利用し、persona本文を再入力させない。pointerとsourceはGit管理外とし、source pathや値を出力・公開mirrorへ複製しない。全値を別fileへ保存せず検証してから、確認後にGitHub Actionsの`OPERATOR_NOTIFICATION_EMAIL`とSSM Standard `SecureString`を作成する。既存parameter valueは取得・復号・上書きせず、`--check`はGitHub secret名とSSM metadataの設定数だけを返す。GitHub secretは標準入力、SSM値はboto3 API request bodyで渡し、process argumentへ秘密値を含めない。

`RuntimeConfig`は`schema_version`、`config_version`、Guild ID、非空channel allowlist、4 Application IDを保持する。`PersonaConfig`は同version、slot、display name、system promptを保持し、1 parameterをUTF-8 3,500 bytes以下に制限する。既存pathを上書きせず新version pathを作り、task definition更新後にstop-before-start deployを行う。token/API keyをCDK context、GitHub secret、CloudFormation output、Obsidianへ保存しない。

## 9. Cost・backup

- Public IPv4とFargate vCPU/memoryは`desiredCount=1`でtaskが実際に稼働する時間だけ課金対象とし、平常の`desiredCount=0`では発生しない。Public IPv4単価は0.005 USD/時を基準にdeploy時に再計算する。
- scale-to-zero待受ではAPI Gateway HTTP APIのrequest数、3 Lambdaのinvocation/compute、1分周期EventBridge rule、DynamoDB on-demand request/storage、CloudWatch Logs/metricsが少量の常時cost候補となる。NAT Gateway、ALB、常駐Gateway processは追加しない。
- ECR連携でのAWS Signer利用自体に追加Signer料金はない。ただしsignature、SBOM、provenance、vulnerability assessmentは各reference artifactとしてECR image quotaと保存容量を消費するため、repository容量とartifact数を月次確認する。
- Fargate既定20 GiB ephemeral storageは追加料金なしとし、追加容量は設定しない。Container Insightsは無効とする。単一taskのMVPではECS標準CPU・メモリ、EventBridge通知、少数のapplication metricを使い、task/container単位のContainer Insights固定費を負担しない。
- `Project` user-defined cost allocation tagをBillingで有効化し、反映後にProject tag budget 20 USD、account全体budget 30 USD、OpenAI project budget 50 USDを設定する。2026-07-30にBillingへの出現をmetadataで確認し、`Inactive`から`Active`へ更新して再取得で確認済み。Cost Anomaly Detectionは月額予算ではなく異常の総影響額を評価するため、notification thresholdは10 USDとする。AWS側2 BudgetとCADは`us-east-1`の独立stackで管理する。
- Budgetはactual 80%/100%とforecasted 100%を通知し、自動停止actionは設けない。Runtime alarm、AWS Budget、Cost Anomaly Detectionは同一のoperator emailをdeploy時parameterで受け取り、実addressをGit、Obsidian、CloudFormation outputへ保存しない。Cost Anomaly Detectionは既存のAWS managed service monitorをARN parameterで再利用し、quota 1の同種monitorを重複作成しない。新しいCDK管理通知の作成・到達確認後に、既存の手動10 USD Budget/CAD subscriptionをoperatorが撤去する。`Project` tagがBillingで`Active`になるまではtag budgetをdeployしない。
- DynamoDB PITRは35日、stack削除でもtableをretainする。業務dataにTTLを設定しないが35日より古い状態の復旧は保証せず、AWS Backupは作成しない。
- DynamoDB on-demand maximum throughputは負荷試験前に推測値を設定しない。初回本番計測後に必要性と値をADRで決定し、設定する場合はthrottle alarmと同時に導入する。

## 10. 公式資料確認記録

| 確認日 | 対象version/service | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-07-16 | Fargate network | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-networking.html | awsvpc、Public IP、IPv6条件 |
| 2026-07-28 | Fargate capacity provider | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-capacity-providers.html | 旧Spot専用設計を廃止し、On-Demand `FARGATE`、平常desired 0、maximum 1へ変更 |
| 2026-07-16 | ECS Exec | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-exec.html | root、writable root、logging |
| 2026-07-19 | Fargate pricing | https://aws.amazon.com/fargate/pricing/ | 既定20 GiB ephemeral storageは追加料金なし、追加容量は設定しない |
| 2026-07-19 | CloudWatch pricing | https://aws.amazon.com/cloudwatch/pricing/ | 単一task MVPではContainer Insightsを無効にし、少数application metricとECS標準metricへ絞る |
| 2026-07-16 | Cost Anomaly Detection | https://docs.aws.amazon.com/cost-management/latest/userguide/getting-started-ad.html | managed anomaly monitor |
| 2026-07-29 | ECS task state change events | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs_task_events.html | STOPPED event、stopCode/reason、EventBridge delivery |
| 2026-07-29 | CloudWatch composite alarms | https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarm-combining.html | underlying alarmをcritical/warningへ集約し通知noiseを抑制 |
| 2026-07-29 | SNS email subscription | https://docs.aws.amazon.com/sns/latest/dg/sns-email-notifications.html | deploy後のemail確認とPendingConfirmation運用 |
| 2026-07-29 | Billing and Cost Management endpoints | https://docs.aws.amazon.com/general/latest/gr/billing.html | Budgets/Cost Explorerに東京endpointがないためcost governanceだけ`us-east-1`へ分離 |
| 2026-07-29 | CreateBudget API | https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_budgets_CreateBudget.html | `FilterExpression`/`Metrics`、`user:Project` tag filter、email通知 |
| 2026-07-29 | Cost Anomaly Detection quotas | https://docs.aws.amazon.com/cost-management/latest/userguide/management-limits.html | AWS managed service monitorはaccountあたり1件のため既存ARNを再利用 |
| 2026-07-29 | AnomalySubscription | https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ce-anomalysubscription.html | DAILY email、absolute impact 10 USDのThresholdExpression |
| 2026-07-16 | Task definition | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html | CPU/memory、stop timeout、awslogs |
| 2026-08-02 | ECS Service Authorization Reference | https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonelasticcontainerservice.html | `DescribeTaskDefinition`はresource typeを持たず、identity policyでは`Resource: "*"`が必要。対象task definitionの限定はapplication側のexact ARN検証で維持 |
| 2026-07-16 | CDK | https://docs.aws.amazon.com/cdk/v2/guide/home.html | stack、synth/diff、logical ID |
| 2026-07-16 | VPC pricing | https://aws.amazon.com/vpc/pricing/ | Public IPv4費用 |
| 2026-07-28 | ECS service desired count | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/update-service-parameters.html | Reconcilerが条件付き状態に従いdesired 0/1を収束、同時taskは最大1 |
| 2026-07-17 | ECS ContainerDefinition | https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_ContainerDefinition.html | Fargate `stopTimeout=120`を明示し、application内部deadlineを90秒へ設定 |
| 2026-07-17 | ECS task definition parameters | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html | ARM64、512 CPU/1024 MiB、health、read-only root、`stopTimeout=120`のtask境界を再確認 |
| 2026-07-17 | Fargate task differences | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html | Fargateで`tmpfs`非対応のためtask bind volumeへ分離（2026-07-20に次行で訂正） |
| 2026-07-20 | ECS tmpfs on Fargate | https://aws.amazon.com/jp/about-aws/whats-new/2026/01/amazon-ecs-tmpfs-mounts-aws-fargate-managed-instances/、https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_LinuxParameters.html | 2026-01-06発表でFargate tmpfs support開始。bind mountを1 MiB tmpfsへ置換。開発者ガイド`fargate-tasks-services.html`の`tmpfs`非対応記載は同日時点で未更新 |
| 2026-07-22 | DHI Community Python 3.14.6 | https://docs.docker.com/dhi/how-to/use/、https://docs.docker.com/dhi/features/ | Apache-2.0 Community、`dhi.io`認証、`-dev`とdistroless runtime、SLA非保証を採用条件に反映 |
| 2026-07-22 | DHI Python registry manifest | https://dhi.io/catalog/python | 3.14.6 Debian 13 index digest、ARM64 manifest、runtime `User=65532`、`nonroot` home、shell/package manager非搭載を実測 |
| 2026-07-17 | Docker build best practices | https://docs.docker.com/build/building/best-practices/ | multi-stage、最小runtime、digest固定、`.dockerignore` |
| 2026-07-17 | uv Docker integration 0.11.29 | https://docs.astral.sh/uv/guides/integration/docker/ | uv image digest固定、`uv sync --frozen --no-dev --no-editable`、cache非同梱 |
| 2026-07-19 | AWS CDK prerequisites / Node support、Node.js releases | https://docs.aws.amazon.com/cdk/v2/guide/prerequisites.html、https://docs.aws.amazon.com/cdk/v2/guide/node-versions.html、https://nodejs.org/en/about/previous-releases | Node.js 24.18.0 Active LTS、TypeScript strict、local CLI固定。Node 26 Currentは採用しない |
| 2026-07-19 | DynamoDB Table CDK API | https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_dynamodb.Table.html | on-demand、PITR 35日、deletion protection、RETAIN |
| 2026-07-19 | ECR CDK API | https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_ecr-readme.html | immutable、scan-on-push、限定lifecycle、RETAIN |
| 2026-07-20 | ECR RegistryScanningConfiguration / lifecycle count rule | https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ecr-registryscanningconfiguration.html、https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-ecr-registryscanningconfiguration-scanningrule.html | 基本scan-on-push無効化、`ENHANCED`/`CONTINUOUS_SCAN`拡張スキャンを`shittim-chest`へ限定適用、`imageCountMoreThan`でuntagged/taggedとも直近5世代保持へ変更 |
| 2026-08-03 | ECR lifecycle policy parameters | https://docs.aws.amazon.com/AmazonECR/latest/userguide/lifecycle_policy_parameters.html | `imageCountMoreThan`の`countNumber`をuntagged/taggedとも3へ変更し、各分類の最新3 imageを保持 |
| 2026-07-19 | cdk-nag 3.0.1 | https://github.com/cdklabs/cdk-nag#usage | CDK `Validations` pluginとunsuppressed finding 0を採用 |
| 2026-07-19 | ECR Managed Signing | https://docs.aws.amazon.com/AmazonECR/latest/userguide/managed-signing.html | Signer profile、repository限定registry rule、push時自動署名、status polling |
| 2026-07-19 | ECR signature verification | https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-signing-verification.html、https://docs.aws.amazon.com/signer/latest/developerguide/image-verification.html | Notation strict verificationを自動deploy gate、ECS hookを防御層に採用 |
| 2026-07-19 | ECR OCI v1.1 / Referrers API | https://docs.aws.amazon.com/AmazonECR/latest/userguide/images.html、https://docs.aws.amazon.com/AmazonECR/latest/APIReference/API_ListImageReferrers.html | signature、SBOM、provenance、scan assessmentをimage digestへ関連付ける |
| 2026-07-31 | ECS image URI / lifecycle hook / CloudFormation property | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/create-task-definition.html、https://docs.aws.amazon.com/AmazonECS/latest/developerguide/lambda-lifecycle-hooks.html、https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-ecs-service-deploymentlifecyclehook.html | digest URI、`PRE_SCALE_UP` fail-closed admission、`HookDetails`はserialized JSON object string |
| 2026-07-19 | AWS Signer pricing / ECR artifact quota | https://docs.aws.amazon.com/signer/latest/developerguide/Welcome.html、https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-signing.html | ECR連携Signer追加料金なし、reference artifactのquota/storage影響を運用へ反映 |
| 2026-07-28 | CDK VPC / FargateService / HTTP API / Lambda | https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_ec2.Vpc.html、https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_ecs.FargateService.html、https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_apigatewayv2.HttpApi.html、https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_lambda.Function.html | NAT 0、Public IP、On-Demand FARGATE、desired 0、3 Lambda VPC外、HTTP API最小routeをassert |
| 2026-07-19 | ECS task IAM role | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html | `SourceAccount`とregion/account限定`SourceArn`でconfused deputyを防止 |
| 2026-07-19 | ECS Parameter Store injection | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-ssm-paramstore.html | execution roleの各parameter限定`ssm:GetParameters`、更新時のnew deploymentを採用 |
| 2026-07-30 | SSM DescribeParameters / PutParameter | https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_DescribeParameters.html、https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_PutParameter.html | metadata-only不足確認、Standard SecureString、既存値非取得・非上書きの対話setupを採用 |
| 2026-07-30 | GitHub CLI secret set | https://cli.github.com/manual/gh_secret_set | private operator emailをprocess argumentでなく標準入力からrepository Actions secretへ登録 |
| 2026-07-30 | Cost allocation tags | https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_ListCostAllocationTags.html、https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_UpdateCostAllocationTagsStatus.html | `Project` user-defined tagのBilling出現を確認後に`Active`へ更新し、CostGovernance deploy gateを解除 |
| 2026-07-30 | AWS Signer Notation prerequisites / installer CHANGELOG | https://docs.aws.amazon.com/signer/latest/developerguide/image-signing-prerequisites.html、https://d2hvyiie56hcat.cloudfront.net/CHANGELOG | installer `2.2.0-1`と同梱CLI `1.3.2`・plugin `1.0.2292`は別versionとして固定・検証する |
| 2026-07-19 | CloudWatch Logs data protection | https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/mask-sensitive-log-data.html | application/Exec log groupの非機密ログ原則に追加防御としてmask policyを適用 |
| 2026-07-31 | CDK assets / CLI credential synthesizer | https://docs.aws.amazon.com/cdk/v2/guide/assets.html、https://docs.aws.amazon.com/cdk/v2/guide/ref-cli-cmd-publish-assets.html、https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.CliCredentialsStackSynthesizer.html | 全file assetを短命plan credentialでCloudFormation前に直接publish |
| 2026-07-31 | S3 multipart upload/checksum boundary | https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html | composite checksumへ移行しないsize上限を事前検証し、plan roleをsingle-part Putへ限定 |
| 2026-07-31 | CloudFormation DeletionPolicy | https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-attribute-deletionpolicy.html | `RetainExceptOnCreate`で初回rollback孤児化を防止 |
| 2026-07-31 | ECR/Inspector scan status | https://docs.aws.amazon.com/AmazonECR/latest/APIReference/API_ImageSigningStatus.html、https://docs.aws.amazon.com/AmazonECR/latest/APIReference/API_ImageScanStatus.html、https://docs.aws.amazon.com/inspector/v2/APIReference/API_ScanStatus.html | pendingとterminalを列挙して認可・終端失敗を即時停止 |
