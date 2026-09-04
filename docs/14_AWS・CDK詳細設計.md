---
aliases:
  - The Shittim Chest AWS詳細設計
tags: [project, shittim-chest, aws, cdk, ecs, detailed-design]
status: production-1.0
created: 2026-07-16
updated: 2026-09-04
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
| RecordsStateful | Tokyo | Archive／Statistics／Session、Media、Memorial upload／queue | termination protection、RETAIN |
| RecordsApplication | Tokyo | Records API／Projector／collector／ADMIN／Memorial Lambda | replaceable application |

通常Production ReleaseがChange Setを作る順序はStateful、Runtime、Operations、CostGovernanceである。
ReleaseIdentityはそのworkflow自身の権限なので、変更時は独立した先行更新工程とする。

## 2. Stateful resources

- DynamoDBはon-demand、PITR 35日、deletion protection、RETAINとする。
- DebateTableは`NEW_IMAGE` Streamを公開し、Records Projectorは後続Stackで購読する。source tableの
  key、index、retentionは変更しない。
- ECRはproduction imageのimmutable tag、enhanced continuous scan、暗号化を有効にする。
- lifecycleはtagged imageの最新3世代、untagged imageの最新3世代を残す。
- AWS Signer／Notation用profileとECR referrerをrelease supply chainに用いる。
- RecordsのMemorial upload bucketはversioningなし、S3 managed encryption、全public access block、TLS必須、
  RETAINとする。production originからのpresigned POSTだけをCORSで許可し、原本と未完了multipart uploadは
  1日で期限切れにする。access logは既存Media access-log bucketの専用prefixへ送る。
- Memorial generation queueはSQS managed encryption、TLS必須、retention 1日、visibility timeout 30分、
  batch 1とする。最大4 receive後は14日retentionの専用DLQへ移し、Projector DLQと混在させない。
  永続checkpointのpaid logical attempt上限は3のままとし、物理receive回数でpaid可否を決めない。通常の3回に加えた
  物理配送余地により、`generation_attempt=3`のclaimがhard timeout／OOM／runtime crashとなった場合も、次の配送が
  stale leaseをattempt 4として回収し、completion-onlyまたはproviderを呼ばないterminal化へ収束できるようにする。

## 3. Runtime network and compute

- VPCはpublic subnetのみ、NAT Gateway 0、taskへpublic IPv4を付ける。
- task security groupはingress 0、HTTPS egressだけを許可する。
- ECS serviceはARM64 On-Demand Fargate、512 CPU units、1,024 MiB、通常desired 0、最大1 task。
- production taskはread-only root filesystem、tmpfs、non-root userを使う。
- ECS Exec用の専用image／task definitionとwrite／root境界はprovisionしない。
- `stopTimeout=120`秒、Container Insightsは個人規模の費用を考慮して無効とする。
- production imageはsource SHAからcanonical ARM64条件でbuildし、digestでtask definitionへ固定する。
- RuntimeはReleaseからstrictな`RecordsPublicHostname` parameterを受け、
  `SHITTIM_RECORDS_MEMORIAL_URL=https://<hostname>/memorial`をproduction taskへ渡す。URL本文やhostnameを
  secret parameterへ複製せず、Discordの公開解放導線だけに使用する。

## 4. API and Lambda

| Component | Main contract |
|---|---|
| HTTP API | moderator Interactionのpublic endpoint、TLS、throttle |
| Ingress Lambda | ARM64、署名検証、durable acceptance、SnapStart alias |
| Status Publisher Lambda | desired public StatusをDiscord RESTへ収束 |
| Runtime Reconciler Lambda | durable stateとECS desired 0／1を収束 |
| Image Admission Lambda | task definition、image digest、signature／attestationを検証 |
| Records Admin Status Lambda | allowlist済みAWS／CloudWatch状態のread-only集約、reserved concurrency 2 |
| Records Admin Config Lambda | runtime promptの参照、immutable revision作成、rollback、audit、reserved concurrency 2 |
| Records Memorial API Lambda | owner-onlyの状態／upload／生成／履歴／reset API、15秒、reserved concurrency 2 |
| Records Memorial Worker Lambda | SQS 1件ずつの画像／文章生成、5分、1,024 MiB、reserved concurrency 1 |

EventBridge SchedulerがRuntime Reconcilerを1分間隔で起動する。LambdaはVPC外に置き、external APIと
AWS APIへの到達にNATを不要とする。Admin Config／Statusは同一管理画面の並行readを受理しつつ、
下流を保護する上限としてreserved concurrency 2を使う。reserved concurrencyとtimeoutはconstruct testで固定する。

## 5. IAM boundaries

- task roleはDynamoDBの必要partitionとStatus Publisher invokeだけを許可する。
- Runtimeがsource親愛度profileをopaque key化するため、task roleは既存Records identity HMAC parameterの
  exact ARNに対する`ssm:GetParameters`だけを持つ。container環境変数へはparameter名だけを渡し、
  CloudFormation dynamic referenceやsecret値を置かない。
- execution roleはnormal image pull、exact SSM secret injection、log writeだけを許可する。
- Lambda roleはhandlerごとに分離し、table leading key、function、service、log groupを限定する。
- Records Admin Config roleはSessionの`SESSION#*`読込、Statisticsの`ADMIN#PROMPT` transaction、
  exact legacy／管理者parameter読込、runtime prompt subtreeの条件付きPutに限定する。revision Putは
  `ssm:Overwrite=false`だけを許し、overwriteを明示的に拒否する。保持期限を過ぎた非active revisionには
  fixed-length revision subtreeだけの`ssm:DeleteParameters`を許し、`active` parameterは削除対象resourceへ
  含めない。Admin StatusのAWS状態取得権限を共有しない。
- Memorial API roleはSession read、source v9 affection profileのGet／Update／transaction、Statisticsの
  owner memorial partitionだけのGet／Query／Put／Update／transaction、temporary upload object、generation
  queue send、Session key／OAuth Origin設定の読込、private memorial画像Getへ限定する。S3の不存在判定に必要な
  bucket-level `ListBucket`は、APIではupload bucketの`uploads/*`とMedia bucketの`memorials/*`、
  WorkerではMedia bucketの`memorials/*`だけをprefix conditionで許可する。Workerに一時写真の列挙権限を
  与えず、どちらもbucket全体を列挙させない。Worker roleはsource tableを読まず、Statistics checkpointの
  Get／Update、Archive GSI3 Query、temporary uploadのGet／Delete、participant画像Get、memorial画像Put、
  generation queue consume、OpenAI keyとactive／legacy participant promptのexact readだけを許可する。
- Admin Status roleはMemorial API／WorkerのLambda状態とgeneration queue／DLQ属性だけを追加で読み、
  message本文、upload object、owner checkpointを取得しない。既存のProjector DLQ表示と権限を維持する。
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
- RecordsApplicationはReleaseがRuntime Stackから検証した`vNNNN`のlegacy config versionとparameter名だけを
  Admin Config Lambdaへ渡す。Lambdaはactive pointerがない間だけ、そのversionの5本文をlegacy sourceとして読む。
- setup toolはv0003からv0004を再構成する場合もsecret本文を表示／local保存しない。
- CDKはsecret valueをlookupせず、parameter nameとmetadataだけを扱う。
- Memorial OpenAI keyは専用setup toolのhidden inputで上書きせず登録し、Records ReleaseはSecureStringの
  metadataだけを事前確認する。値はLambda環境変数、CloudFormation、workflow、artifactへ渡さない。
- RecordsEdgeはReleaseが同account／regionのMemorial upload bucketから決定したexact regional S3 domainを
  `RecordsMemorialUploadOriginDomain` parameterで受け、CSPの`connect-src`を`'self'`とそのoriginだけへ限定する。
  bucket wildcardや任意URLは許可しない。

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
