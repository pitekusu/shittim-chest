---
aliases:
  - The Shittim Chest DynamoDB詳細設計
tags: [project, shittim-chest, dynamodb, data, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-07-16
---

# DynamoDB・データ整合性詳細設計

## 1. Table設定

- 単一table、on-demand、PK/SKはstring、PITR 35日、deletion protection有効、`RETAIN`とする。
- 全itemに`schema_version`、`created_at`、`updated_at`をUTCで保存する。
- debate本文とDiscord threadは自動期限なしで保存し、TTLを設定しない。「永久保存」は自動削除しない意味であり、過去状態への復旧保証はPITRの35日までとする。AWS Backupは採用しない。
- TTLは期限切れ補助recordだけに使用し、lease解放やsecurity処理へ依存しない。

## 2. Key設計

| 種別 | PK | SK |
|---|---|---|
| Debate meta | `DEBATE#<uuid7>` | `META` |
| Evidence | `DEBATE#<uuid7>` | `EVIDENCE#<seq>` |
| 初回意見 | `DEBATE#<uuid7>` | `INITIAL#<agent>` |
| 最終案 | `DEBATE#<uuid7>` | `FINAL#<agent>` |
| 投票 | `DEBATE#<uuid7>` | `VOTE#<agent>` |
| 決定事項 | `DEBATE#<uuid7>` | `DECISION` |
| outbox | `DEBATE#<uuid7>` | `OUTBOX#<operation-id>` |
| global slot | `CONTROL#GLOBAL` | `SLOT#0..2` |
| Guild quota | `QUOTA#GUILD#<guild-id>` | `DAY#<JST-YYYY-MM-DD>` |

## 3. GSI

### GSI1: thread lookup

META itemへ`gsi1pk=THREAD#<thread-id>`、`gsi1sk=DEBATE#<uuid7>`を設定する。Discord component受信時のsession検索に使う。

### GSI2: recoverable discovery

進行中METAへ`gsi2pk=RECOVERABLE`、`gsi2sk=<updated-at>#<debate-id>`を設定する。terminal状態への遷移で属性を削除する。GSIはeventual consistencyのため候補発見だけに使い、再開権限はbase tableのlease条件付き更新で確定する。

## 4. META必須属性

`debate_id`、Guild/channel/thread/message/user ID、question、phase、recovery state、winner、model/prompt/schema version、active elapsed、lease owner、lease expiry、fencing token、error codeを保存する。400KB制限へ近づけないようartifactを別itemへ分離する。

## 5. 受付transaction

`TransactWriteItems`で次を原子的に実行する。

1. 日次quotaが30未満であることを条件にincrement。
2. 期限切れまたは空いている3 slotの1つへowner、expiry、fencing tokenを設定。
3. METAを`ACCEPTED`で作成し、既存PKを拒否。

transaction cancel理由は`QUOTA_EXCEEDED`、`NO_SLOT_AVAILABLE`、`DUPLICATE_DEBATE`へ変換する。

## 6. Lease・fencing

- leaseは60秒、処理中は20秒ごとにrenewする。
- acquireごとにfencing tokenを単調incrementする。
- META自身のphase更新はownerとfencing tokenを`Update`のconditionにする。別itemのartifact保存、outbox作成・完了は`ConditionCheck(META)+Put/Update`を同じ`TransactWriteItems`へ含め、旧workerのcross-item writeを拒否する。
- `COMPLETED`、`FAILED`、`CANCELLED`へのterminal遷移とslot解放は同一transactionで行う。graceful process終了時に進行中slotを無条件解放せず、強制終了時はexpiry後に後続taskが取得する。

## 7. Outbox algorithm

1. operation ID、Bot ID、22文字nonce、content hash、thread ID、chunk sequence、status=`PREPARED`、attempt=`0`を`ConditionCheck(META)+Put`で保存する。
2. publisherは`PREPARED`またはclaim期限切れだけを条件付きclaimし、claim owner、claim expiry、attempt、next retryを保存してDiscordへ送信する。
3. 成功時は`ConditionCheck(META)+Update`でmessage ID、sent_at、status=`SENT`を保存する。
4. 送信成功・更新失敗時、または数分を超える停止後はnonce、content hash、chunk sequence、thread履歴を照合し、既存messageを採用する。
5. 内容hashが異なる同一operation IDは`OUTBOX_CONFLICT`として停止する。chunkはsequence昇順で送信し、前chunkが`SENT`になるまで次chunkをclaimしない。

## 8. boto3 adapter

- DynamoDB resource interfaceとnative Python型を優先する。
- transaction等resourceで不足する操作はresourceの`.meta.client`を使用する。
- typed service exceptionをadapterでdomain errorへ変換し、`ClientError`はtop-level境界だけで扱う。
- Queryは1MB paginationを考慮し、Scanを通常pathで使用しない。
- floatを保存せず、必要な数値はintまたは`Decimal`を使用する。

## 9. Schema migration

- readerは現行versionと直前versionを読めるようにする。
- writeは常に現行version。lazy migrationは条件付き更新で行う。
- destructive migrationはbackup/PITR確認、dry-run、item count、rollback手順をADRへ記録する。

## 10. 公式資料確認記録

| 確認日 | 対象version/service | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-07-16 | DynamoDB core | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.CoreComponents.html | PK/SK、item分割 |
| 2026-07-16 | Transactions | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transactions.html | quota、slot、METAの原子性 |
| 2026-07-16 | TransactWriteItems API | https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html | cross-item fencing `ConditionCheck` |
| 2026-07-16 | GSI | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GSI.html | discoveryと排他の分離 |
| 2026-07-16 | PITR | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Point-in-time-recovery.html | 35日restore |
| 2026-07-16 | boto3 DynamoDB | https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb.html | resource/client、pagination |
