---
aliases:
  - The Shittim Chest AWS詳細設計
tags: [project, shittim-chest, aws, cdk, ecs, detailed-design]
status: production-1.0
created: 2026-07-16
updated: 2026-08-29
---

# AWS・CDK詳細設計

## 1. Environment and stacks

workloadは単一accountの`ap-northeast-1`、account-globalなcost resourceだけ`us-east-1`へ置く。
全resourceにProject／Environment／ManagedBy tagを付ける。

| Stack | Region | Ownership | Protection |
|---|---|---|---|
| Stateful | Tokyo | DynamoDB、ECR、Signer | termination protection、RETAIN |
| ReleaseIdentity | Tokyo | Runtime用とRecords用の分離されたGitHub OIDC role | termination protection |
| Runtime | Tokyo | network、API、4 Lambda、ECS、retained admission log、scheduler | replaceable runtime |
| Operations | Tokyo | metric filter、alarm、dashboard、SNS、EventBridge | replaceable monitoring |
| CostGovernance | Virginia | Budgets、anomaly subscription | global cost control |

通常Production ReleaseがChange Setを作る順序はStateful、Runtime、Operations、CostGovernanceである。
ReleaseIdentityはそのworkflow自身の権限なので、変更時は独立した先行更新工程とする。

## 2. Stateful resources

- DynamoDBはon-demand、PITR 35日、deletion protection、RETAINとする。
- DebateTableは`NEW_IMAGE` Streamを公開し、Records Projectorは後続Stackで購読する。source tableの
  key、index、retentionは変更しない。
- ECRはproduction imageのimmutable tag、enhanced continuous scan、暗号化を有効にする。
- lifecycleはtagged imageの最新3世代、untagged imageの最新3世代を残す。
- AWS Signer／Notation用profileとECR referrerをrelease supply chainに用いる。

## 3. Runtime network and compute

- VPCはpublic subnetのみ、NAT Gateway 0、taskへpublic IPv4を付ける。
- task security groupはingress 0、HTTPS egressだけを許可する。
- ECS serviceはARM64 On-Demand Fargate、512 CPU units、1,024 MiB、通常desired 0、最大1 task。
- production taskはread-only root filesystem、tmpfs、non-root userを使う。
- ECS Exec用の専用image／task definitionとwrite／root境界はprovisionしない。
- `stopTimeout=120`秒、Container Insightsは個人規模の費用を考慮して無効とする。
- production imageはsource SHAからcanonical ARM64条件でbuildし、digestでtask definitionへ固定する。

## 4. API and Lambda

| Component | Main contract |
|---|---|
| HTTP API | moderator Interactionのpublic endpoint、TLS、throttle |
| Ingress Lambda | ARM64、署名検証、durable acceptance、SnapStart alias |
| Status Publisher Lambda | desired public StatusをDiscord RESTへ収束 |
| Runtime Reconciler Lambda | durable stateとECS desired 0／1を収束 |
| Image Admission Lambda | task definition、image digest、signature／attestationを検証 |

EventBridge SchedulerがRuntime Reconcilerを1分間隔で起動する。LambdaはVPC外に置き、external APIと
AWS APIへの到達にNATを不要とする。reserved concurrencyとtimeoutはconstruct testで固定する。

## 5. IAM boundaries

- task roleはDynamoDBの必要partitionとStatus Publisher invokeだけを許可する。
- execution roleはnormal image pull、exact SSM secret injection、log writeだけを許可する。
- Lambda roleはhandlerごとに分離し、table leading key、function、service、log groupを限定する。
- `ecs:DescribeTaskDefinition`はresource-level permission非対応のため、Image Admission、Release Deploy、
  Records Admin Status Lambda roleで独立statementの`Resource: "*"`を用いる。Image Admissionと
  Release Deployはfamily、revision、container、digestをapplicationでexact validationする。Admin Status
  roleは`aws:RequestedRegion`をproduction regionへ限定し、handlerでexact ECS service task definition ARN、
  application container、ECR repository、digestを検証してからtagを解決する。
- Release roleは固定stack／`release-*` Change Set、ECR repository、Signer、artifact bucketへ限定する。
- Records plan／deploy／backfill／drift roleは既存Runtime Release roleから分離する。plan／driftは
  immutable main subject、deploy／backfillは`production` Environment subjectだけを信頼し、
  Records roleへsource DebateTableのread／write権限を付与しない。
- GitHub runnerへlong-lived AWS keyを渡さず、immutable repository identityのOIDCだけを使う。

## 6. Private configuration

- RuntimeConfig pointerは`v0004`、schemaはv2。4 Bot、Guild、allowed channels、farewell channelを
  exact validationする。
- PersonaConfig schema v1を4 slot分用意し、本文をCloudFormation parameterやrepositoryへ含めない。
- setup toolはv0003からv0004を再構成する場合もsecret本文を表示／local保存しない。
- CDKはsecret valueをlookupせず、parameter nameとmetadataだけを扱う。

## 7. Operations and cost

- CloudWatch Logsはdedicated group、retention、data protection policyを持つ。
- application EMFは固定namespace／dimension／metric allowlistで出力する。LambdaのJSON log envelopeへ
  EMF payloadを文字列として入れず、`_aws`をrootに持つ1行のJSON eventとして標準出力へ直接書く。
- critical／warning composite alarm、dashboard、abnormal ECS STOPPED EventBridge通知を作る。
- SNS email subscriptionはoperatorのconfirmationを必要とする。
- Project budget 20 USD、account budget 30 USD、actual／forecast通知とAWS managed service anomaly
  monitorのdaily subscriptionを使う。Budget metricは`NetUnblendedCost`である。

## 8. CDK quality

- root `package-lock.json`をdependency正本とし、strict TypeScript、Vitest、CDK assertions、cdk-nag、
  credentialなしsynthをCIで実行する。
- logical ID、stack name、cross-stack exportをtestで固定し、意図しないreplacementを検出する。
- CDK sourceとlive stackの差は週次drift workflowでread-only検出し、自動修復しない。

## 9. 公式資料確認記録

| 確認日 | 対象 | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-08-14 | Fargate networking | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-networking.html | awsvpcとpublic IP |
| 2026-08-14 | Fargate capacity | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-capacity-providers.html | On-Demand scale-to-zero |
| 2026-08-29 | ECS IAM | https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonelasticcontainerservice.html | DescribeTaskDefinition wildcardとAdmin Status application境界 |
| 2026-08-14 | AWS CDK | https://docs.aws.amazon.com/cdk/v2/guide/home.html | stack ownershipとsynth |
| 2026-08-14 | AWS Budgets | https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html | budget notification |
