---
title: Scale-to-Zero Goal
aliases:
  - シッテムの箱 Scale-to-Zero 実装指示
  - Discord HTTP Interaction / Fargate Scale-to-Zero
tags:
  - shittim-chest
  - codex
  - goal
  - aws
  - discord
  - ecs
  - fargate
  - dynamodb
  - scale-to-zero
status: approved
created: 2026-07-28
updated: 2026-07-29
canonical: true
canonical_for: requirements-and-design
related:
  - "[[30_scale-to-zero-commit-plan]]"
  - "[[20_scale-to-zero-completion-checklist]]"
---

# Scale-to-Zero Goal

## 0. この文書の位置付け

この文書は、シッテムの箱へ次の機能を導入するための**要件・基本設計・詳細実装指示の正本**である。

- Discord HTTP Interaction Ingress
- On-Demand FargateのScale-to-Zero
- DynamoDBを用いた永続FIFO受付
- Interaction IDを用いた冪等受付
- 起動状態を依頼チャンネルへ公開するStatus Message
- 起動から3分時点のユーザー向け失敗表示
- 受付から15分までの自動復旧
- 15分経過時のterminal failure
- 最後の議論が完全終了してから30分後の自動停止
- 1分周期のRuntime Reconciler
- STOPPEDまたはIDLEに限定するproduction deploy guard
- GitHub上でのbranch、commit、push、PR、CI
- 既存のGitHub Actions通知機構を利用したDiscordへのCI・PR状況通知

Codexは、コード編集を開始する前にこの文書を最後まで読み、次の関連文書も全文確認すること。

- [[30_scale-to-zero-commit-plan]]
- [[20_scale-to-zero-completion-checklist]]

3文書の役割は次のとおりとする。

1. `10_scale-to-zero-goal.md`
   - 実装する機能
   - 維持する不変条件
   - 状態遷移
   - データモデル
   - 処理順序
   - セキュリティ
   - CDK
   - テスト要求
   - 実行境界
   - rolloutとrollback
2. `30_scale-to-zero-commit-plan.md`
   - commit単位
   - push規則
   - 通常PR
   - 中断時checkpoint
   - 再開手順
   - サブエージェント運用
3. `20_scale-to-zero-completion-checklist.md`
   - 実装完了判定
   - 試験証跡
   - CI証跡
   - 未デプロイ確認
   - デプロイ後確認項目

この文書だけを読んでも技術仕様を復元できるようにする。
チェックリストにしか存在しない要件を作らない。

3文書間で矛盾がある場合は、次の順に優先する。

1. 本文書の「絶対的な実行境界」
2. 本文書の「維持する既存不変条件」
3. 本文書の「確定済みの利用者判断」
4. 本文書の各機能詳細
5. `20_scale-to-zero-completion-checklist.md`
6. `30_scale-to-zero-commit-plan.md`

現在のリポジトリと本文書が食い違う場合、既存の安全性・整合性・認可・冪等性を弱めて本文書へ合わせてはならない。
コードと設計書を調査し、最小かつ安全な変更案を採用し、差異と判断理由を通常PRへ記録すること。

---

# 1. `/goal`での利用方法

## 1.1 `/goal`へ渡す短文

```text
/goal シッテムの箱へDiscord HTTP Interaction受付とOn-Demand FargateのScale-to-Zeroを実装する。通常時はECSタスク0、依頼時に0.5 vCPU・1 GiBのOn-Demand Fargateを起動し、最後の議論が完全終了してから30分後に停止する。既存の討論状態機械、認可、DynamoDB transaction、quota、lease、fencing、recovery、Outbox、冪等性を維持する。実装、ローカル検証、CDK test、cdk-nag、cdk synth、GitHubブランチ・commit・push・PR作成・CI成功までを完了する。AWSリソースの作成・変更、Discord Application設定の変更、実際のScale-to-Zero切替は行わない。既存のGitHub Actions通知機構を用いたDiscordへのCI・PR状況投稿は許可する。
```

## 1.2 `/goal`作成後に渡す通常メッセージ

```text
Obsidian Vault内の次の3文書を実装仕様として全文確認してください。
フォルダ：10_Project/10_シッテムの箱/100_Ondemand Fargate

- 10_scale-to-zero-goal.md
- 30_scale-to-zero-commit-plan.md
- 20_scale-to-zero-completion-checklist.md

3文書を最後まで読んだ後、コード編集前に次を通常PRの計画へ転記してください。

1. 絶対に変更してはならない不変条件
2. 許可される外部操作
3. 禁止される外部操作
4. 実装workstream
5. commit・push単位
6. Completion criteria
7. 指示間の矛盾または曖昧さ
8. 現在のリポジトリとの差異
9. 実環境でのみ確認可能な事項
10. 最初に実施する限定試験

10_scale-to-zero-goal.mdを要件・設計の正本、
30_scale-to-zero-commit-plan.mdをcommit・中断復旧の正本、
20_scale-to-zero-completion-checklist.mdを完了判定の正本として扱ってください。

各論理commitの直後にpushし、最初の実質的commit後に通常PRを作成してください。
AWSまたはDiscord Applicationへwriteする操作は実行しないでください。
GitHub上の変更と、既存機構によるDiscordへのCI・PR通知は許可します。
```

通常メッセージを送るためにGoalをpauseする必要はない。
Goal作成後、そのまま同じGoalへ通常メッセージとして送る前提とする。

---

# 2. Goalの対象範囲

本Goalで完了させる対象は次である。

## 2.1 調査

- リポジトリ内のすべての`AGENTS.md`
- README
- requirements
- design-document mirror
- domain
- application
- adapters
- runtime
- tools
- CDK
- GitHub Actions
- tests
- security checks
- release/deploy workflow
- 既存Discord通知機構
- branch protection
- required checks

## 2.2 実装

- HTTP Interactionの署名検証
- HTTP eventから型付きapplication inputへの変換
- Ingress Request model
- Runtime State model
- DynamoDB補助record
- FIFO Queue
- Interaction冪等性
- 20件待機上限
- claimとclaim expiry
- Runtime wake
- Runtime Reconciler
- Status Publisher
- Runtime Ingress Drainer
- 既存accept/retry/cancel use caseとの統合
- 完全終了判定
- IDLE遷移
- 30分後scale down
- graceful shutdown統合
- deploy guard
- CDKリソース
- IAM
- ログredaction
- ドキュメント

## 2.3 検証

- formatter
- linter
- type checker
- unit tests
- async tests
- property/contract tests
- DynamoDB Local integration tests
- botocore StubberまたはAWS SDK fake tests
- Discord API mock tests
- RuntimeLifecycle regression tests
- race tests
- CDK tests
- cdk-nag
- cdk synth
- CloudFormation template静的検査
- package/build
- documentation checks
- security checks
- GitHub Actions CI

## 2.4 GitHub

- 作業branch作成
- commit
- push
- 通常PR作成
- PR本文更新
- PR comment
- label、assignee、milestone
- review指摘への修正
- CI実行と再実行
- required checks確認
- 既存通知機構によるDiscord通知
- Review Ready化

PRのmergeは必須条件ではない。
利用者の指示、branch protection、required reviewが許せばGitHub上のmerge自体は許可されるが、AWS deploymentを伴ってはならない。

## 2.5 本Goalの終了地点

本Goalは原則として次で終了する。

```text
実装完了
+ ローカル検証完了
+ CDK test成功
+ cdk-nag成功
+ cdk synth成功
+ template静的検査成功
+ 通常PR作成
+ required CI成功
+ AWS未デプロイ
+ Discord Application未変更
```

---

# 3. 作業開始前の必須確認

開始前に必ず次を確認すること。

1. リポジトリ内のすべての`AGENTS.md`
2. 現在の`main`
3. README
4. requirements
5. design-document mirror
6. Discord Interaction adapter
7. Discord command registration
8. Discord component routing
9. Discord client supervisor
10. RuntimeLifecycle
11. RuntimeAdmissionGateway
12. Debate acceptance
13. Retry use case
14. Cancel use case
15. DynamoDB serializer
16. DynamoDB repository
17. DynamoDB transaction受付
18. operation-result idempotency
19. Guild日次quota
20. global slot
21. lease
22. fencing
23. recovery
24. durable Outbox
25. Outbox dispatcher
26. terminal transition
27. CDK Stateful Stack
28. CDK Runtime Stack
29. ECS Service定義
30. Fargate capacity provider設定
31. security group
32. VPC/subnet
33. secrets/parameters
34. GitHub Actions workflows
35. 既存のDiscord向けCI・PR通知機構
36. cdk-nag
37. CodeQL等のsecurity workflow
38. package/build workflow
39. documentation checks
40. deployment/release workflow
41. GitHub Environment
42. branch protectionとrequired checks

調査結果として、通常PRまたは作業計画へ次を記録する。

- 既存の物理Bot identity数
- control Botの役割
- participant Botの役割
- 現在のECS desiredCount
- 現在のcapacity provider
- 現在のCPU/memory
- 現在のDynamoDB table数
- serializer schema version
- Runtime READYの成立条件
- recovery完了条件
- admissionのfail-closed条件
- Outbox SENTの判定方法
- global slotの保持方法
- current deploy workflowの起動条件
- 既存Discord CI/PR通知の起動条件
- 実装指示とコードとの差異

コードを確認せず、ファイル名、class名、function名、schema version、AWS logical ID、secret名、workflow名を推測して実装してはならない。

---

# 4. Ultraモードとサブエージェント

本GoalはGPT-5.6 Sol Ultraの利用を想定する。

## 4.1 主担当が所有する事項

- 全体アーキテクチャ
- 不変条件
- data model
- schema migration
- shared model
- shared port
- DynamoDB key設計
- transaction境界
- race condition
- workstream間統合
- commit境界
- GitHub履歴
- PR本文
- Completion criteria
- 最終レビュー

## 4.2 サブエージェントへ委任可能なworkstream

- 既存コード調査
- Discord署名検証
- Ingress Request repository
- Runtime State repository
- Status Publisher
- Ingress Drainer
- Reconciler
- CDK/IAM
- test design
- security review
- documentation
- GitHub Actions/Discord通知確認

## 4.3 制約

- 全体設計を複数サブエージェントへ独立に決めさせない
- shared modelとportを主担当が先に確定する
- 同じファイルを複数サブエージェントが同時編集しない
- 同じbranchへ無秩序にcommit/pushしない
- サブエージェントもAWS writeを行わない
- サブエージェントもDiscord Applicationを変更しない
- 既存CI/PR Discord通知以外の実Discord投稿を行わない
- 各成果物を主担当が不変条件と照合する
- 最終統合試験は主担当が実施する
- 競合する提案を自動的に混ぜない
- 部分実装を完成扱いにしない

サブエージェントの成果物には次を含める。

- 担当範囲
- 調査結果
- 変更ファイル
- 実装内容
- 実行した試験
- 試験結果
- 未解決事項
- 他workstreamへの影響
- 推奨commit境界

commit、push、中断、再開は[[30_scale-to-zero-commit-plan]]に従う。

---

# 5. 絶対的な実行境界

## 5.1 許可するローカル操作

- コード編集
- 文書編集
- formatter
- linter
- type checker
- unit test
- async test
- property test
- contract test
- DynamoDB Local
- botocore Stubber
- AWS SDK fake/mock
- Discord HTTP/API fake/mock
- local container build
- local package build
- CDK unit test
- cdk-nag
- cdk synth
- `cdk.out`の静的検査
- CloudFormation templateの静的検査
- git diff
- git status
- git log
- git branch
- git commit
- git rebase
- git merge
- 公式資料のread-only参照

## 5.2 許可するGitHub操作

GitHubに関する変更と操作は許可する。

- branch作成
- commit
- push
- 通常PR作成
- PR更新
- PR本文編集
- PR comment
- review返信
- review指摘に基づく修正
- label
- assignee
- milestone
- issue作成・更新
- GitHub Actions workflow変更
- CI実行
- 非デプロイworkflowの`workflow_dispatch`
- CI job再実行
- Actions log確認
- artifact確認
- required checks確認
- branch update
- rebase
- conflict resolution
- repository内GitHub設定ファイル変更
- Dependabot等の設定変更
- GitHub上のmerge

ただし、GitHub Actions経由であっても、後述するAWS writeとDiscord Application変更を実行してはならない。

GitHub上のsecret、variable、environmentを扱う場合：

- secret値を取得・表示しない
- secret値をPR、comment、artifact、logへ出さない
- AWS production credentialsを新規追加しない
- 既存のDiscord通知secretを優先して再利用する
- deployment approvalを迂回しない
- branch protectionを迂回しない

## 5.3 許可するDiscord通知

以前実装されたGitHub ActionsまたはWebhookによる、DiscordへのCI・PR状況通知は許可する。

許可する通知：

- PR作成
- PR更新
- CI開始
- CI成功
- CI失敗
- required check結果
- security check結果
- review待ち
- merge結果
- workflow失敗要約

要件：

- 既存通知機構を優先して再利用する
- 既存方式で足りる場合、新しいWebhookを作成しない
- 新規Discord Bot/Applicationを作成しない
- Webhook URLやBot tokenをログへ出さない
- PR、comment、artifactへsecretを含めない
- 議題本文や個人情報を通知へ含めない
- 同一runの重複通知を抑制する
- workflow名、PR番号、branch、commit、結果、GitHubリンクを中心にする
- 通知失敗だけを理由に実装やCI本体を失敗扱いにしない
- 通知失敗は独立して報告する

この通知はDiscord実環境操作禁止の明示的な例外である。

## 5.4 禁止するAWS操作

次を実行してはならない。

- `cdk deploy`
- `cdk destroy`
- `cdk bootstrap`
- `cdk watch`
- `sam deploy`
- `terraform apply`
- `aws cloudformation deploy`
- `aws cloudformation create-stack`
- `aws cloudformation update-stack`
- `aws cloudformation delete-stack`
- `aws ecs update-service`
- `aws ecs run-task`
- Lambda functionの作成・更新・削除
- API Gatewayの作成・更新・削除
- EventBridge Rule/Schedulerの作成・更新・削除
- IAM role/policyの作成・更新・削除
- DynamoDB実環境へのwrite
- SSM Parameter Storeへのwrite
- Secrets Managerへのwrite
- ECRへのpush
- CloudWatch Alarmの作成・更新
- AWSアカウント内の永続的変更
- production cold-start試験
- 実Fargate起動・停止試験
- production deployment workflowの実行
- production deployment approvalの承認

AWS認証情報が利用可能でも実行してはならない。

## 5.5 禁止するDiscord操作

既存CI・PR通知を除き、次を実行してはならない。

- Discord Developer Portal変更
- Interactions Endpoint URL登録・切替
- Application Command登録・変更
- 実Bot tokenを使ったアプリケーション機能試験
- 実チャンネルでの議論依頼
- 実status message試験
- 実Retry/Cancel試験
- participant Botによる投稿
- production Discord Application変更

## 5.6 CDK synthの境界

`cdk synth`は許可する。

要件：

- AWS live context lookupへ依存しない
- deploy済みstackを参照しない
- production stackに対する`cdk diff`を実行しない
- AWS認証情報がなくてもsynthできる
- `cdk.out`はローカルまたはCI artifactとしてのみ扱う
- 既存方針で追跡対象でなければ`cdk.out`をcommitしない
- live lookupを必要とする新設計を追加しない

---

# 6. 目標アーキテクチャ

## 6.1 通常時

```text
ECS desiredCount = 0
ECS runningCount = 0
Discord Bot = オフライン表示を許容
Discord Application Command = 登録済みのまま
Fargate vCPU・memory課金 = なし
API Gateway/Lambda/DynamoDB等の待受系だけが存在
```

常時オンライン表示のための別Gateway process、別ECS Service、常駐Lambda接続は追加しない。

## 6.2 Interaction受付

```text
Discord
  -> API Gateway HTTP API
  -> DiscordIngress Lambda
  -> raw body署名検証
  -> Interactionの型付き変換
  -> Guild/channel validation
  -> Ingress Request永続化
  -> operation idempotency確定
  -> 公開Status Message作成要求
  -> Runtime wake要求
  -> ECS desiredCount=1へ収束
  -> Discord初回応答
```

Requestの永続化前にECSだけを起動してはならない。
ECS起動APIが失敗しても、保存済みRequestはReconcilerが回復可能でなければならない。

## 6.3 ECS起動後

```text
0.5 vCPU / 1 GiB On-Demand Fargate
  -> runtime process開始
  -> runtime_instance_id生成
  -> Discord supervisor開始
  -> 全physical identity READY
  -> command schema整合性確認
  -> recoverable debate列挙
  -> recovery初期処理完了
  -> Runtime READYまたはBUSY
  -> Ingress Drainer開始
  -> FIFO claim
  -> 既存accept/retry/cancel use case実行
  -> 議論開始
```

READY前またはrecovery完了前にIngressを処理してはならない。

## 6.4 停止

```text
最後の議論が完全終了
  -> pending/claimed/retrying Ingressなし
  -> active debate taskなし
  -> active leaseなし
  -> pending/claimed Outboxなし
  -> 必須Status updateなし
  -> Runtime IDLE
  -> idle_since固定
  -> stop_eligible_at = idle_since + 30分
  -> Reconcilerが停止条件再確認
  -> generation不変
  -> STOPPING
  -> desiredCount=0
  -> graceful SIGTERM
  -> STOPPEDへ収束
```

本Goalでは実AWS・実Discordで動作させない。
コード、試験、CDK template、文書で検証する。

---

# 7. 確定済みの利用者判断

以下は変更してはならない。

## 7.1 Botのオンライン状態

Fargate停止中に、各Discord Botがオフライン表示になることを許容する。

常時オンライン表示専用の別プロセスを追加しない。

## 7.2 Fargate

通常Service：

```text
capacity provider: FARGATE
FARGATE_SPOT: 使用しない
cpu: 512
memoryLimitMiB: 1024
desiredCount: 0
active task count: 1
maximum ECS task count: 1
```

On-Demand Fargateのみを使用する。
Spotとの混在、fallback、自動切替は実装しない。

## 7.3 討論並列数

- ECS task数は1
- 既存global slotは3
- 討論の同時実行数は既存どおり最大3
- Scale-to-Zeroを理由にglobal slotを変更しない

## 7.4 FIFO待機上限

- FIFO
- 待機上限20件
- 非terminalのPENDING、CLAIMED、RETRYINGを待機数へ含める
- すでにacceptされglobal slotで実行中の討論は20件へ含めない
- 20件待機中の21件目は永続化しない
- 21件目は明示的な混雑応答を返す

## 7.5 停止時刻

停止基準：

```text
最後の依頼から30分ではない
最後の議論が完全終了してから30分
```

1分周期Reconcilerを使う。

実停止が設計上次となることを許容する。

```text
30分00秒から30分59秒後
```

## 7.6 3分のユーザー向け失敗

各Ingress Requestの受付から3分以内にRuntimeがREADYにならない場合、ユーザー向けには起動失敗を表示する。

```text
⚠️ 3分以内にシッテムの箱を起動できませんでした。
依頼は保存されており、自動復旧を継続しています。
再実行は不要です。
```

3分時点ではterminal FAILEDにしない。
Requestを削除しない。
待機数を減らさない。
自動復旧を停止しない。

## 7.7 15分のterminal failure

自動復旧はRequest受付から最大15分まで継続する。

3分後から15分以内に復旧：

```text
✅ シッテムの箱が復旧しました。
議論を開始します。
```

15分経過しても開始できない場合：

- Requestをterminal FAILED
- queue counterを減算
- 自動起動対象から除外
- 公開messageを最終失敗へ更新
- 後日自動実行しない
- ユーザーが再実行できる状態にする

```text
❌ シッテムの箱を起動できませんでした。
依頼を再実行してください。
```

15分経過Requestが、翌日の起動や別Requestの起動時に実行されてはならない。

## 7.8 起動状態の公開

起動状態はephemeral応答だけでなく、依頼チャンネルへ公開する。

公開messageに含める項目：

- 状態
- 議題
- 依頼者表示名
- 受付時刻

Ingress Requestへ保存する。

- `status_channel_id`
- `status_message_id`
- `status_message_state`

Interaction tokenは永続化しない。

## 7.9 完全終了

### COMPLETED

- AttemptがCOMPLETED
- 必須最終回答OutboxがSENT
- 必須通知がSENT
- application-owned taskが終了

### FAILED

- AttemptがFAILED
- 必須エラー通知OutboxがSENT
- retry待ちでない
- application-owned taskが終了

### CANCELLED

- AttemptがCANCELLED
- 必須キャンセル通知OutboxがSENT
- application-owned taskが終了

FAILED/CANCELLED recordが存在するだけでRuntimeを起動し続けてはならない。
必須通知SENT前にIDLEへ移行してはならない。

## 7.10 Deployment

将来の通常production deployは次だけ許可する。

- STOPPED
- IDLE

次ではfail closedとする。

- STARTING
- BUSY
- STOPPING
- recovery中
- 未処理処理を持つDEGRADED

緊急deployは明示的なbreak-glassに限定し、監査情報を残す。

本Goalではdeploy guardを実装・試験・文書化するだけで、deploymentを実行しない。

---

# 8. 維持する既存不変条件

次を必ず維持する。

- `requester_id`だけを認可主体として使用する
- usernameとdisplay nameを認可へ使用しない
- UUIDv7 Debate ID
- UUIDv7 Attempt ID
- immutable attempt history
- Retry時に新Attemptを作る
- deterministic voting
- SDK非依存application core
- DynamoDB transaction受付
- Guild日次quota
- global 3-slot
- lease 60秒
- lease renewal 20秒
- fencing token
- strongly consistent operation-result idempotency
- durable Discord Outbox
- Outbox nonce/content hash照合
- terminal遷移とslot解放の原子性
- GSIは候補発見専用
- base tableの条件付き更新をownershipの正本とする
- fail-closed schema migration
- 通常経路でScanを使用しない
- Queryの1MB pagination
- DynamoDB item 400KB上限検査
- graceful SIGTERM
- stale worker write拒否
- recoverable debate再開
- command schema hash

Scale-to-Zeroを理由に次を再設計しない。

- Debate domain
- Attempt domain
- OpenAI adapter
- Evidence
- voting
- final decision
- lease
- fencing
- Outbox
- quota
- global slot

新しい受付・Runtime制御は既存application coreの外側へ追加し、既存use caseを再利用する。

---

# 9. 役割と責務境界

## 9.1 DiscordIngress Lambda

責務：

- raw HTTP request受信
- 署名検証
- timestamp検証
- PING処理
- 型付きInteraction変換
- Guild/channel validation
- Interaction冪等性確認
- queue上限確認
- Ingress Request永続化
- Runtime wake要求
- Status Publisher要求
- 初回ephemeral応答

責務外：

- AI議論
- Discord Gateway接続
- participant Bot操作
- Debate execution
- voting
- final answer生成
- lease保持
- Outbox dispatch

## 9.2 DiscordStatusPublisher Lambda

責務：

- control Botで公開message作成
- 公開message更新
- channel/message ID保存
- 状態遷移に応じた表示
- Discord一時失敗の再試行可能化
- 冪等なcreate/update

責務外：

- Interaction token保存
- participant Bot token使用
- AI議論
- Runtime起動判断の正本

## 9.3 RuntimeReconciler Lambda

責務：

- lost wake回復
- Runtime desired stateとECSの収束
- 3分Status更新要求
- 15分terminal failure
- expired claim検出
- 30分IDLE停止
- wake/stop race回復

責務外：

- Debate execution
- Discord Gateway接続
- application-owned task実行
- leaseを正本として書き換えること

## 9.4 ECS Runtime

責務：

- Discord Gateway接続
- 全identity READY
- command schema確認
- recovery
- Ingress Drainer
- accept/retry/cancel use case
- Debate execution
- Outbox dispatch
- graceful shutdown

## 9.5 DynamoDB

正本：

- Ingress Request
- Interaction operation result
- queue counter
- Runtime State
- Debate/Attempt
- lease/fencing
- Outbox

DynamoDB TTLは正確性の正本にしない。

---

# 10. Discord HTTP Interaction

## 10.1 受信経路

CDK template上で次を定義する。

```text
Discord
  -> API Gateway HTTP API
  -> DiscordIngress Lambda
```

禁止：

- API Gateway REST API
- ALB
- ECS上の常駐Web Server
- LambdaのVPC配置
- NAT Gateway追加

## 10.2 対象Interaction

少なくとも次を扱う。

- PING
- APPLICATION_COMMAND
- MESSAGE_COMPONENT
- 新規議論
- Retry
- Cancel
- 既存control panelの必要なComponent

新規議論だけHTTPへ移し、Retry/Cancelを旧Gateway経路へ残してはならない。
同じ操作がHTTPとGatewayの両方で二重受付されないようにする。

## 10.3 Raw Bodyと署名検証

JSON parseまたは業務処理より前に次を検証する。

- `X-Signature-Ed25519`
- `X-Signature-Timestamp`
- raw request body

要件：

- API Gateway eventから受信したraw bodyのbytesを正しく復元する
- base64 encoded bodyの有無を適切に扱う
- 署名検証前にJSONを再serializeしない
- raw bodyを変更しない
- header名の大文字小文字差を安全に扱う
- header欠落は401
- 不正hexまたは不正長は401
- 不正署名は401
- timestamp parse失敗は401
- 許容時間を超えたtimestampを拒否する
- 未来方向に異常なtimestampも拒否する
- PINGへPONG
- Public Keyを設定値として注入
- Public KeyとBot tokenを分離
- raw bodyをログへ出さない
- signatureをログへ出さない
- Interaction tokenをログへ出さない
- pure functionとして試験可能
- Lambda handlerへdiscord.py clientを持ち込まない

timestampの許容幅は既存security policyがあれば従う。
なければ明示的な定数として定義し、テストし、文書化する。
値をコード内へ散在させない。

## 10.4 型付き変換

HTTP payloadをそのままapplication coreへ渡さない。

少なくとも次の型付きinputへ変換する。

- Interaction ID
- Interaction kind
- application ID
- Guild ID
- channel ID
- requester ID
- requester username
- requester display name
- command name
- command options
- component custom ID
- source message ID
- source thread ID
- question
- received timestamp

Interaction tokenは初回応答に必要な一時データとしてhandler scope内だけで扱い、永続modelへ含めない。

未知のInteraction type、未知のcommand、未知のcomponentはfail closedまたは安全なunsupported応答にする。

---

# 11. 初回応答とStatus Message

## 11.1 初回応答

Discordの初回応答期限内に返せる構造にする。

停止中または起動中：

```text
⏳ シッテムの箱を起動しています。
チャンネルへ起動状況を表示します。
```

READY時：

```text
✅ 議論依頼を受け付けました。
チャンネルへ進行状況を表示します。
```

待機上限：

```text
❌ 現在20件の依頼が待機しています。
しばらくしてから再実行してください。
```

invalid Guild/channel、認可、入力エラーは、既存の安全なユーザー向けエラー形式へ合わせる。

初回応答はephemeralとする。

## 11.2 公開Status Message

公開messageには少なくとも次を含める。

```text
状態: <state>
議題: <questionまたは安全に短縮した表示>
依頼者: <display name>
受付時刻: <timestamp>
```

question全文を通常ログへ出してはならない。
公開messageへ表示する長さ、mention、Markdown、制御文字の安全化は既存Discord出力方針に従う。

status state：

- STARTING
- ACCEPTED
- STARTUP_TIMEOUT
- RECOVERED
- TERMINAL_FAILED
- REJECTED

状態遷移例：

```text
新規受付・Runtime未起動
  -> STARTING

Runtimeが処理可能
  -> ACCEPTED

3分経過・READYでない
  -> STARTUP_TIMEOUT

STARTUP_TIMEOUT後、15分以内に復旧
  -> RECOVERED
  -> ACCEPTEDまたは進行中表示

15分経過
  -> TERMINAL_FAILED

入力、Guild、channel、quota等の永続的拒否
  -> REJECTED
```

同じ状態を毎分再編集しない。
Status Publisher invocationが重複してもmessageを重複作成しない。

## 11.3 Message IDの永続化

保存：

- `status_channel_id`
- `status_message_id`
- `status_message_state`
- `status_message_updated_at`
- 必要なら安全なpublisher operation ID

message作成とID保存の間で失敗しても、重複作成を抑制できる設計にする。

既存Outboxを再利用できる場合は不変条件を維持して再利用する。
別の軽量publisher operationを用いる場合も、nonceまたはoperation IDによる冪等性を持たせる。

Interaction tokenは保存しない。

---

# 12. Ingress RequestのData Model

既存DynamoDB tableへ補助recordを追加する。
新しいtableを作らない。

## 12.1 FIFO Record例

```text
PK = CONTROL#INGRESS
SK = REQUEST#<UTC-sortable-created-at>#<interaction-id>
```

これは例であり、既存key naming規約がある場合はそれに合わせる。
ただし、ScanなしでFIFO Query可能なkey設計にする。

## 12.2 Operation Result例

```text
PK = INGRESS_OPERATION#<interaction-id>
SK = RESULT
```

既存operation-result idempotency abstractionを再利用できる場合は再利用する。

## 12.3 Queue Counter例

```text
PK = CONTROL#INGRESS
SK = COUNTER
```

既存counterまたはtransaction設計に合わせる。
待機上限20件を、eventually consistentなcount Queryだけで判定してはならない。

## 12.4 必要属性

- `record_type`
- `record_schema_version`
- `interaction_id`
- `operation_id`
- `interaction_kind`
- `command_name`
- `custom_id`
- `question`
- `requester_id`
- `requester_username`
- `requester_display_name`
- `guild_id`
- `channel_id`
- `source_message_id`
- `source_thread_id`
- `status_channel_id`
- `status_message_id`
- `status_message_state`
- `status_message_updated_at`
- `status`
- `created_at`
- `updated_at`
- `startup_deadline_at`
- `terminal_deadline_at`
- `next_attempt_at`
- `claim_owner`
- `claim_expiry`
- `delivery_attempt`
- `error_code`
- `error_detail_code`
- `accepted_debate_id`
- `accepted_attempt_id`
- `completed_at`
- `ttl`

status：

- PENDING
- CLAIMED
- RETRYING
- ACCEPTED
- COMPLETED
- REJECTED
- FAILED

3分経過はterminal statusにしない。
`status_message_state=STARTUP_TIMEOUT`または専用属性で表現する。

## 12.5 Interaction冪等性

同じInteraction IDの再送時：

- Queue recordを増やさない
- queue counterを増やさない
- Runtime generationを意味的に増やさない
- Status Messageを重複作成しない
- Debateを重複作成しない
- Attemptを重複作成しない
- 同じoperation resultを返す
- 既存terminal結果があれば、それに応じた応答を返す

Interaction IDだけで不足する既存operation identity規約がある場合は、その規約に従う。
ただし、Discord retryで同一操作が二重実行されないことを試験する。

## 12.6 20件上限

待機数に含める。

- PENDING
- CLAIMED
- RETRYING

含めない。

- ACCEPTED
- COMPLETED
- REJECTED
- FAILED
- すでにDebateとしてglobal slot内で実行中のもの

受付transactionの中で次を原子的または同等の条件付き設計で行う。

1. operation result未作成確認
2. queue counter `< 20`確認
3. Ingress Request作成
4. operation result作成
5. queue counter増加
6. Runtime wake state更新が同一transactionに適する場合は更新

ECS API呼出しはtransaction外となるため、保存後に実行し、失敗はReconcilerが回復する。

## 12.7 TTL

TTLは後日削除のみに使う。

TTLへ依存してはならないもの：

- 冪等性
- claim expiry
- 認可
- startup deadline
- terminal deadline
- queue counter
- IDLE判定
- Runtime wake
- terminal transition

---

# 13. Runtime State Data Model

既存DynamoDB tableへ次の補助recordを追加する。

```text
PK = CONTROL#RUNTIME
SK = STATE
```

key名は既存規約へ合わせてよいが、単一のRuntime State正本を持つ。

## 13.1 属性

- `record_type`
- `record_schema_version`
- `state`
- `generation`
- `desired_count`
- `runtime_instance_id`
- `wake_started_at`
- `last_request_at`
- `started_at`
- `ready_at`
- `busy_since`
- `idle_since`
- `stop_eligible_at`
- `stopping_at`
- `stopped_at`
- `updated_at`
- `last_error_code`
- `last_reconciled_at`
- 必要なら`version`または条件付き更新用属性

state：

- STOPPED
- STARTING
- READY
- BUSY
- IDLE
- STOPPING
- DEGRADED

## 13.2 状態遷移

型付き関数で管理する。
任意のstring代入で遷移させない。

代表遷移：

```text
STOPPED -> STARTING
STARTING -> READY
STARTING -> BUSY
STARTING -> DEGRADED
READY -> BUSY
READY -> IDLE
BUSY -> IDLE
BUSY -> DEGRADED
IDLE -> STARTING
IDLE -> STOPPING
STOPPING -> STOPPED
STOPPING -> STARTING
DEGRADED -> STARTING
DEGRADED -> READY
DEGRADED -> BUSY
DEGRADED -> IDLE
```

不正遷移はfail closedにする。

## 13.3 Interaction受付時

新しい有効Request受付時：

- generationを単調increment
- `last_request_at`更新
- `idle_since`削除
- `stop_eligible_at`削除
- STOPPED、IDLE、STOPPINGならSTARTING
- `desired_count=1`

duplicate Interactionでは、同じ操作の再送だけを理由にgenerationを増やさない。

STARTING中の追加RequestでRuntime全体の`wake_started_at`を毎回リセットしない。

各Requestの3分・15分はRequest自身の`created_at`を基準にする。

## 13.4 Runtime Stateの限界

Runtime Stateを次の代替正本にしてはならない。

- Debate
- Attempt
- lease
- fencing
- Outbox
- quota
- global slot
- operation result

Runtime Stateは、ECS runtimeの集約状態とdesired stateを表す補助正本である。

---

# 14. PortとAdapter

application coreへAWS SDK、Discord SDK、Lambda eventを持ち込まない。

少なくとも次のportを検討し、既存architectureへ合わせて定義する。

## 14.1 Clock

```text
now() -> datetime
```

fake Clockで3分、15分、30分を試験可能にする。

## 14.2 Ingress Repository

- create idempotent request
- get operation result
- query FIFO pending
- claim
- release/retry
- mark accepted
- mark terminal
- update status message metadata
- count/counter操作
- query expired claim
- query startup deadlines

## 14.3 Runtime State Repository

- strongly consistent get
- request wake
- mark started
- mark ready
- mark busy
- mark idle
- begin stopping
- mark stopped
- mark degraded
- conditional generation update

## 14.4 ECS Runtime Control

- describe current service/task state
- request desiredCount=1
- request desiredCount=0

AWS SDK responseをdomainへ漏らさない。

## 14.5 Discord Status Publisher

- create public status
- update public status
- map status state to content
- handle idempotent operation
- classify retryable/permanent error

## 14.6 Runtime Activity Inspector

完全終了とIDLE判定に必要な情報を集約する。

- pending ingress
- claimed ingress
- retrying ingress
- active application task
- active lease
- recovery activity
- pending Outbox
- claimed Outbox
- status update待ち
- checkpoint/shutdown activity

既存repositoryやruntime metricsを安全に組み合わせる。
Scanを追加しない。

---

# 15. DiscordIngress処理順序

処理順序を曖昧にしない。

## 15.1 PING

1. raw body取得
2. signature/timestamp検証
3. JSON parse
4. PING判定
5. PONG返却

DynamoDBやECSを不要に呼ばない。

## 15.2 新規議論

1. raw body取得
2. signature/timestamp検証
3. JSON parse
4. 型付きinput変換
5. command validation
6. Guild validation
7. channel validation
8. requester情報抽出
9. question validation
10. Interaction operation result確認
11. duplicateなら保存済み結果を返す
12. queue上限をtransaction内で確認
13. Ingress Request永続化
14. queue counter増加
15. Runtime wake state更新
16. Status Message作成要求
17. ECS desiredCount=1要求
18. ephemeral初回応答

実装上、初回応答期限のため15から17を完全同期できない場合は、永続化完了後に最小限の処理だけを行い、残りをReconciler/Publisherへ委ねる。
ただし、Requestが保存されていないのに成功応答を返してはならない。

## 15.3 Retry

1. signature検証
2. component/command parse
3. requester_id抽出
4. operation idempotency
5. 対象Debate/Attempt解決
6. requester_id認可
7. Ingress Request保存
8. Runtime wake
9. 既存Retry use caseへ後で接続
10. 新Attempt作成
11. IDLE解除

username/display nameで認可しない。

## 15.4 Cancel

1. signature検証
2. component/command parse
3. requester_id抽出
4. operation idempotency
5. 対象Debate/Attempt解決
6. requester_id認可
7. Ingress Request保存または既存control flowへ接続
8. Runtime wakeが必要なら要求
9. 既存Cancel use case実行
10. 必須キャンセル通知SENTまで完全終了にしない

## 15.5 永続的拒否

次はREJECTEDまたは適切なterminal errorにできる。

- invalid Guild
- invalid channel
- unsupported command/component
- invalid input
- authorization failure
- quota exceeded
- 対象不存在
- すでにterminalで再実行不可

slot不足や一時的Runtime不足はterminalにしない。

---

# 16. Ingress ClaimとDrainer

## 16.1 Claim対象

- PENDING
- 再試行可能なRETRYING

条件：

- `next_attempt_at`が未設定または現在以前
- claimが未設定または期限切れ
- `terminal_deadline_at`を超過していない
- operationがterminalでない

## 16.2 Claim時更新

- `status=CLAIMED`
- `claim_owner=<runtime_instance_id>`
- `claim_expiry=<now + claim duration>`
- `delivery_attempt += 1`
- `updated_at=now`

claim durationは既存の処理特性に合わせて定数化し、試験する。
lease 60秒と同一概念として混同しない。

## 16.3 Drainer開始条件

次をすべて満たすまでdrainしない。

- runtime process開始済み
- runtime_instance_id確定
- Discord supervisor開始済み
- 全physical identity READY
- command schema整合性確認済み
- recoverable debate列挙済み
- recovery初期処理完了
- Runtime READYまたはBUSY
- RuntimeAdmissionGatewayが受付可能

## 16.4 FIFO

FIFOはDynamoDB Query順で実現する。
通常経路にScanを使わない。

paginationを処理し、1MBで打ち切らない。

同時に最大3討論を実行できる既存global slotを尊重する。

## 16.5 新規議論の接続順序

既存コードの責務に合わせ、少なくとも次の処理を既存use caseへ接続する。

- channel解決
- starter message作成
- thread作成
- control panel作成
- request DTO作成
- 既存`accept_debate`
- Guild日次quota
- global 3-slot
- operation-result idempotency
- Debate/Attempt永続化
- accepted Debate ID保存
- accepted Attempt ID保存
- debate task開始

既存の処理順序が異なる場合は、その不変条件を維持する。

## 16.6 Slot不足

slot不足時：

- terminal FAILEDにしない
- queue counterを減らさない
- PENDINGまたはRETRYINGへ戻す
- `next_attempt_at`を設定
- claim owner/expiryを安全に解除または更新
- busy loopを避ける
- Status Messageを必要以上に更新しない

## 16.7 Crash Recovery

task crash後：

- claim expiry経過後に別runtimeが再取得できる
- operation idempotencyでDebate重複作成を防ぐ
- accepted Debate IDがあれば再作成せず既存状態を確認する
- stale runtimeのwriteを拒否する
- fencingを迂回しない

---

# 17. 3分・15分起動期限

各Request：

```text
startup_deadline_at = created_at + 3 minutes
terminal_deadline_at = created_at + 15 minutes
```

## 17.1 3分未満

Runtimeが未READYでもRequestはPENDING/CLAIMED/RETRYINGのまま維持できる。

2分59秒ではSTARTUP_TIMEOUTにしない。

## 17.2 3分到達

RuntimeがそのRequestを開始可能な状態でない場合：

- `status_message_state=STARTUP_TIMEOUT`
- 公開message更新要求
- Requestは非terminal
- queue counter維持
- Runtime desiredCount=1維持
- Reconcilerによる復旧継続
- 同じ状態を毎分再通知しない

3分判定はRuntime全体の`wake_started_at`ではなく、Request自身の`created_at`を基準にする。

## 17.3 3分から15分の復旧

RuntimeがREADYとなりRequestを開始可能になった場合：

- RECOVERED表示要求
- FIFO順序を維持
- operation idempotency確認
- claim
- 既存use case実行
- Debate重複作成禁止
- accepted stateへ更新
- queue counter減算

RECOVERED表示後の最終表示遷移は既存UXへ合わせる。

## 17.4 15分到達

まだ未処理の場合：

- terminal FAILED
- queue counter減算
- error code記録
- claim解除
- 最終失敗表示要求
- Runtime wakeの根拠から除外
- 後日自動実行しない

14分59秒ではterminalにしない。

15分処理と同時にRuntimeがREADYになったraceでは、条件付き更新で片方だけを成立させる。
terminal FAILED済みRequestをDrainerが実行してはならない。

---

# 18. 完全終了とIDLE

## 18.1 IDLEへ移行可能な条件

次をすべて満たす。

- pending ingress = 0
- claimed ingress = 0
- retrying ingress = 0
- application-owned debate task = 0
- active global lease = 0
- recovery task実行中でない
- pending Outbox = 0
- claimed Outbox = 0
- 必須Status update待ち = 0
- shutdown/checkpoint実行中でない

単にDebate/Attemptがterminalであるだけでは不十分。

## 18.2 IDLE開始

```text
state = IDLE
idle_since = now
stop_eligible_at = now + 30 minutes
```

すでにIDLEなら`idle_since`と`stop_eligible_at`をpollごとに更新しない。

IDLE時に新しい作業が見つかった場合はIDLEを解除する。

## 18.3 IDLE解除条件

- 新規議論
- Retry
- Cancel
- PENDING
- RETRYING
- expired CLAIMED
- recoverable debate
- pending Outbox
- claimed Outbox
- Status update待ち
- Runtime recovery
- application-owned task生成

## 18.4 FAILED/CANCELLED

FAILED/CANCELLED recordが存在するだけでIDLEを妨げない。

ただし、次が残っていればIDLEにしない。

- 必須エラー通知未SENT
- 必須キャンセル通知未SENT
- retry待ち
- application-owned task
- claim
- Outbox

---

# 19. Runtime Reconciler

CDK templateへ1分周期のReconcilerを定義する。

```text
rate(1 minute)
```

実際のRule/Scheduler作成は本Goalでは行わない。

## 19.1 責務

- lost wake回復
- desiredCount=1への収束
- ECS実状態確認
- Runtime State修復
- 3分Status更新
- 15分terminal failure
- expired claim回復
- 30分IDLE停止
- wake/stop race回復
- 重複invocationの冪等処理

## 19.2 Scale-up条件

少なくとも次を考慮する。

- PENDINGあり
- retry可能RETRYINGあり
- 期限切れCLAIMEDあり
- Runtime STARTING
- Runtime BUSYかつdesired_count不整合
- 再試行可能DEGRADED
- 未処理RequestがあるのにdesiredCount=0
- STOPPING中に新しいgenerationがある
- recoverable debateが存在する
- pending Outbox等によりruntimeが必要

Scale-upを要求する前に、terminal deadline超過Requestを除外またはterminal化する。

## 19.3 Scale-down条件

次をすべて満たす。

- `state=IDLE`
- `stop_eligible_at<=now`
- 未処理Ingressなし
- active taskなし
- active leaseなし
- pending/claimed Outboxなし
- pending Status updateなし
- generation不変
- `desired_count=1`
- 新しい処理なし

## 19.4 非原子性

DynamoDB updateとECS UpdateServiceは原子的でない。

そのため、次の失敗を前提にする。

- DynamoDB成功/ECS失敗
- ECS成功/DynamoDB失敗
- Lambda timeout
- duplicate invocation
- stale invocation
- throttling
- transient network error

冪等なreconciliationで次回実行時に収束させる。

## 19.5 STOPPING中の新規Request

新規Request受付：

- generation increment
- idle情報削除
- STARTING
- desired_count=1
- ECS desiredCount=1要求

すでにUpdateService(0)が成功していても、次回Reconcilerが1へ戻す。

stale generationを持つ停止処理が、新しいRequestを上書きしてはならない。

## 19.6 擬似コードの意図

```text
read runtime state strongly consistent
read/query actionable ingress without Scan
expire requests whose terminal deadline passed
publish due status transitions idempotently

if work requires runtime:
    conditionally move runtime toward STARTING/generation
    request ECS desiredCount=1 idempotently
    return

if runtime is IDLE and stop_eligible_at <= now:
    re-check activity and generation
    conditionally enter STOPPING
    request ECS desiredCount=0
    return

reconcile stored desired_count with observed ECS state
```

実際のコードは既存architectureへ合わせる。

---

# 20. Graceful Shutdown

既存SIGTERM処理を維持・統合する。

順序：

1. admission close
2. 新規Ingress claim停止
3. 新規Debate開始停止
4. active state checkpoint
5. Outbox dispatcherを安全停止
6. Discord clients close
7. bounded shutdown timeout
8. Runtime State更新
9. process終了

要件：

- leaseを無条件解放しない
- crashと正常停止を区別できる
- expiry後回収を維持する
- stale worker writeをfencingで拒否する
- STOPPING後の新規RequestをReconcilerが回復できる
- IDLE判定済みでもshutdown手順を省略しない
- shutdown途中の例外をログへ安全に記録する
- tokenやpayloadをログへ出さない

---

# 21. CDKリソース

template上で少なくとも次を定義する。

- API Gateway HTTP API
- DiscordIngress Lambda
- DiscordStatusPublisher Lambda
- RuntimeReconciler Lambda
- EventBridge RuleまたはScheduler
- Lambda Log Groups
- IAM Roles/Policies
- API Gateway stage
- Interactions Endpoint URL Output
- desiredCount=0のECS Service
- On-Demand FARGATE
- CPU 512
- memory 1024 MiB

## 21.1 ECS

維持：

- Public Subnet
- `assignPublicIp=true`
- NAT Gatewayなし
- ingressなし
- HTTPS outboundのみ
- digest-pinned image
- read-only root filesystem
- CloudWatch Logs
- Container Insights無効
- 通常taskのECS Exec無効
- task roleとexecution role分離

変更：

- desiredCountを0
- capacity providerをFARGATE
- FARGATE_SPOTを通常Serviceから除去
- CPU 512
- memory 1024

ECS Service Auto Scalingを追加しない。
最大task数を1とする。

## 21.2 Lambda

LambdaはVPC外とする。

個別functionの責務を分離する。

- Ingress
- Status Publisher
- Reconciler

必要なtimeout、memory、retry、reserved concurrencyは、既存規約と処理量に基づいて明示し、無根拠に過大化しない。

IngressはDiscord初回応答期限を意識し、長時間処理を行わない。

## 21.3 API Gateway

- HTTP API
- 必要最小route
- bodyをaccess logへ含めない
- stageを明示
- endpoint Output
- CORSを不要に広げない
- public endpointであることを署名検証で保護
- WAF等を本Goalの必須追加にしない

## 21.4 EventBridge

- 1分周期
- Reconciler Lambdaだけをtarget
- least-privilege invoke permission
- duplicate delivery前提
- payloadにsecretを含めない

## 21.5 DynamoDB

- 新しいtableを作らない
- 既存tableを再利用
- PITR維持
- deletion protection維持
- RETAIN維持
- table replacementを発生させない
- 必要なGSI追加は原則避ける
- GSIが必要なら既存設計と移行影響を詳細に説明し、通常経路Scan禁止を維持する

---

# 22. IAM

least privilegeを実装する。

## 22.1 Ingress Lambda

必要候補：

- 既存DynamoDB tableの必要なGet/Put/Update/TransactWrite/Query
- ECS ServiceのDescribe
- 対象ECS ServiceへのUpdateService
- Status Publisher invokeが直接必要なら対象functionだけ
- CloudWatch Logs

対象resourceを限定する。

## 22.2 Status Publisher Lambda

必要候補：

- 既存DynamoDB tableの必要なGet/Update
- control Bot token取得のread
- CloudWatch Logs
- 必要な外向きHTTPS

参加者Bot token、OpenAI API keyへのaccessを付与しない。

## 22.3 Reconciler Lambda

必要候補：

- 既存DynamoDB tableの必要なGet/Query/Update/TransactWrite
- 対象ECS ServiceのDescribe/UpdateService
- Status Publisher invoke
- CloudWatch Logs

wildcard resource/actionを安易に使用しない。

cdk-nag suppressionが必要なら、具体的なresource、理由、代替不能性を記載する。

---

# 23. SecretsとConfiguration

分離する。

- Discord Application Public Key
- control Bot token
- participant Bot tokens
- OpenAI API key
- AWS resource identifiers
- allowed Guild IDs
- allowed channel IDs
- timestamp tolerance
- startup timeout 3分
- terminal timeout 15分
- idle timeout 30分
- queue limit 20
- reconcile interval 1分

要件：

- Public Keyはsecretでなくても、設定値としてコードから分離する
- control Bot tokenだけをStatus Publisherへ渡す
- participant Bot tokenをLambdaへ渡さない
- OpenAI API keyをLambdaへ渡さない
- Interaction tokenを保存しない
- config値を複数箇所へ重複定義しない
- testで本番secretを必要としない
- CDK synthでsecret値を解決しない
- templateへsecret値を埋め込まない

---

# 24. Logging・Observability

## 24.1 ログへ出してはならないもの

- raw Interaction body
- signature
- Interaction token
- Bot token
- Webhook URL
- OpenAI API key
- question全文
- 個人情報の不要な詳細
- secret ARNから取得した値

## 24.2 ログへ出してよい識別子

必要最小限：

- hashed/redacted Interaction ID
- operation ID
- Debate ID
- Attempt ID
- Runtime generation
- runtime_instance_id
- state transition
- error code
- delivery attempt
- PR/CI通知ではPR番号とcommit

IDをログへ出す既存方針があれば従う。

## 24.3 Metrics/Alarm

本Goalでは実Alarm作成・実検証を行わない。
必要なmetric/運用観測点を文書化する。

候補：

- ingress accepted/rejected
- queue depth
- duplicate interaction
- startup timeout
- terminal startup failure
- runtime wake attempt
- reconciler failure
- status publisher failure
- scale-down attempt
- stale generation rejection
- claim recovery
- Outbox pending
- ECS start latency

実測していない値を報告しない。

---

# 25. Deploy Guard

将来のproduction deploy用guardを実装する。

## 25.1 許可

- STOPPED
- IDLE

## 25.2 拒否

- STARTING
- BUSY
- STOPPING
- recovery中
- 未処理処理を持つDEGRADED
- Runtime State読取不能
- malformed Runtime State
- strongly consistent read失敗

状態不明時はfail closed。

## 25.3 Break Glass

明示入力を必須とする。

監査項目：

- 実行者
- 実行時刻
- commit SHA
- 理由
- deploy前state
- workflow run ID
- 対象environment

break-glassを通常経路で自動有効化しない。
reasonなしで許可しない。

## 25.4 本Goalでの制約

- guardのコード実装
- unit test
- local test
- workflow静的確認
- 文書化

production deployment workflowは起動しない。

---

# 26. GitHub ActionsとDiscord通知

## 26.1 CI

既存workflowを確認し、正規コマンドを利用する。

少なくとも次を維持または追加する。

- formatter
- lint
- type check
- unit tests
- async tests
- security
- package/build
- SBOM等の既存check
- documentation safety
- CDK tests
- cdk-nag
- cdk synth
- template assertions

PR CIとproduction deploymentを分離する。

PR eventでdeployment jobが起動してはならない。

## 26.2 Discord通知

既存通知機構を利用する。

通知内容：

- repository
- workflow
- PR番号
- PR title
- branch
- commit
- result
- GitHub link

禁止：

- secret
- token
- Webhook URL
- AWS account ID
- question本文
- 個人情報
- Actions log全文

同じworkflow runの開始・成功・失敗を重複送信しない。

通知失敗はCI本体と分離し、PRへ記録する。

## 26.3 PR

最初の実質的commit後に通常PRを作成する。

PR本文：

- 背景
- Goal
- 実装概要
- 主要設計判断
- 変更したAWS構成
- AWS未デプロイ
- Discord Application未変更
- DynamoDB補助record
- HTTP Interaction
- 3分/15分
- 30分IDLE
- race対策
- IAM/secret境界
- ローカル試験
- CI結果
- cdk-nag
- cdk synth
- rollout
- rollback
- デプロイ後確認
- 既知制約
- 最新commit SHA

commit・pushの詳細は[[30_scale-to-zero-commit-plan]]に従う。

---

# 27. Security Requirements

次を満たす。

- raw body署名検証
- timestamp replay対策
- invalid Guild拒否
- invalid channel拒否
- unsupported operation拒否
- API route最小化
- Lambda VPC外
- least-privilege IAM
- Bot token最小配布
- Interaction token非保存
- raw payload非ログ
- signature非ログ
- secret非ログ
- question全文非ログ
- API Gateway access logにbodyなし
- X-Rayにpayload/secretなし
- cdk-nag suppressionへ具体的理由
- GitHub Actions logへsecretなし
- Discord CI/PR通知へsecretなし
- PR/comment/artifactへsecretなし
- malformed DynamoDB record fail closed
- stale generation拒否
- stale worker write拒否
- requester_id認可維持
- operation idempotency維持

security invariantをテスト容易化や実装短縮のために弱めない。

---

# 28. テスト要求

テスト名や配置は既存規約へ合わせる。

実時間sleep、実AWS、実Discord Applicationを使用しない。

## 28.1 Discord署名

- 正常署名
- 不正署名
- header欠落
- signature不正hex
- signature不正長
- timestamp parse失敗
- timestamp期限切れ
- 未来timestamp異常
- raw body非改変
- base64 body
- PING/PONG
- unsupported type
- raw body非ログ
- signature非ログ
- Interaction token非ログ

## 28.2 Ingress受付

- Application Command
- Message Component
- 新規議論
- Retry
- Cancel
- Guild拒否
- channel拒否
- invalid input
- duplicate Interaction
- operation result再利用
- queue 19件
- queue 20件到達
- 21件目拒否
- concurrent acceptance
- 保存失敗時wakeなし
- wake失敗時Request保持
- Status Publisher失敗時Request保持
- Lambda retry

## 28.3 Ingress Repository

- FIFO
- Query pagination
- conditional create
- operation idempotency
- queue counter増加
- queue counter減少
- counter下限
- claim
- claim expiry
- 二重claim拒否
- RETRYING
- next_attempt_at
- terminal transition
- 3分非terminal
- 15分terminal
- malformed record fail closed
- schema version fail closed
- TTL非依存
- 400KB上限

## 28.4 Runtime State

- STOPPED -> STARTING
- STARTING -> READY
- READY -> BUSY
- BUSY -> IDLE
- IDLE -> STOPPING
- STOPPING -> STOPPED
- STOPPING -> STARTING
- DEGRADED recovery
- 不正遷移拒否
- generation単調増加
- duplicate Interactionで不要増加なし
- STARTING追加Request
- wake_started_at非リセット
- idle_since保持
- stale generation拒否
- strongly consistent read
- malformed state fail closed

## 28.5 Status Publisher

- message作成
- message ID保存
- duplicate invocation
- STARTING
- ACCEPTED
- STARTUP_TIMEOUT
- RECOVERED
- TERMINAL_FAILED
- REJECTED
- 同一状態の重複更新抑制
- Discord 429/5xx相当
- permanent error
- message消失
- token非ログ
- Interaction token不使用

## 28.6 Drainer

- READY前drain禁止
- command schema確認前禁止
- recovery前禁止
- FIFO
- pagination
- claim
- duplicate Debate防止
- accepted ID保存
- slot不足RETRYING
- quota rejection
- Retry
- Cancel
- claim expiry
- crash recovery
- stale runtime拒否
- lease/fencing維持
- operation-result維持

## 28.7 3分・15分

fake Clockで実施。

- 2分59秒
- 3分
- 3分時点非terminal
- Status timeout一回のみ
- 自動復旧継続
- 3分01秒復旧
- 10分復旧
- 14分59秒
- 15分
- 15分terminal
- queue counter減算
- 15分後非実行
- 15分とREADY同時race
- duplicate Reconciler

## 28.8 IDLE

- Debate terminalだけでは非IDLE
- 最終Outbox SENT前非IDLE
- 最終Outbox SENT後IDLE
- FAILED通知前非IDLE
- FAILED通知後IDLE
- CANCELLED通知前非IDLE
- CANCELLED通知後IDLE
- active leaseあり非IDLE
- recovery中非IDLE
- Status update待ち非IDLE
- 29分59秒
- 30分
- 30分59秒
- 複数討論
- 最後の完全終了から計測
- 新規Requestで解除
- Retryで解除
- Cancelで解除
- idle_since非リセット

## 28.9 Reconciler Race

- pendingなのにdesiredCount=0
- STARTINGなのにECS停止
- DynamoDB成功/ECS失敗
- ECS成功/DynamoDB失敗
- duplicate invocation
- stale invocation
- throttling
- STOPPING直前Request
- UpdateService(0)直後Request
- stale generation
- 次回収束
- terminal deadline超過
- expired claim
- workなしで不要scale-upしない

## 28.10 Graceful Shutdown

- admission close
- 新規claim禁止
- active task checkpoint
- Outbox停止
- Discord close
- bounded timeout
- lease無条件解放禁止
- stale worker write拒否
- SIGTERM回帰
- shutdown中の新規Request回復

## 28.11 Deploy Guard

- STOPPED許可
- IDLE許可
- STARTING拒否
- BUSY拒否
- STOPPING拒否
- recovery拒否
- DEGRADED条件
- read失敗拒否
- malformed state拒否
- break-glass
- reason必須
- 監査項目
- PR workflow非deploy

## 28.12 CDK

- desiredCount=0
- FARGATE
- FARGATE_SPOT不存在
- CPU 512
- memory 1024
- task max 1
- HTTP API
- Lambda 3個
- EventBridge 1分
- Lambda VPC外
- NAT Gateway 0
- 新DynamoDB table 0
- IAM最小化
- endpoint Output
- log retention
- secret非埋込み
- Container Insights無効
- ECS Exec無効
- PITR維持
- deletion protection維持
- RETAIN維持
- cdk-nag
- synth

---

# 29. ローカル検証

リポジトリで定義された正規コマンドを調査して使用する。

最低限：

- formatter
- linter
- type checker
- unit tests
- async tests
- DynamoDB serializer tests
- DynamoDB Local integration tests
- AWS Stubber tests
- Discord mock tests
- Runtime tests
- CDK tests
- cdk-nag
- cdk synth
- CloudFormation template静的検査
- package/build
- documentation checks
- security checks

外部serviceが必要な試験は、local fake/mock/Stubberへ置き換える。

依存serviceや環境問題で実行できない場合：

- 成功したと報告しない
- skipped理由を記録
- 代替検証を記録
- Completion criteriaへの影響を記録
- PRをReview Readyにできるか判断する

---

# 30. CDK Synth後の静的検査

生成templateで次を確認する。

- FARGATE_SPOT不存在
- desiredCount=0
- CPU 512
- memory 1024
- ECS task max 1
- NAT Gateway追加なし
- Lambda VPC設定なし
- DynamoDB新規tableなし
- API route必要最小
- IAM scope
- secret埋込みなし
- Container Insights無効
- ECS Exec無効
- Public Subnet/assignPublicIp維持
- PITR維持
- deletion protection維持
- RETAIN維持
- endpoint Output
- EventBridge rate 1 minute
- Lambda 3個
- Log Group/retention
- live context lookup依存なし

templateの論理ID名だけに依存した脆弱な検査を避け、resource typeとpropertyを確認する。

---

# 31. Documentation

次を更新する。

- README
- requirements
- basic design
- detailed design
- DynamoDB design
- ECS/Fargate design
- Discord adapter design
- RuntimeLifecycle design
- security design
- threat model
- operations runbook
- deployment runbook
- incident recovery runbook
- cost assumptions
- traceability matrix

ADRを追加する。

題名例：

```text
Discord HTTP Interaction Ingress and On-Demand Fargate Scale-to-Zero
```

ADR：

- Background
- Context
- Decision
- Alternatives
- HTTP Interaction選択理由
- Botオフライン許容
- Status Message
- Interaction token非保存
- FIFO 20件
- 既存3-slot
- 3分ユーザー向け失敗
- 15分terminal failure
- On-Demand Fargate
- 0.5 vCPU / 1 GiB
- 完全終了から30分
- 1分Reconciler
- eventual convergence
- deploy guard
- break-glass
- GitHub PR/CI
- Discord CI/PR通知
- security
- cost
- rollout
- rollback
- consequences

---

# 32. Rollout Plan

本Goalでは実行せず、runbookだけを作成する。

順序：

1. 補助DynamoDB record対応をdeploy
2. serializer/schema互換性確認
3. Status Publisherをdeploy
4. Ingress Drainer対応Runtimeをdeploy
5. Reconcilerをdeploy
6. API Gateway/Ingress Lambdaをdeploy
7. Discord PING確認
8. 署名検証確認
9. invalid signature拒否確認
10. Status Message確認
11. ECS desiredCount=1を維持した状態でHTTP受付確認
12. 新規議論確認
13. Retry確認
14. Cancel確認
15. duplicate Interaction確認
16. queue上限確認
17. Interactions Endpoint切替
18. On-Demand FARGATE切替
19. CPU 512 / memory 1024確認
20. deploy guard有効化
21. desiredCount=0切替
22. cold-start計測
23. 3分失敗表示試験
24. 3分から15分の復旧試験
25. 15分terminal試験
26. 完全終了判定確認
27. 30分IDLE停止試験
28. STOPPING中Request試験
29. UpdateService(0)直後Request試験
30. alarm/運用確認

各手順へ記載：

- 前提条件
- 実行command
- 期待結果
- 確認方法
- 中止条件
- rollback
- 監視項目
- 保存する証跡
- 実行責任者
- 次工程へのgate

本Goal中にこのrunbookを実行してはならない。

---

# 33. Rollback Plan

少なくとも次を記載する。

- Interactions Endpointを旧経路へ戻す手順
- desiredCountを旧値へ戻す手順
- capacity providerを旧設定へ戻す手順
- CPU/memoryを旧設定へ戻す手順
- Ingress受付停止手順
- pending Requestの扱い
- Status Messageの扱い
- Reconciler停止手順
- Runtime Stateの復旧
- 補助recordの互換性
- serializer downgrade可否
- DynamoDB recordを削除せず安全に無視する方法
- deploy guard解除/復旧
- Discord通知workflowのrollback
- evidence保存
- rollback後検証

Stateful dataを破壊するrollbackを既定にしない。

---

# 34. Non-goals

本Goalでは次を実装または実行しない。

- AWS実環境deployment
- Discord Application設定変更
- production cold-start
- SQS
- Step Functions
- EventBridge Pipes
- DynamoDB Streams
- 新DynamoDB table
- OpenSearch
- Web管理画面
- ALB
- NAT Gateway
- ECS Service Auto Scaling
- 複数ECS task
- Fargate Spot
- LambdaでAI議論
- LambdaからOpenAI API呼出し
- participant BotのLambda移行
- requester_usernameによる認可
- display nameによる認可
- TTLによる排他
- TTLによるdeadline
- 常時オンライン用process
- Interaction token永続化
- existing debate domainの再設計
- voting algorithm変更
- model selection変更

---

# 35. Completion Criteria

詳細なチェックは[[20_scale-to-zero-completion-checklist]]を正本とする。

少なくとも次をすべて満たす。

1. desiredCount=0がCDK synth結果に存在
2. FARGATE On-Demandが存在
3. FARGATE_SPOTが通常Serviceから消えている
4. CPU 512
5. memory 1024
6. task max 1
7. HTTP Interaction Ingress
8. raw body署名検証
9. timestamp replay対策
10. Ingress Request永続化
11. FIFO
12. 20件上限
13. Interaction冪等性
14. Status Message
15. Interaction token非保存
16. Runtime State
17. generation fencing
18. Ingress Drainer
19. READY/recovery前処理禁止
20. 3分表示
21. 3分時点非terminal
22. 15分terminal
23. 15分超過Request非実行
24. 完全終了定義
25. Outbox SENT前非IDLE
26. 30分IDLE
27. wake/stop race対策
28. graceful shutdown
29. deploy guard
30. break-glass監査
31. least-privilege IAM
32. Lambda VPC外
33. NAT Gateway追加なし
34. 新DynamoDB tableなし
35. local tests成功
36. CDK tests成功
37. cdk-nag成功
38. cdk synth成功
39. template静的検査成功
40. documentation整合
41. rollout plan
42. rollback plan
43. branch作成
44. commit/push
45. 通常PR
46. required CI成功
47. Discord CI/PR通知
48. AWS未デプロイ
49. Discord Application未変更
50. deployment workflow未実行
51. worktree clean
52. 全commit push済み
53. 最新commit SHA記録

実環境でのみ確認可能な項目はCompletion criteriaに含めず、デプロイ後確認として分離する。

---

# 36. デプロイ後確認項目

本Goal中は未実施として記録する。

- 実cold-start時間
- Discord実署名request
- 実PING/PONG
- 実Status Message
- 実Interactions Endpoint
- 実ECS起動
- 実0.5 vCPU/1 GiB
- 実3分失敗
- 実3分から15分復旧
- 実15分terminal
- 実30分停止
- 実wake/stop race
- 実CloudWatch metric/alarm
- 実IAM認可
- 実deploy guard
- 実break-glass
- 実rollback

実測していないものを実測値として報告しない。

---

# 37. 最終報告

PR本文と最終応答へ次を記載する。

- 実装概要
- 変更した主要ファイル
- サブエージェントの分担
- サブエージェント成果の統合結果
- CDK上の追加・変更resource
- AWSへresourceを作成していないこと
- DynamoDB補助record
- schema version
- Discord受信方式
- raw body署名検証
- timestamp replay対策
- FIFO
- 20件上限
- Interaction冪等性
- Status Message
- Interaction token非保存
- Runtime State
- generation
- ECS起動sequence
- Drainer開始条件
- 3分処理
- 15分処理
- 完全終了
- 30分IDLE
- wake/stop race
- graceful shutdown
- deploy guard
- break-glass
- IAM
- secret境界
- logging判断
- 実行したローカル試験
- 各試験結果
- CDK test
- cdk-nag
- cdk synth
- template静的検査
- CI結果
- Discord通知結果
- PR URL
- 最新commit SHA
- 未解決事項
- 指示書との差異
- デプロイ後確認
- rollout
- rollback
- git diff概要
- worktree clean
- 全commit push済み
- AWS writeなし
- Discord Application変更なし
- deployment workflow未実行

実行していない試験を成功と報告してはならない。

Completion criteriaを満たせない場合はGoalを成功扱いにせず、次を明示する。

- 未完了項目
- 阻害要因
- 現在の安全な状態
- 最新push済みcommit
- 再開手順
- 次に実施する作業
