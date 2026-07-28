---
title: Scale-to-Zero Commit Plan
aliases:
  - Scale-to-Zero 中断復旧計画
  - Scale-to-Zero Commit・Push計画
tags:
  - shittim-chest
  - codex
  - git
  - github
  - checkpoint
  - scale-to-zero
status: approved
created: 2026-07-28
updated: 2026-07-29
canonical_for: commit-and-resume
related:
  - "[[10_scale-to-zero-goal]]"
  - "[[20_scale-to-zero-completion-checklist]]"
---

# Scale-to-Zero Commit Plan

## 0. 文書の位置付け

この文書は、[[10_scale-to-zero-goal]]を長時間実行するときのcommit、push、Draft PR、中断、再開手順の正本である。

使用量上限、セッション終了、ツール障害、CI障害、作業環境切替が起きても、GitHub上の最新push済みcommitとDraft PRから安全に再開できる状態を維持する。

---

## 1. 基本原則

- 1つの巨大commitへまとめない
- 論理的に独立した変更単位ごとにcommitする
- 各論理commitの直後にpushする
- 未commit変更を長時間保持しない
- 未push commitを複数蓄積しない
- 実装だけを先行させ、試験を最後へまとめない
- 各機能の試験を原則として同じcommitへ含める
- push済み履歴を安易に書き換えない
- force pushを原則使用しない
- 最初の実質的commit後にDraft PRを作成する
- Draft PRのチェックリストを進捗の正本とする
- 各commitでsecurity invariantを維持する

各commitは最低限、次を満たす。

- 変更目的が1つに絞られている
- commit messageだけで内容を判別できる
- 無関係なformat変更を含まない
- secret、token、account IDを含まない
- `git diff --check`が成功する
- 変更範囲のformatter、lint、type check、testが成功する
- 一時的に安全性を無効化したコードを残さない
- 後続commitがなくても変更意図を追跡できる

既存commit規約がある場合はそれを優先する。

---

## 2. 作業開始時

1. `main`を最新化する
2. worktreeがcleanであることを確認する
3. 専用branchを作成する
4. baselineの主要検査を実行する
5. 既存mainの失敗を記録する
6. Draft PR用チェックリストを準備する

branch名は既存規約に従う。規約がない場合:

```text
feat/discord-http-scale-to-zero
```

baseline確認だけを目的とした空commitは作らない。

---

## 3. Commit単位

実際のリポジトリ構造に合わせて順序やファイル境界は調整できる。ただし責務境界は原則維持する。

複数単位を統合する場合は、個別ではbuild、type safety、schema整合性が成立しない等の理由をPRへ記録する。

### Commit 1: 共通ModelとPort

対象:

- Ingress Request model
- Runtime State model
- status enum、interaction kind、status message state
- startup/terminal deadline
- Clock port
- ECS runtime control port
- Discord status publisher port
- 共通例外
- serializerに必要な型

試験:

- model validation
- enum fail closed
- deadline計算
- 不正状態拒否
- fake Clock

外部adapterは実装しない。

```text
feat(domain): add ingress and runtime state models
```

完了後、直ちにpushする。

---

### Commit 2: Ingress Request永続化

対象:

- FIFO Queue record
- Interaction ID冪等性record
- transactional queue counter
- 20件上限
- condition expression
- serializer/deserializer
- schema version
- claim、claim expiry、retry、terminal transition、TTL

試験:

- FIFO
- conditional create
- duplicate Interaction
- 19件から20件、20件時の追加拒否
- 同時受付競合
- claim、二重claim拒否、expiry回収
- terminal時counter減算
- malformed record fail closed
- pagination
- TTL非依存

既存transaction、400KB検査、operation-result idempotencyを維持する。

```text
feat(persistence): add idempotent ingress queue
```

完了後、直ちにpushする。

---

### Commit 3: Runtime State永続化

対象:

- `CONTROL#RUNTIME`
- generation
- Runtime state transition
- wake開始、READY、BUSY、IDLE、STOPPING、STOPPED、DEGRADED
- 条件付き更新
- strongly consistent read

試験:

- 許可遷移、不正遷移拒否
- generation単調増加
- STARTING中の追加Request
- wake_started_at不要リセット防止
- stale generation拒否
- idle_since保持
- malformed record fail closed

```text
feat(persistence): add fenced runtime state record
```

完了後、直ちにpushする。

---

### Commit 4: Discord署名検証とHTTP変換境界

対象:

- Ed25519署名検証
- timestamp replay対策
- raw body処理
- header validation
- PING/PONG
- APPLICATION_COMMAND/MESSAGE_COMPONENT parse
- API Gateway eventから型付きinputへの変換
- 安全なエラー応答
- log redaction

試験:

- 正常/不正署名
- header欠落
- timestamp期限切れ
- raw body非改変
- PING、command、component
- unsupported interaction
- raw payload/signature/Interaction token非ログ

AWSリソースは追加しない。

```text
feat(discord): verify and parse HTTP interactions
```

完了後、直ちにpushする。

---

### Commit 5: Discord Ingress Use CaseとLambda Handler

対象:

- Guild/channel validation
- Ingress Request保存
- queue上限/duplicate応答
- Runtime wake要求
- Status Publisher要求
- ephemeral初回応答
- DiscordIngress Lambda handler
- AWS SDK port/adapter

試験:

- 保存後にscale-up要求
- 保存失敗時にscale-upしない
- scale-up失敗時もRequest保持
- duplicateでqueueを増やさない
- queue full
- Guild/channel拒否
- Lambda retry
- AWS Stubber
- secret非ログ

```text
feat(ingress): persist interactions before runtime wake
```

完了後、直ちにpushする。このpush後、Draft PRがなければ作成する。

---

### Commit 6: 公開Status Message Publisher

対象:

- 公開message作成
- status_channel_id/status_message_id保存
- STARTING、ACCEPTED、STARTUP_TIMEOUT、RECOVERED、TERMINAL_FAILED、REJECTED
- DiscordStatusPublisher Lambda
- 冪等なcreate/update

試験:

- duplicate invoke
- message ID保存
- 状態別本文
- 重複編集防止
- Discord一時失敗/retry
- message消失
- Bot token非ログ
- Interaction token不使用

```text
feat(discord): publish durable public request status
```

完了後、直ちにpushする。

---

### Commit 7: Interaction非依存のApplication入力

対象:

- 既存controllerから`discord.Interaction`依存を分離
- 新規議論、Retry、Cancelの型付きinput
- requester情報
- source message/thread情報
- 既存use case adapter

試験:

- application coreへDiscord objectを渡さない
- requester_id認可
- username/display name非認可
- operation ID冪等性
- accept/retry/cancel回帰

```text
refactor(application): decouple commands from discord interactions
```

完了後、直ちにpushする。

---

### Commit 8: Runtime Ingress Drainer

対象:

- FIFO Query、claim、claim期限
- PENDING/RETRYING処理
- slot不足時再待機
- accept/retry/cancel接続
- accepted Debate/Attempt ID保存
- crash後回収

試験:

- 全identity READY前drain禁止
- recovery前drain禁止
- FIFO
- duplicate debate防止
- slot不足、quota超過
- Retry、Cancel
- claim expiry、crash recovery
- lease/fencing維持

```text
feat(runtime): drain persisted interactions after recovery
```

完了後、直ちにpushする。

---

### Commit 9: 起動期限とRuntime Reconciler

対象:

- 1分Reconciler本体
- lost wake回復
- desiredCount=1への収束
- 3分timeout表示、復旧表示
- 15分terminal failure
- expired claim検出
- ECS state確認、ECS update port

試験:

- 2:59、3:00、3〜15分復旧、14:59、15:00
- 3分時点非terminal
- 15分後非実行
- pendingなのにdesiredCount=0
- duplicate invocation
- ECS API一時失敗
- DynamoDB成功/ECS失敗
- ECS成功/DynamoDB失敗
- 次回収束

fake Clockを使用する。

```text
feat(runtime): reconcile wake and startup deadlines
```

完了後、直ちにpushする。

---

### Commit 10: 完全終了、IDLE、Scale Down

対象:

- 完全終了判定
- Outbox SENT、application task、active lease確認
- IDLE、idle_since、stop_eligible_at
- 30分後STOPPING
- generationによる停止競合防止
- desiredCount=0要求、STOPPED収束

試験:

- Debate terminalだけではIDLEにしない
- Outbox SENT後IDLE
- FAILED/CANCELLED通知後
- 29:59、30:00
- 複数討論
- IDLE中の新規Request/Retry
- idle_since非リセット
- STOPPING直前Request
- UpdateService(0)直後Request
- stale generation、次回回復

```text
feat(runtime): scale down after thirty minutes idle
```

完了後、直ちにpushする。

---

### Commit 11: Graceful Shutdown統合

対象:

- admission close
- 新規claim停止
- checkpoint
- Outbox停止
- Discord client close
- bounded shutdown
- Runtime State更新
- SIGTERM統合

試験:

- shutdown中claim禁止
- lease無条件解放禁止
- stale worker write拒否
- timeout時安全終了
- RuntimeLifecycle回帰

```text
feat(runtime): integrate scale-down with graceful shutdown
```

完了後、直ちにpushする。

---

### Commit 12: CDKリソース

対象:

- HTTP API
- DiscordIngress/StatusPublisher/Reconciler Lambda
- 1分EventBridge
- Log Groups、IAM、Output
- ECS desiredCount=0
- FARGATE、CPU 512、Memory 1024

試験:

- FARGATE_SPOT不存在
- desiredCount、CPU、Memory
- Lambda VPC外
- NAT Gateway追加なし
- 新DynamoDB tableなし
- API route、IAM scope、secret非埋込み
- log retention、cdk-nag、synth

`cdk deploy`は禁止。`cdk.out`は既存方針で追跡対象でなければcommitしない。

```text
feat(infra): define on-demand scale-to-zero resources
```

完了後、直ちにpushする。

---

### Commit 13: Deploy GuardとGitHub Actions

対象:

- IDLE/STOPPED guard
- fail-closed判定
- break-glass入力と監査
- PR用CI
- production deployment job分離
- 既存Discord CI/PR通知統合

試験:

- STOPPED/IDLE許可
- STARTING/BUSY/STOPPING拒否
- DEGRADED条件
- break-glass、監査項目
- PR workflowがdeployしない
- notificationでsecret非露出

```text
ci: guard deployments and report pull request checks
```

完了後、直ちにpushする。

---

### Commit 14: 横断試験とRegression補強

統合後に判明した不足だけを対象とする。

- workstream間contract test
- local end-to-end相当試験
- DynamoDB Local統合
- Reconciler/Drainer競合
- RuntimeLifecycle、serializer migration、Outbox回帰
- CDK template静的検査
- security regression

実装不足をmockで隠さない。

```text
test: cover scale-to-zero integration and races
```

完了後、直ちにpushする。

---

### Commit 15: 設計文書とRunbook

対象:

- README、requirements、ADR
- basic/detailed design
- DynamoDB、Discord、RuntimeLifecycle、ECS/Fargate設計
- security、threat model
- operations/deployment/rollback/incident runbook
- cost、traceability

明記:

- AWS未デプロイ
- Discord Application未変更
- 既存CI/PR通知だけ許可
- 3分と15分の差
- 完全終了から30分
- FIFO 20件と既存3-slot
- Interaction token非保存
- rollout/rollback、デプロイ後確認

```text
docs: document HTTP ingress and scale-to-zero operations
```

完了後、直ちにpushする。

---

### Commit 16: 最終整合性修正

最終検査で発見した小規模修正だけに使用する。

- lint、type annotation、import
- 文書リンク、fixture、CDK assertion
- reviewの軽微指摘

大規模実装を`fix tests`や`misc changes`へまとめない。変更がなければ作成不要。

```text
fix: resolve final scale-to-zero validation findings
```

完了後、直ちにpushする。

---

## 4. Commit前後の必須確認

各commit前:

1. `git status --short`
2. `git diff --stat`
3. `git diff --check`
4. staged内容確認
5. 対象formatter
6. 対象lint
7. 対象type check
8. 対象test

無関係な変更、別workstreamの未完成変更をstageしない。必要に応じて部分stageを使う。

commit後:

1. commit作成確認
2. message確認
3. worktree確認
4. remote push確認
5. Draft PR反映確認

---

## 5. Push規則

各論理commitの直後にpushする。

禁止:

- 5個以上のcommitをlocalだけに保持
- 1時間以上、未commit変更だけを保持
- 大規模変更完了まで最初のpushを遅延
- push済みcommitを理由なくamend
- 再開性を損なうforce push
- サブエージェント成果を未確認で一括commit

push不能時は作業を無制限に続けず、現在commit hash、未push commit、未commit変更、阻害要因、次の安全な行動を記録する。

---

## 6. Draft PR進捗管理

最初の実質的commit後にDraft PRを作成する。PR本文へ次を置く。

```text
- [ ] 共通ModelとPort
- [ ] Ingress Request永続化
- [ ] Runtime State永続化
- [ ] Discord署名検証
- [ ] Ingress Lambda
- [ ] Status Publisher
- [ ] Application入力分離
- [ ] Runtime Ingress Drainer
- [ ] 3分・15分Reconciler
- [ ] 完全終了・30分IDLE
- [ ] Graceful Shutdown
- [ ] CDK
- [ ] Deploy Guard・GitHub Actions
- [ ] 横断試験
- [ ] 文書
- [ ] 全ローカル検証
- [ ] required CI
```

常に記載する:

- 完了済み単位
- 現在作業中
- 次に実施
- failing checks
- 未解決設計差異
- AWS未デプロイ
- Discord Application未変更

---

## 7. 中断直前のCheckpoint

継続困難時は可能な限り次を行う。

1. 現在変更確認
2. 安全に成立する範囲まで整理
3. 最小試験
4. checkpoint commit
5. push
6. Draft PRへ再開情報

通常の論理commitとして成立しない場合のみ許可:

```text
wip(checkpoint): preserve <workstream> progress
```

PRコメントへ残す:

- 完了済み/未完成部分
- failing test
- 次に編集するファイル
- 次に実行するコマンド
- 重要な設計判断
- 一時コード/TODO
- 最新commit hash

checkpointがbuild不能なら明示する。再開後は無理な履歴改変をせず後続commitで完成させてよい。

---

## 8. 再開時の手順

1. Draft PRを読む
2. 最新PRコメントを読む
3. remote branchをfetch
4. 最新commit hash確認
5. `git status`
6. `git log --oneline --decorate`
7. CI結果確認
8. checkpoint確認
9. 限定試験を再実行
10. 次の未完了単位から再開

push済みworkstreamを理由なく再実装しない。mainが進んでいる場合はbranch policyに従って取り込み、競合解消を追跡可能にする。

---

## 9. サブエージェント成果物

各サブエージェントは主担当へ次を返す。

- 担当範囲
- 変更ファイル
- 実装内容
- 実行した試験と結果
- 未解決事項
- 他workstreamへの影響
- 推奨commit境界

主担当が確認してからstage、commit、pushする。同一branchへ無秩序にcommit/pushしない。

---

## 10. CI運用

- 早いcheckは各pushで確認
- 重い統合試験はworkstream区切りで確認
- required checksはPR完成前に全成功
- notification failureとCI failureを分離
- CI失敗原因をPRへ記録
- retryだけで成功扱いにせず再現性確認
- flaky testを放置しない
- CIのためにsecurity checkを無効化しない
- deployment jobを実行しない

---

## 11. 最終全体検証

最低限:

- formatter、linter、type checker
- unit/async tests
- DynamoDB Local integration
- AWS Stubber tests
- Discord mock tests
- Runtime tests
- CDK tests、cdk-nag、cdk synth
- CloudFormation template静的検査
- package/build、docs、security checks

修正が必要なら責務に応じた追加commitを作成し、直ちにpushする。

---

## 12. Goal完了時のGit状態

- worktree clean
- 全commit remote push済み
- Draft PRまたはReview Ready PRが存在
- PR checklist最新
- required CI結果記録済み
- 未解決事項記載済み
- AWS未デプロイ
- Discord Application未変更
- 既存CI/PR通知結果記録済み
- 最新commit hashを最終報告へ記載

Completion criteriaをすべて満たした場合のみReview Readyへ変更する。
