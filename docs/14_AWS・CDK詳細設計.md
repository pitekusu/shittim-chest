---
aliases:
  - The Shittim Chest AWS詳細設計
tags: [project, shittim-chest, aws, cdk, ecs, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-07-19
---

# AWS・CDK詳細設計

## 1. Environment

- 本番は単一AWS accountの`ap-northeast-1`だけとし、別のAWS開発環境を作らない。
- localとCIはfake、DynamoDB Local、SDK contract testを使用し、AWS credentialをCIへ渡さない。外部接続確認は本番deploy後の限定smoke testで行う。
- 全resourceへ`Project=shittim-chest`、`Environment=production`、`ManagedBy=cdk`を付ける。

## 2. CDK app・stack

| Stack | Resource | Policy |
|---|---|---|
| `ShittimChest-Prod-Stateful` | DynamoDB、ECR、AWS Signer profile、ECR Managed Signing configuration | termination protection、DynamoDB/ECR/Signer profile `RETAIN` |
| `ShittimChest-Prod-Runtime` | VPC、SG、ECS cluster/service/task、IAM、app/break-glass Exec log group | Statefulへ一方向依存 |
| `ShittimChest-Prod-Operations` | dashboard、alarm、SNS、EventBridge、Budget、Cost Anomaly Detection | Runtime/Statefulを監視。Container Insightsは作成・有効化しない |

L2 constructを優先し、L1/escape hatchはADRで理由を残す。construct IDとlogical IDは初回deploy後に変更しない。`cdk.context.json`をcommitし、`cdk-nag`の`AwsSolutionsChecks`と`cdk synth --strict`を必須とする。

STEP-09Aでは最新Active LTSのNode.js 24.18.0、TypeScript 7.0.2、CDK CLI 2.1132.0、`aws-cdk-lib` 2.261.0、constructs 10.7.0、cdk-nag 3.0.1、Vitest 4.1.10を完全固定し、`package-lock.json`を正本とする。Node.js 26はCurrentでありLTS化前のため採用しない。CDK推奨feature flagを`cdk.json`へ全て明示し、将来のdefault変更でtemplateが暗黙変化しないようにする。cdk-nag 3は旧Aspect APIではなくCDK `Validations` pluginとして登録する。

STEP-09Aの`StatefulStack`は次を実装する。

- stack名は`ShittimChest-Prod-Stateful`、termination protectionを有効にする。
- DynamoDB table名は`shittim-chest-production`。`PK`/`SK`、ALL projectionの`gsi1`/`gsi2`、on-demand、AWS-managed encryption、deletion protection、PITR 35日、`RETAIN`とする。TTLとmaximum throughputは設定しない。
- ECR repository名は`shittim-chest`。暗号化propertyは指定せずECR既定暗号化を使用し、scan-on-push、除外filterなしの完全`IMMUTABLE`、`RETAIN`とする。untaggedと一意な`candidate-*`だけを14日でexpireし、`release-*`と`git-<full-sha>`には期限規則を設定しない。
- AWS Signer profile `shittim_chest_ecr`を`Notation-OCI-SHA384-ECDSA`、署名有効期間135か月、`RETAIN`で作成する。registry全体で一意な`AWS::ECR::SigningConfiguration`はrepository filter `shittim-chest`だけへ同profileを適用し、push時のECR Managed Signingを有効にする。別repositoryのruleを追加するときは同resourceへ集約する。
- `Project`、`Environment`、`ManagedBy` tagをapp rootから付与する。
- local/PRではdummy accountを使うassertionとcredentialなしのstrict synthだけを行い、bootstrap、deploy、AWS resource作成は行わない。

STEP-09Bの`RuntimeStack`はVPC、SG、ECS cluster/service、平常・break-glass task definition、IAM role、SSM SecureString参照、CloudWatch Logsを実装する。image digestとruntime config versionはCloudFormation parameterとし、形式を`^sha256:[0-9a-f]{64}$`と`^v[0-9]{4}$`でfail closedに検証する。image digestにdefaultを設けず、releaseは平常とbreak-glassの検証済みdigestを必ず明示する。local/PRでは構成assertion、cdk-nag、credentialなしのstrict synthまでとし、AWS resourceはdeployしない。

## 3. Network

- IPv4 CIDRは`10.42.0.0/24`、2 AZに各1つの`/26` Public Subnetだけ、NAT Gatewayなし、Internet Gatewayあり。
- Fargateは`awsvpc`、`AssignPublicIp=ENABLED`、routeは`0.0.0.0/0 -> IGW`。
- Security Groupはingress ruleなし。egressはTCP 443を許可する。
- ALB、NAT instance、DNS64、NAT64、Service Connectは作成しない。
- VPC Flow Logsはno-ingress・TCP 443 outboundのみの単一task MVPでは費用対効果が低いため作成しない。セキュリティincidentの調査でnetwork visibility不足が実証された場合はADRで再評価する。
- Discord/OpenAIがAAAAを公式supportし、24時間canaryを満たし、IPv6-only移行時にbreak-glass ECS Execを廃止する判断が完了するまでIPv6-onlyへ移行しない。

## 4. ECS・Fargate

| 項目 | 値 |
|---|---|
| Capacity Provider | `FARGATE_SPOT`のみ |
| desired count | 1 |
| CPU / Memory | 512 CPU units / 1,024 MiB |
| Architecture | ARM64既定、互換性または価格不利時だけx86_64 |
| Platform | Linux Fargate 1.4.0以上 |
| Deployment | minimum healthy 0%、maximum 100%、stop-before-start |
| AZ rebalancing | 無効。maximum 100%と両立させ、二重Bot接続を防ぐ |
| stop timeout | 120秒 |
| Circuit breaker | enable + rollback |
| Container Insights | 無効。account defaultを変更せず、このclusterでも有効化しない |

Spot singleton停止とcapacity不足中の全面停止を仕様として許容する。EventBridgeで`Your Spot Task was interrupted.`を記録・通知する。

application側のgraceful shutdown deadlineは90秒とし、`stopTimeout=120`の残り30秒をDiscord client close、log driver、container runtimeの終了余裕にする。AWSはSpot interruption時にSIGTERMを送り、configured `stopTimeout`後にSIGKILLするため、container実装時は値の省略を禁止する。

## 5. Container definition

- 平常taskはinit process有効、application userはnon-root、read-only root filesystem、privileged無効、Linux capability全削除、ECS Exec無効とする。
- app health checkはprocess/event-loop heartbeatだけを確認し、Discord/OpenAI障害をrestart理由にしない。
- `awslogs` modeは`blocking`を明示し、secret・質問・回答全文をstdoutへ出さない。
- applicationとbreak-glass Execは専用log groupに分け、各90日保持、`RETAIN`、AWS-managed encryption、CloudWatch Logs data protectionによるcredential・個人識別情報のmaskを適用する。
- 一時書込みはheartbeat用の`/tmp/shittim-chest` bind mountだけに許可する。Fargate既定20 GiB ephemeral storage内に収め、追加`ephemeralStorage`は設定しない。平常imageはECS Execだけのためにshell utilityを追加しない。

imageは`/tmp/shittim-chest`をUID/GID `10001:10001`、mode `0700`で事前作成し、Fargate bind mount後もnon-root applicationがheartbeatを書込める構成にする。

STEP-08AではPython `3.14.6-slim-trixie`とuv `0.11.29`のmulti-architecture image index digestを固定したmulti-stage `Dockerfile`を実装する。production imageはnumeric UID/GID `10001`、exec形式entrypoint、`SIGTERM` stop signalを使用し、uv、build cache、raw source、testを含めない。event loop ownerが5秒ごとにPID付きheartbeatを`/tmp/shittim-chest/heartbeat`へatomic更新し、health commandは20秒以内の更新、PID形式、process生存だけを本文出力なしで検査する。

Fargate task definitionは`tmpfs`をsupportしないため、productionでは既定20 GiB ephemeral storage内のtask bind mountを`/tmp/shittim-chest`へ定義する。mountごとの容量上限や追加課金はなく、容量を増やす`ephemeralStorage`は設定しない。local container試験では同等のtmpfsを使う。`initProcessEnabled=true`もtask definition側のSTEP-09で設定し、STEP-08A imageだけで設定済みとは扱わない。

### 5.1 Break-glass task definition

通常のlog/metric調査で不足し承認されたincidentだけ、stop-before-startでbreak-glass revisionへ切り替える。break-glass版はroot filesystemを書込み可能にし、ECS Exec、`/bin/sh`、`script`、`cat`、4つの`ssmmessages` action、専用CloudWatch Logs書込権限を有効にする。sessionはroot実行であることを前提に、`logging=OVERRIDE`、専用90日log group、開始理由・操作者・開始終了時刻を記録する。調査終了後は平常revisionへ戻し、Exec agentがないtaskへ置換されたことを確認する。

STEP-08Aの`break-glass` image targetはproduction runtimeへ`/bin/sh`、`cat`、`script`、`ps`だけを追加し、application実行時は引き続きUID/GID `10001`とする。root filesystem書込み可否、Exec agent、IAM、log group、承認workflowはimageではなくSTEP-09/10のbreak-glass task revisionで制御する。

## 6. ECR

- tag mutabilityは除外filterなしの`IMMUTABLE`とし、`IMMUTABLE_WITH_EXCLUSION`、mutable tag、`latest`を禁止する。`git-<full-sha>`、`candidate-<full-sha>-<run-id>`、`release-<version>`は追跡用labelにすぎず、deploy入力へ使用しない。
- task definitionのimage URIは常に`<account>.dkr.ecr.ap-northeast-1.amazonaws.com/shittim-chest@sha256:<64-hex>`とする。release manifest、change set、rollbackも同じdigestを正とし、tagからdeploy時に再解決しない。
- repository暗号化はECR既定のserver-side encryptionを使用し、CDK/CloudFormationで`EncryptionConfiguration`を指定しない。customer managed KMS keyは作成しない。
- scan-on-pushを有効にし、ECR Managed SigningはAWS Signer profile `shittim_chest_ecr`でpush時に自動署名する。push principalへ対象repositoryのupload権限と対象profileの`signer:SignPayload`だけを許可する。
- untagged/candidate imageだけを14日でexpireし、`release-*`と`git-<full-sha>`は自動削除しない。現行・直前正常digestをdeploy manifestとともに保護し、lifecycle preview後に適用する。
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
- vulnerability assessmentはECR scan完了後のfindingをseverity別に正規化し、scan timestamp、scanner、image digest、finding countを含むJSONとしてOCI artifactへattachする。critical/highの未承認findingがある場合はattach後もreleaseを不合格とする。質問、secret、private runtime値を含めない。
- subject imageを削除するとECRがreference artifactを24時間以内にcleanupする。署名やSBOMだけをrollback証跡として扱わず、使用中・直前正常image digest自体を保持する。

### 6.2 自動検証

release planとEnvironment承認後のdeploy jobは同じdigestへ次を順番に実行し、1件でも失敗したらchange setを実行しない。

1. `describe-images`でtagではなくmanifest digestの存在とmedia typeを確認する。
2. `describe-image-signing-status --image-id imageDigest=...`をbounded pollingし、期待するSigner profileが`COMPLETE`であることを確認する。
3. AWS公式Notation installerをversion/digest固定で導入し、AWS Signer trust store、strict policy、期待profile ARNを使って`notation verify <repository>@sha256:<digest>`を実行する。これを署名の暗号学的検証・revocation確認とし、signing statusだけで代替しない。
4. `list-image-referrers --subject-id imageDigest=...`でsignature、SPDX SBOM、build provenance、vulnerability assessmentの4種が`ACTIVE`であることを確認し、artifact digestをmanifestと一致させる。
5. GitHub artifact attestationはrepository identity、workflow、commit、subject digestを検証し、SBOM hashとscan gateを再確認する。

STEP-09Bではtask definitionがdigest URI以外を拒否するassertionを追加する。STEP-09CではECS `PRE_SCALE_UP` Lambda lifecycle hookによるserver-side signing status/referrer admissionを追加し、timeoutや不一致を`FAILED`としてrollbackする。暗号学的Notation verificationはrelease workflowを正とし、hookは防御層として用いる。

## 7. IAM

- Execution role: 対象ECR repositoryのpull、application CloudWatch Logs、task definitionが参照する各Parameterの`ssm:GetParameters`だけ。ECRの`GetAuthorizationToken`以外はresourceを限定し、AWS-managed encryptionのためKMS decryptは付与しない。
- 平常Task role: 実装が使用する対象DynamoDB table/indexの`ConditionCheckItem`、`GetItem`、`PutItem`、`UpdateItem`、`Query`だけ。EMFはstdoutの`awslogs`経由であり`cloudwatch:PutMetricData`は付与しない。secret読取、`ssmmessages`、Exec log group書込権限を持たない。
- Break-glass Task role: 平常権限に加え4つの`ssmmessages` actionと専用Exec log group書込だけを一時的に許可する。
- GitHub plan roleはimmutable main subject、deploy roleはimmutable `production` Environment subjectに限定する。planはchange set作成、ECR push、対象Signer profileの`signer:SignPayload`、署名状態・scan・referrer取得を許可し、deployはEnvironment承認済みchange set実行と検証用readだけ、drift roleはread-onlyとする。
- `iam:PassRole`は対象execution/task role ARNと`iam:PassedToService=ecs-tasks.amazonaws.com`へ限定する。
- ECS task trustは`ecs-tasks.amazonaws.com`。`aws:SourceAccount`を実accountへ一致させ、`aws:SourceArn=arn:<partition>:ecs:ap-northeast-1:<account>:*`の`ArnLike`を付ける。ECS公式の制約によりclusterまでは限定できない。

## 8. Parameter Store

SecureString値はCloudFormation/CDKで作成せず、operatorが事前登録しCDKはversion付きの名前だけを参照する。GitHub Actions、CloudFormation output、deploy manifestはparameter値を取得しない。

```text
/shittim-chest/production/openai/api-key
/shittim-chest/production/discord/moderator/token
/shittim-chest/production/discord/participant-a/token
/shittim-chest/production/discord/participant-b/token
/shittim-chest/production/discord/participant-c/token
/shittim-chest/production/runtime/v0001
/shittim-chest/production/personas/v0001/moderator
/shittim-chest/production/personas/v0001/participant-a
/shittim-chest/production/personas/v0001/participant-b
/shittim-chest/production/personas/v0001/participant-c
```

`RuntimeConfig`は`schema_version`、`config_version`、Guild ID、非空channel allowlist、4 Application IDを保持する。`PersonaConfig`は同version、slot、display name、system promptを保持し、1 parameterをUTF-8 3,500 bytes以下に制限する。既存pathを上書きせず新version pathを作り、task definition更新後にstop-before-start deployを行う。token/API keyをCDK context、GitHub secret、CloudFormation output、Obsidianへ保存しない。

## 9. Cost・backup

- Public IPv4は0.005 USD/時を基準に約3.65 USD/月を見込み、deploy時に再計算する。
- ECR連携でのAWS Signer利用自体に追加Signer料金はない。ただしsignature、SBOM、provenance、vulnerability assessmentは各reference artifactとしてECR image quotaと保存容量を消費するため、repository容量とartifact数を月次確認する。
- Fargate既定20 GiB ephemeral storageは追加料金なしとし、追加容量は設定しない。Container Insightsは無効とする。単一taskのMVPではECS標準CPU・メモリ、EventBridge通知、少数のapplication metricを使い、task/container単位のContainer Insights固定費を負担しない。
- `Project` user-defined cost allocation tagをBillingで有効化し、反映後にProject tag budget 50 USD、account全体budget 30 USD、OpenAI project budget 50 USDを設定する。Cost Anomaly Detectionの通知thresholdは30 USDとする。
- Budgetはactual 80%/100%とforecasted 100%を通知し、自動停止actionは設けない。Cost Anomaly Detectionのservice monitorを有効化する。
- DynamoDB PITRは35日、stack削除でもtableをretainする。業務dataにTTLを設定しないが35日より古い状態の復旧は保証せず、AWS Backupは作成しない。
- DynamoDB on-demand maximum throughputは負荷試験前に推測値を設定しない。初回本番計測後に必要性と値をADRで決定し、設定する場合はthrottle alarmと同時に導入する。

## 10. 公式資料確認記録

| 確認日 | 対象version/service | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-07-16 | Fargate network | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-networking.html | awsvpc、Public IP、IPv6条件 |
| 2026-07-16 | Fargate Spot | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-capacity-providers.html | Spot専用、停止設計 |
| 2026-07-16 | ECS Exec | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-exec.html | root、writable root、logging |
| 2026-07-19 | Fargate pricing | https://aws.amazon.com/fargate/pricing/ | 既定20 GiB ephemeral storageは追加料金なし、追加容量は設定しない |
| 2026-07-19 | CloudWatch pricing | https://aws.amazon.com/cloudwatch/pricing/ | 単一task MVPではContainer Insightsを無効にし、少数application metricとECS標準metricへ絞る |
| 2026-07-16 | Cost Anomaly Detection | https://docs.aws.amazon.com/cost-management/latest/userguide/getting-started-ad.html | managed anomaly monitor |
| 2026-07-16 | Task definition | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html | CPU/memory、stop timeout、awslogs |
| 2026-07-16 | CDK | https://docs.aws.amazon.com/cdk/v2/guide/home.html | stack、synth/diff、logical ID |
| 2026-07-16 | VPC pricing | https://aws.amazon.com/vpc/pricing/ | Public IPv4費用 |
| 2026-07-17 | Fargate Spot termination | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-capacity-providers.html | SIGTERM後にconfigured stopTimeoutでSIGKILL、singletonはcapacity復帰まで停止する前提を再確認 |
| 2026-07-17 | ECS ContainerDefinition | https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_ContainerDefinition.html | Fargate `stopTimeout=120`を明示し、application内部deadlineを90秒へ設定 |
| 2026-07-17 | ECS task definition parameters | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html | ARM64、512 CPU/1024 MiB、health、read-only root、`stopTimeout=120`のtask境界を再確認 |
| 2026-07-17 | Fargate task differences | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html | Fargateで`tmpfs`非対応のためtask bind volumeへ分離 |
| 2026-07-17 | Python official image 3.14.6 | https://hub.docker.com/_/python | slim-trixieのamd64/arm64 multi-arch index digestを固定 |
| 2026-07-17 | Docker build best practices | https://docs.docker.com/build/building/best-practices/ | multi-stage、最小runtime、digest固定、`.dockerignore` |
| 2026-07-17 | uv Docker integration 0.11.29 | https://docs.astral.sh/uv/guides/integration/docker/ | uv image digest固定、`uv sync --frozen --no-dev --no-editable`、cache非同梱 |
| 2026-07-19 | AWS CDK prerequisites / Node support、Node.js releases | https://docs.aws.amazon.com/cdk/v2/guide/prerequisites.html、https://docs.aws.amazon.com/cdk/v2/guide/node-versions.html、https://nodejs.org/en/about/previous-releases | Node.js 24.18.0 Active LTS、TypeScript strict、local CLI固定。Node 26 Currentは採用しない |
| 2026-07-19 | DynamoDB Table CDK API | https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_dynamodb.Table.html | on-demand、PITR 35日、deletion protection、RETAIN |
| 2026-07-19 | ECR CDK API | https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_ecr-readme.html | immutable、scan-on-push、限定lifecycle、RETAIN |
| 2026-07-19 | cdk-nag 3.0.1 | https://github.com/cdklabs/cdk-nag#usage | CDK `Validations` pluginとunsuppressed finding 0を採用 |
| 2026-07-19 | ECR Managed Signing | https://docs.aws.amazon.com/AmazonECR/latest/userguide/managed-signing.html | Signer profile、repository限定registry rule、push時自動署名、status polling |
| 2026-07-19 | ECR signature verification | https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-signing-verification.html、https://docs.aws.amazon.com/signer/latest/developerguide/image-verification.html | Notation strict verificationを自動deploy gate、ECS hookを防御層に採用 |
| 2026-07-19 | ECR OCI v1.1 / Referrers API | https://docs.aws.amazon.com/AmazonECR/latest/userguide/images.html、https://docs.aws.amazon.com/AmazonECR/latest/APIReference/API_ListImageReferrers.html | signature、SBOM、provenance、scan assessmentをimage digestへ関連付ける |
| 2026-07-19 | ECS image URI / lifecycle hook | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/create-task-definition.html、https://docs.aws.amazon.com/AmazonECS/latest/developerguide/lambda-lifecycle-hooks.html | digest URI、`PRE_SCALE_UP` fail-closed admission |
| 2026-07-19 | AWS Signer pricing / ECR artifact quota | https://docs.aws.amazon.com/signer/latest/developerguide/Welcome.html、https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-signing.html | ECR連携Signer追加料金なし、reference artifactのquota/storage影響を運用へ反映 |
| 2026-07-19 | CDK VPC / FargateService 2.261.0 | https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_ec2.Vpc.html、https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_ecs.FargateService.html | NAT 0、Public IP、Spot専用、minimum 0/maximum 100、AZ rebalancing無効をassert |
| 2026-07-19 | ECS task IAM role | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html | `SourceAccount`とregion/account限定`SourceArn`でconfused deputyを防止 |
| 2026-07-19 | ECS Parameter Store injection | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-ssm-paramstore.html | execution roleの各parameter限定`ssm:GetParameters`、更新時のnew deploymentを採用 |
| 2026-07-19 | CloudWatch Logs data protection | https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/mask-sensitive-log-data.html | application/Exec log groupの非機密ログ原則に追加防御としてmask policyを適用 |
