---
title: Scale-to-Zero Completion Checklist
aliases:
  - Scale-to-Zero 完了判定
  - Scale-to-Zero Acceptance Checklist
tags:
  - shittim-chest
  - codex
  - checklist
  - acceptance
  - scale-to-zero
status: approved
created: 2026-07-28
updated: 2026-07-29
canonical_for: completion
related:
  - "[[10_scale-to-zero-goal]]"
  - "[[30_scale-to-zero-commit-plan]]"
---

# Scale-to-Zero Completion Checklist

## 0. 使用方法

この文書は[[10_scale-to-zero-goal]]の完了判定の正本である。

Codexは通常PR本文へ本チェックリストを転記するか、各節へ証跡リンクを記録すること。実行していない試験を完了扱いにしない。実環境でのみ確認可能な項目は「デプロイ後確認」へ分離する。

---

## 1. Preflight

- [ ] すべての`AGENTS.md`を確認した
- [ ] `main`を最新化した
- [ ] baselineのformatter/lint/type/test結果を記録した
- [ ] 既存mainの失敗と今回変更の失敗を区別した
- [ ] README、requirements、design-document mirrorを確認した
- [ ] RuntimeLifecycleとRuntimeAdmissionGatewayを確認した
- [ ] DynamoDB serializer/repositoryを確認した
- [ ] lease、fencing、recovery、Outboxを確認した
- [ ] CDK Stateful/Runtime Stackを確認した
- [ ] GitHub Actionsと既存Discord通知を確認した
- [ ] branch protectionとrequired checksを確認した
- [ ] 3文書の矛盾・曖昧さを通常PRへ記録した
- [ ] 現行リポジトリとの差異を通常PRへ記録した

証跡:

```text
PR/commit/link:
```

---

## 2. 実行境界

- [ ] AWSへwriteしていない
- [ ] `cdk deploy/destroy/bootstrap/watch`を実行していない
- [ ] ECS UpdateService/RunTaskを実行していない
- [ ] 実DynamoDBへwriteしていない
- [ ] ECRへpushしていない
- [ ] production deployment workflowを実行・承認していない
- [ ] Discord Developer Portalを変更していない
- [ ] Interactions Endpoint URLを変更していない
- [ ] Application Commandを変更していない
- [ ] 実Bot機能試験を実行していない
- [ ] 許可された既存CI/PR Discord通知だけを実行した
- [ ] secret、token、Webhook URLをログやPRへ出していない

証跡:

```text
AWS writeなし:
Discord Application変更なし:
許可された通知:
```

---

## 3. GitHubと中断耐性

- [ ] 専用branchを作成した
- [ ] 最初の実質的commit後に通常PRを作成した
- [ ] 論理単位ごとにcommitした
- [ ] 各論理commit後にpushした
- [ ] 未push commitを長時間蓄積しなかった
- [ ] PR checklistを更新した
- [ ] 中断時の再開情報がPRへ残る構成にした
- [ ] checkpoint commitがある場合、未完成内容を明記した
- [ ] サブエージェント成果を主担当が統合確認した
- [ ] 不必要なforce pushをしていない
- [ ] worktreeがcleanである
- [ ] 全commitがremoteへpush済み
- [ ] 最新commit hashを記録した

証跡:

```text
Branch:
PR:
Latest commit:
```

---

## 4. 既存不変条件

- [ ] requester_idだけを認可主体としている
- [ ] username/display nameを認可へ使用していない
- [ ] UUIDv7 Debate/Attempt IDを維持した
- [ ] immutable attempt historyを維持した
- [ ] Retry時に新Attemptを作成する
- [ ] deterministic votingを維持した
- [ ] application coreがSDK非依存である
- [ ] DynamoDB transaction受付を維持した
- [ ] Guild日次quotaを維持した
- [ ] global 3-slotを維持した
- [ ] lease 60秒、renewal 20秒を維持した
- [ ] fencing tokenを維持した
- [ ] strongly consistent operation-result idempotencyを維持した
- [ ] durable Discord Outboxを維持した
- [ ] Outbox nonce/content hash照合を維持した
- [ ] terminal遷移とslot解放の原子性を維持した
- [ ] GSIを候補発見専用としている
- [ ] base table条件付き更新をownershipの正本としている
- [ ] fail-closed schema migrationを維持した
- [ ] 通常経路でScanを使用していない
- [ ] Query 1MB paginationを維持した
- [ ] 400KB item上限検査を維持した
- [ ] graceful SIGTERMを維持した
- [ ] stale worker writeを拒否する
- [ ] recoverable debate再開を維持した
- [ ] command schema hashを維持した

---

## 5. 共通ModelとPort

- [ ] Ingress Request modelを追加した
- [ ] Runtime State modelを追加した
- [ ] status/interaction/status message stateを型で定義した
- [ ] startup/terminal deadlineを型付きで扱う
- [ ] Clock portを追加した
- [ ] ECS runtime control portを追加した
- [ ] Discord status publisher portを追加した
- [ ] malformed enum/stateをfail closedで拒否する
- [ ] fake Clock試験がある

証跡:

```text
Files:
Tests:
```

---

## 6. Ingress Request永続化

- [ ] 既存DynamoDB tableを使用した
- [ ] 新DynamoDB tableを追加していない
- [ ] FIFO sort keyを実装した
- [ ] Interaction ID冪等性recordを実装した
- [ ] duplicateでqueue、待機数、status message、討論を増やさない
- [ ] transactionalまたは同等の20件上限を実装した
- [ ] 20件待機中の21件目を永続化しない
- [ ] PENDING/CLAIMED/RETRYING/ACCEPTED/COMPLETED/REJECTED/FAILEDを実装した
- [ ] claim owner/expiryを保存する
- [ ] expired claimを回収できる
- [ ] 二重claimを条件付き更新で拒否する
- [ ] terminal時にqueue counterを減算する
- [ ] Query paginationを実装した
- [ ] TTLを削除以外の正確性へ使用していない
- [ ] malformed recordをfail closedで拒否する
- [ ] schema versionを明示した

証跡:

```text
Files:
Tests:
```

---

## 7. Runtime State永続化

- [ ] `CONTROL#RUNTIME`相当のrecordを追加した
- [ ] STOPPED/STARTING/READY/BUSY/IDLE/STOPPING/DEGRADEDを実装した
- [ ] generationを単調増加させる
- [ ] stale generation updateを拒否する
- [ ] desired_count、runtime_instance_id、wake_started_atを保持する
- [ ] STARTING中の追加Requestでwake_started_atをリセットしない
- [ ] idle_since、stop_eligible_atを保持する
- [ ] IDLE中のpollでidle_sinceをリセットしない
- [ ] strongly consistent readを使用する
- [ ] Runtime StateをDebate/Attempt/lease/Outboxの代替正本にしていない
- [ ] 不正状態遷移を拒否する

---

## 8. Discord HTTP署名検証

- [ ] API Gateway HTTP APIを前提とする
- [ ] raw bodyをJSON parse前に検証する
- [ ] `X-Signature-Ed25519`を検証する
- [ ] `X-Signature-Timestamp`を検証する
- [ ] header欠落・不正署名を401にする
- [ ] 古いtimestampを拒否する
- [ ] raw bodyを変更しない
- [ ] PINGへPONGを返す
- [ ] APPLICATION_COMMAND/MESSAGE_COMPONENTをparseする
- [ ] 新規議論、Retry、Cancel、必要なComponentを扱う
- [ ] Public KeyとBot tokenを分離した
- [ ] raw body、signature、Interaction tokenをログへ出さない
- [ ] pure function試験がある
- [ ] Lambda handlerへdiscord.py clientを持ち込んでいない

---

## 9. Discord Ingress Lambda

- [ ] Guild/channel validationを実装した
- [ ] Ingress Requestを先に保存する
- [ ] 保存失敗時にwake要求しない
- [ ] wake失敗時も保存済みRequestを保持する
- [ ] queue full/duplicateへ適切に応答する
- [ ] ephemeral初回応答を返す
- [ ] Status Publisher要求を行う
- [ ] Runtime wakeを要求する
- [ ] API Gateway event、Lambda Context、raw JSON、boto3 responseをapplication coreへ渡さない
- [ ] AWS SDK呼出しをportへ分離した
- [ ] Stubber/fake試験がある
- [ ] secretをログへ出さない

---

## 10. 公開Status Message

- [ ] 公開message作成を実装した
- [ ] status_channel_id/status_message_idを保存する
- [ ] STARTING/ACCEPTED/STARTUP_TIMEOUT/RECOVERED/TERMINAL_FAILED/REJECTEDを実装した
- [ ] duplicate invokeでmessageを重複作成しない
- [ ] 同一状態を毎分再編集しない
- [ ] Discord一時失敗をretryできる
- [ ] message消失時の扱いを定義した
- [ ] コントロール用Botだけを使用する
- [ ] 参加者Bot tokenをLambdaへ渡していない
- [ ] Interaction tokenを保存・使用していない
- [ ] Bot tokenをログへ出さない

---

## 11. Application入力分離

- [ ] application coreから`discord.Interaction`依存を除去した
- [ ] 新規議論/Retry/Cancelの型付きinputがある
- [ ] requester_idを認可へ使用する
- [ ] username/display nameは表示用途だけに使う
- [ ] source message/thread情報を型で渡す
- [ ] 既存accept/retry/cancel use caseへ接続した
- [ ] operation idempotencyを維持した
- [ ] 既存controller経路の回帰試験がある

---

## 12. Runtime Ingress Drainer

- [ ] 全Discord identity READY前にdrainしない
- [ ] command schema確認前にdrainしない
- [ ] recovery完了前にdrainしない
- [ ] FIFOでclaimする
- [ ] PENDINGと再試行可能RETRYINGを処理する
- [ ] claim owner/expiryを設定する
- [ ] crash後に別taskが回収できる
- [ ] slot不足をterminal FAILEDにしない
- [ ] slot不足でPENDING/RETRYINGへ戻しnext_attempt_atを設定する
- [ ] quota errorをREJECTED/FAILEDへ扱う
- [ ] 新規議論を既存quota/3-slotへ接続する
- [ ] Retryで新Attemptを作成する
- [ ] Cancelでrequester_id認可を行う
- [ ] accepted Debate/Attempt IDを保存する
- [ ] duplicate debateを防止する
- [ ] lease/fencingを迂回しない

---

## 13. 3分・15分起動期限

- [ ] startup_deadline_atはcreated_at+3分
- [ ] terminal_deadline_atはcreated_at+15分
- [ ] 2:59でtimeoutにしない
- [ ] 3:00でユーザー向けtimeout表示を行う
- [ ] 3分時点でterminalにしない
- [ ] 3分後もscale-upを継続する
- [ ] 3〜15分の復旧を処理しRECOVERED表示する
- [ ] 復旧後にexactly-once相当で開始する
- [ ] 14:59でterminalにしない
- [ ] 15:00でterminal FAILED、counter減算、最終表示を行う
- [ ] 15分超過Requestを後日実行しない
- [ ] fake Clockを使用する
- [ ] 実時間sleepを使用しない

---

## 14. Runtime Reconciler

- [ ] 1分周期をCDKで定義した
- [ ] lost wakeを回復する
- [ ] pendingなのにdesiredCount=0を回復する
- [ ] STARTING/retry可能DEGRADEDをdesiredCount=1へ収束させる
- [ ] expired claimを扱う
- [ ] duplicate invocationが冪等である
- [ ] DynamoDB成功/ECS失敗から収束する
- [ ] ECS成功/DynamoDB失敗から収束する
- [ ] ECS API一時失敗から次回収束する
- [ ] STOPPING中の新規Requestでgeneration増加、STARTING復帰、desiredCount=1へ収束する
- [ ] AWS API呼出しをportへ分離した
- [ ] Stubber/fake試験がある

---

## 15. 完全終了とIDLE

- [ ] Debate terminalだけではIDLEにしない
- [ ] 必須最終回答/エラー/キャンセルOutbox SENTを確認する
- [ ] application-owned task終了を確認する
- [ ] active lease、recovery、pending/claimed Outbox、status待ち、checkpointがないことを確認する
- [ ] IDLEでidle_sinceを一度だけ設定する
- [ ] stop_eligible_at=now+30分を設定する
- [ ] pollでidle_sinceを更新しない
- [ ] 新規Request、Retry、Cancel、expired claim、recoverable debate、pending Outbox、status待ち、recoveryでIDLEを解除する
- [ ] 29:59で停止しない
- [ ] 30:00以降に停止可能
- [ ] 複数討論の最後の完全終了から計測する

---

## 16. Scale DownとGraceful Shutdown

- [ ] scale down前にgeneration不変、未処理Ingressなし、新規処理なしを確認する
- [ ] STOPPINGへ条件付き遷移する
- [ ] desiredCount=0要求をport経由で行う
- [ ] STOPPING直前/UpdateService(0)直後のRequest raceを処理する
- [ ] stale generationで停止を拒否する
- [ ] admission close、新規claim停止、checkpoint、Outbox安全停止を行う
- [ ] Discord clientをcloseする
- [ ] bounded shutdown timeoutを設ける
- [ ] leaseを無条件解放しない
- [ ] stale worker writeをfencingで拒否する
- [ ] SIGTERM回帰試験がある

---

## 17. ECS/Fargate CDK

- [ ] ECS Service desiredCount=0
- [ ] FARGATEを使用する
- [ ] FARGATE_SPOTが存在しない
- [ ] CPU 512、Memory 1024 MiB
- [ ] active/max task count 1
- [ ] Public Subnet、assignPublicIp=trueを維持した
- [ ] NAT Gateway/ingressを追加していない
- [ ] HTTPS outboundのみを維持した
- [ ] digest-pinned image、read-only root filesystemを維持した
- [ ] CloudWatch Logs、Container Insights無効、通常ECS Exec無効を維持した
- [ ] task roleとexecution roleを分離した

---

## 18. HTTP API/Lambda CDK

- [ ] API Gateway HTTP APIを定義した
- [ ] REST API/ALBを追加していない
- [ ] DiscordIngress/StatusPublisher/Reconciler Lambdaを定義した
- [ ] 1分EventBridge Rule/Schedulerを定義した
- [ ] Lambda Log Groups、API stage、Endpoint URL Outputを定義した
- [ ] LambdaをVPCへ配置していない
- [ ] NAT Gatewayを追加していない
- [ ] 新DynamoDB tableを追加していない
- [ ] Public KeyとBot tokenを分離した
- [ ] Status Publisherへコントロール用Bot tokenだけを渡す
- [ ] 参加者Bot token/OpenAI API keyをLambdaへ渡していない
- [ ] secretをtemplateへ埋め込んでいない
- [ ] API routeとIAMを最小化した
- [ ] Log retentionを明示した

---

## 19. Stateful Resource保護

- [ ] DynamoDB PITR/deletion protection/RETAINを維持した
- [ ] 既存table replaceを発生させない設計である
- [ ] 新tableを作成していない
- [ ] live context lookupを追加していない
- [ ] AWS認証情報なしでsynthできる

---

## 20. Deploy Guard

- [ ] STOPPED/IDLEで通常deployを許可する
- [ ] STARTING/BUSY/STOPPING/未処理DEGRADEDで拒否する
- [ ] fail closedである
- [ ] break-glassを明示入力と理由必須にする
- [ ] 実行者、時刻、commit、理由、deploy前stateを監査する
- [ ] unit/local testがある
- [ ] production deployment workflowを実行していない

---

## 21. Security

- [ ] Ed25519署名検証とtimestamp replay対策を実装した
- [ ] invalid Guild/channelを拒否する
- [ ] API route最小、Lambda VPC外、least-privilege IAM
- [ ] Bot token配布を最小化した
- [ ] Interaction tokenを永続化していない
- [ ] raw payload/signature/secret/question全文を通常ログへ出さない
- [ ] API access logへbodyを含めない
- [ ] X-Rayへpayload/secretを含めない
- [ ] cdk-nag suppressionへ具体的理由を記載した
- [ ] GitHub Actions/Discord通知/PR/artifactへsecretを出さない
- [ ] security invariantを試験のために弱めていない

---

## 22. 必須テスト

### Discord Ingress

- [ ] 正常/不正署名、header欠落、timestamp期限切れ
- [ ] raw body保持、PING/PONG、Command、Component
- [ ] Guild/channel拒否、duplicate、token非保存
- [ ] queue 19件、20件、追加拒否、同時受付

### Status Publisher

- [ ] message作成/ID保存/duplicate
- [ ] STARTING、3分失敗、RECOVERED、FAILED、ACCEPTED、REJECTED
- [ ] Discord一時失敗、token非ログ

### Repository

- [ ] FIFO、pagination、conditional create、idempotency
- [ ] claim、expiry、二重claim拒否、RETRYING、terminal、counter
- [ ] malformed fail closed、TTL非依存

### Runtime

- [ ] READY/recovery前drain禁止
- [ ] FIFO、duplicate防止、slot不足、quota、Retry、Cancel
- [ ] crash回収、lease/fencing維持

### 起動期限

- [ ] 2:59、3:00非terminal、復旧継続、3〜15分復旧、14:59、15:00、期限切れ非実行

### IDLE

- [ ] Outbox SENT前非IDLE、最終SENT後IDLE
- [ ] FAILED/CANCELLED通知後IDLE
- [ ] 29:59不可、30:00可能
- [ ] 複数討論、新規Request/Retry解除、idle_since非リセット

### Reconciler Race

- [ ] STOPPING直前、UpdateService(0)直後、stale generation
- [ ] pending/desiredCount=0、duplicate invocation、ECS API失敗
- [ ] DynamoDB成功/ECS失敗、ECS成功/DynamoDB失敗、次回収束

### CDK

- [ ] desiredCount=0、FARGATE、FARGATE_SPOT不存在、CPU/Memory
- [ ] HTTP API、Lambda 3個、1分周期、VPC外、NAT 0
- [ ] 最小IAM、Output、log retention、cdk-nag、Stateful保護

---

## 23. ローカル検証

- [ ] formatter、linter、type checker成功
- [ ] unit/async tests成功
- [ ] DynamoDB serializer/Local integration成功
- [ ] AWS Stubber tests成功
- [ ] Discord mock tests成功
- [ ] Runtime tests成功
- [ ] CDK tests、cdk-nag、cdk synth成功
- [ ] CloudFormation template静的検査成功
- [ ] package/build、documentation、security checks成功
- [ ] 実時間sleep、実AWS、実Discord Applicationを使用していない

証跡:

```text
Commands:
Results:
CI:
```

---

## 24. CDK Synth静的検査

- [ ] desiredCount=0、FARGATE、CPU 512、Memory 1024
- [ ] FARGATE_SPOT不存在
- [ ] NAT Gateway追加なし、Lambda VPC設定なし
- [ ] 新DynamoDB tableなし
- [ ] HTTP API route/IAM最小
- [ ] secret埋込みなし
- [ ] Container Insights無効
- [ ] PITR/deletion protection/RETAIN維持
- [ ] live lookup依存なし
- [ ] AWS認証情報なしでsynth可能

---

## 25. GitHub ActionsとDiscord通知

- [ ] PR workflowがAWS deployを実行しない
- [ ] PR workflowがDiscord Applicationを変更しない
- [ ] production deployment jobとCIを分離した
- [ ] required/security checksを維持した
- [ ] PR/CI/security結果を既存機構で通知した、または不要理由を記録した
- [ ] 同一runの重複通知を抑制した
- [ ] workflow名、PR番号、commit、結果、GitHubリンクを表示した
- [ ] Webhook URL/tokenをログへ出していない
- [ ] 通知失敗をCI失敗と混同していない

---

## 26. Documentation

- [ ] README、requirements、ADRを更新した
- [ ] basic/detailed designを更新した
- [ ] DynamoDB、ECS/Fargate、Discord、RuntimeLifecycle設計を更新した
- [ ] security、threat modelを更新した
- [ ] operations/deployment/rollback/incident runbookを更新した
- [ ] cost assumptions、traceabilityを更新した
- [ ] 3分と15分の差、完全終了から30分、FIFO 20件と3-slotを説明した
- [ ] Interaction token非保存、Botオフライン許容、On-Demand Fargateを説明した
- [ ] AWS未デプロイ、Discord Application未変更を明記した
- [ ] rollout/rollback、デプロイ後確認を記載した

---

## 27. PR要件

- [ ] title/commit messageが規約準拠
- [ ] 背景、実装概要、主要設計判断を記載した
- [ ] AWS構成、DynamoDB補助record、Discord HTTP Interactionを記載した
- [ ] 3分/15分/30分、race対策、IAM/secret境界を記載した
- [ ] ローカル試験、CI、cdk synth、cdk-nag結果を記載した
- [ ] rollout/rollback、既知制約、デプロイ後確認を記載した
- [ ] AWS未デプロイ、Discord Application未変更を明記した
- [ ] 最新commit hashを記載した
- [ ] secret、account ID、個人情報を含めていない

---

## 28. Completion Criteria

次がすべて成立した場合のみ成功扱いにする。

- [ ] 実装、ローカル試験、CDK test、cdk-nag、cdk synth、template検査成功
- [ ] desiredCount=0、On-Demand FARGATE、CPU 512、Memory 1024、FARGATE_SPOT不存在
- [ ] HTTP Interaction、署名検証、FIFO 20件、冪等性、公開status message、token非保存
- [ ] Runtime State、Drainer、READY/recovery前処理禁止
- [ ] 3分非terminal、15分terminal、完全終了、30分IDLE、race対策
- [ ] deploy guard、least-privilege IAM、NAT追加なし、新tableなし
- [ ] 文書、rollout/rollback完成
- [ ] branch、commit、push、PR、required CI成功
- [ ] 既存Discord CI/PR通知実施
- [ ] AWS未変更、Discord Application未変更、deployment未実行
- [ ] worktree clean、全commit push済み、latest hash記録済み

---

## 29. デプロイ後確認

本Goalでは未実施として記録する。

- [ ] 実cold-start時間
- [ ] Discord実署名request、PING/PONG
- [ ] 実status message、Interactions Endpoint
- [ ] 実ECS起動、0.5 vCPU/1 GiB
- [ ] 実3分失敗、3〜15分復旧、15分terminal
- [ ] 実30分停止、wake/stop競合
- [ ] 実CloudWatch alarm、IAM認可
- [ ] 実deploy guard、break-glass監査、rollback

---

## 30. 最終判定

### GO

- [ ] Completion Criteriaがすべて完了
- [ ] required CI成功
- [ ] 未解決事項が成功を妨げない
- [ ] AWS未デプロイ
- [ ] Discord Application未変更
- [ ] PRをReview Readyにできる

### NO-GO条件

次のいずれかがある場合は成功扱いにしない。

- [ ] required check失敗
- [ ] security invariant未達
- [ ] race condition未解決
- [ ] cdk-nag/cdk synth失敗
- [ ] AWS write実行
- [ ] Discord Application変更
- [ ] Interaction token永続化
- [ ] FARGATE_SPOT残存
- [ ] 新DynamoDB table追加
- [ ] 30分の起点が最後の依頼
- [ ] 3分時点terminal FAILED
- [ ] 15分超過Requestが後日実行可能
- [ ] Outbox SENT前にIDLE
- [ ] 再開可能なcommit/push/PRが残っていない

NO-GO時は未完了項目、阻害要因、現在の安全な状態、最新push済みcommit、再開手順、次の作業を報告する。
