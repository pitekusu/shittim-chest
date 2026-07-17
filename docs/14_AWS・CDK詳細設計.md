---
aliases:
  - The Shittim Chest AWS詳細設計
tags: [project, shittim-chest, aws, cdk, ecs, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-07-16
---

# AWS・CDK詳細設計

## 1. Environment

- 本番は単一AWS accountの`ap-northeast-1`だけとし、別のAWS開発環境を作らない。
- localとCIはfake、DynamoDB Local、SDK contract testを使用し、AWS credentialをCIへ渡さない。外部接続確認は本番deploy後の限定smoke testで行う。
- 全resourceへ`Project=shittim-chest`、`Environment=production`、`ManagedBy=cdk`を付ける。

## 2. CDK app・stack

| Stack | Resource | Policy |
|---|---|---|
| `ShittimChest-Prod-Stateful` | DynamoDB、ECR | termination protection、DynamoDB `RETAIN` |
| `ShittimChest-Prod-Runtime` | VPC、SG、ECS cluster/service/task、IAM、app/break-glass Exec log group | Statefulへ一方向依存 |
| `ShittimChest-Prod-Operations` | Container Insights、dashboard、composite alarm、SNS、EventBridge、Budget、Cost Anomaly Detection | Runtime/Statefulを監視 |

L2 constructを優先し、L1/escape hatchはADRで理由を残す。construct IDとlogical IDは初回deploy後に変更しない。`cdk.context.json`をcommitし、`cdk-nag`の`AwsSolutionsChecks`と`cdk synth --strict`を必須とする。

## 3. Network

- 2 AZ、Public Subnetだけ、NAT Gatewayなし、Internet Gatewayあり。
- Fargateは`awsvpc`、`AssignPublicIp=ENABLED`、routeは`0.0.0.0/0 -> IGW`。
- Security Groupはingress ruleなし。egressはTCP 443を許可する。
- ALB、NAT instance、DNS64、NAT64、Service Connectは作成しない。
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
| stop timeout | 120秒 |
| Circuit breaker | enable + rollback |
| Container Insights | enhanced observability有効 |

Spot singleton停止とcapacity不足中の全面停止を仕様として許容する。EventBridgeで`Your Spot Task was interrupted.`を記録・通知する。

application側のgraceful shutdown deadlineは90秒とし、`stopTimeout=120`の残り30秒をDiscord client close、log driver、container runtimeの終了余裕にする。AWSはSpot interruption時にSIGTERMを送り、configured `stopTimeout`後にSIGKILLするため、container実装時は値の省略を禁止する。

## 5. Container definition

- 平常taskはinit process有効、application userはnon-root、read-only root filesystem、privileged無効、Linux capability全削除、ECS Exec無効とする。
- app health checkはprocess/event-loop heartbeatだけを確認し、Discord/OpenAI障害をrestart理由にしない。
- `awslogs` modeは`blocking`を明示し、secret・質問・回答全文をstdoutへ出さない。
- 一時書込みは容量制限した`/tmp` mountだけに許可する。平常imageはECS Execだけのためにshell utilityを追加しない。

STEP-08AではPython `3.14.6-slim-trixie`とuv `0.11.29`のmulti-architecture image index digestを固定したmulti-stage `Dockerfile`を実装する。production imageはnumeric UID/GID `10001`、exec形式entrypoint、`SIGTERM` stop signalを使用し、uv、build cache、raw source、testを含めない。event loop ownerが5秒ごとにPID付きheartbeatを`/tmp/shittim-chest/heartbeat`へatomic更新し、health commandは20秒以内の更新、PID形式、process生存だけを本文出力なしで検査する。

Fargate task definitionは`tmpfs`をsupportしないため、productionの`/tmp`はSTEP-09で容量制限したtask bind mountとして定義する。local container試験では同等のtmpfsを使う。`initProcessEnabled=true`もtask definition側のSTEP-09で設定し、STEP-08A imageだけで設定済みとは扱わない。

### 5.1 Break-glass task definition

通常のlog/metric調査で不足し承認されたincidentだけ、stop-before-startでbreak-glass revisionへ切り替える。break-glass版はroot filesystemを書込み可能にし、ECS Exec、`/bin/sh`、`script`、`cat`、4つの`ssmmessages` action、専用CloudWatch Logs書込権限を有効にする。sessionはroot実行であることを前提に、`logging=OVERRIDE`、専用90日log group、開始理由・操作者・開始終了時刻を記録する。調査終了後は平常revisionへ戻し、Exec agentがないtaskへ置換されたことを確認する。

STEP-08Aの`break-glass` image targetはproduction runtimeへ`/bin/sh`、`cat`、`script`、`ps`だけを追加し、application実行時は引き続きUID/GID `10001`とする。root filesystem書込み可否、Exec agent、IAM、log group、承認workflowはimageではなくSTEP-09/10のbreak-glass task revisionで制御する。

## 6. ECR

- tag immutability有効、scan-on-push有効、deployはcommit SHA tagとdigestで固定する。
- untagged/candidate imageだけを14日でexpireし、`release-*`と`git-<full-sha>`は自動削除しない。現行・直前正常digestをdeploy manifestとともに保護し、lifecycle preview後に適用する。
- ARM64 imageを必須、x86_64はcompatibility fallbackとして同一sourceからbuildする。

## 7. IAM

- Execution role: ECR pull、CloudWatch Logs、task definitionが参照するversion付きParameterの`ssm:GetParameters`、必要なKMS decrypt。
- 平常Task role: 対象DynamoDB table/indexとCloudWatch EMFだけ。secret読取、`ssmmessages`、Exec log group書込権限を持たない。
- Break-glass Task role: 平常権限に加え4つの`ssmmessages` actionと専用Exec log group書込だけを一時的に許可する。
- GitHub plan roleはimmutable main subject、deploy roleはimmutable `production` Environment subjectに限定する。planはchange set作成、deployはEnvironment承認済みchange set実行、drift roleはread-onlyとする。
- `iam:PassRole`は対象execution/task role ARNと`iam:PassedToService=ecs-tasks.amazonaws.com`へ限定する。
- ECS task trustは`ecs-tasks.amazonaws.com`。serviceが提供するcontext keyを公式資料で確認してconfused-deputy条件を付ける。

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
- `Project` user-defined cost allocation tagをBillingで有効化し、反映後にProject tag budget 50 USD、account全体budget 50 USD、OpenAI project budget 50 USDを設定する。
- Budgetはactual 80%/100%とforecasted 100%を通知し、自動停止actionは設けない。Cost Anomaly Detectionのservice monitorも有効化する。
- DynamoDB PITRは35日、stack削除でもtableをretainする。業務dataにTTLを設定しないが35日より古い状態の復旧は保証せず、AWS Backupは作成しない。
- DynamoDB on-demand maximum throughputは負荷試験前に推測値を設定しない。初回本番計測後に必要性と値をADRで決定し、設定する場合はthrottle alarmと同時に導入する。

## 10. 公式資料確認記録

| 確認日 | 対象version/service | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-07-16 | Fargate network | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-networking.html | awsvpc、Public IP、IPv6条件 |
| 2026-07-16 | Fargate Spot | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-capacity-providers.html | Spot専用、停止設計 |
| 2026-07-16 | ECS Exec | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-exec.html | root、writable root、logging |
| 2026-07-16 | Container Insights | https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-enhanced-observability-metrics-ECS.html | `RunningTaskCount`、enhanced observability |
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
