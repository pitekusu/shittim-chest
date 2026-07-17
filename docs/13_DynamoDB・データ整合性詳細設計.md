---
aliases:
  - The Shittim Chest DynamoDB詳細設計
tags: [project, shittim-chest, dynamodb, data, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-07-17
---

# DynamoDB・データ整合性詳細設計

## 1. Table設定

- 単一table、on-demand、PK/SKはstring、PITR 35日、deletion protection有効、`RETAIN`とする。
- 全itemに`schema_version`、`created_at`、`updated_at`をUTCで保存する。STEP-04Aのcurrent record schemaは`2`とし、readerはschema `1`を構造検証後に`2`へup-convertする。未知versionはfail closedとする。
- debate本文とDiscord threadは自動期限なしで保存し、TTLを設定しない。「永久保存」は自動削除しない意味であり、過去状態への復旧保証はPITRの35日までとする。AWS Backupは採用しない。
- TTLは期限切れ補助recordだけに使用し、lease解放やsecurity処理へ依存しない。

## 2. Key設計

| 種別 | PK | SK |
|---|---|---|
| Debate meta | `DEBATE#<uuid7>` | `META` |
| Attempt meta | `DEBATE#<uuid7>` | `ATTEMPT#<attempt-uuid7>#META` |
| Evidence | `DEBATE#<uuid7>` | `ATTEMPT#<attempt-uuid7>#EVIDENCE#<seq>` |
| 初回意見 | `DEBATE#<uuid7>` | `ATTEMPT#<attempt-uuid7>#INITIAL#<agent>` |
| 最終案 | `DEBATE#<uuid7>` | `ATTEMPT#<attempt-uuid7>#FINAL#<agent>` |
| 投票 | `DEBATE#<uuid7>` | `ATTEMPT#<attempt-uuid7>#VOTE#<agent>` |
| 決定事項 | `DEBATE#<uuid7>` | `ATTEMPT#<attempt-uuid7>#DECISION` |
| outbox | `DEBATE#<uuid7>` | `ATTEMPT#<attempt-uuid7>#OUTBOX#<operation-id>` |
| operation result | `OPERATION#<operation-id>` | `RESULT` |
| global slot | `CONTROL#GLOBAL` | `SLOT#0..2` |
| Guild quota | `QUOTA#GUILD#<guild-id>` | `DAY#<JST-YYYY-MM-DD>` |

## 3. GSI

### GSI1: thread lookup

Debate META itemへ`gsi1pk=THREAD#<thread-id>`、`gsi1sk=DEBATE#<uuid7>`を設定する。Discord component受信時のsession検索に使う。

### GSI2: recoverable discovery

進行中Attempt METAへ`gsi2pk=RECOVERABLE`、`gsi2sk=<updated-at>#<debate-id>#<attempt-id>`を設定する。terminal状態への遷移で属性を削除する。GSIはeventual consistencyのため候補発見だけに使い、再開権限はbase tableのlease条件付き更新で確定する。

## 4. META必須属性

Debate METAは`debate_id`、Guild/channel/thread/message/user ID、question、`current_attempt_id`、schema versionを保存する。Attempt METAは`attempt_id`、`retry_of`、phase、`failed_from_phase`、recovery state、winner、model/prompt/schema version、active elapsed、lease owner、lease expiry、fencing token、error codeを保存する。400KB制限へ近づけないようartifactを別itemへ分離する。

## 5. 受付transaction

`TransactWriteItems`で次を原子的に実行する。

1. 日次quotaが30未満であることを条件にincrement。
2. 期限切れまたは空いている3 slotの1つへowner、expiry、fencing tokenを設定。
3. Debate METAをcurrent attempt付きで作成し、既存PKを拒否。
4. 初回Attempt METAを`ACCEPTED`、`retry_of=null`で作成する。
5. operation resultを専用itemへ条件付き作成し、debate/attempt/request bindingを保存する。

transaction cancel理由は`QUOTA_EXCEEDED`、`NO_SLOT_AVAILABLE`、`DUPLICATE_DEBATE`へ変換する。
operation resultはoperation IDからstrongly consistent `GetItem`できる専用keyとし、eventually consistent GSIや`ClientRequestToken`の10分だけへ冪等性を依存しない。SDK tokenにはtable、operation、aggregate、slot/fencingを含む入力のhashを使い、同一AWS account内の別tableや別transactionとの衝突を防ぐ。

## 6. Retry transaction

FAILED retryは事前のstrongly consistent read後、1つの成功した`TransactWriteItems`で更新部分を原子的に実行する。

1. 事前にoperation resultをstrongly consistent readし、完了済みなら保存済みnew attempt IDを返す。
2. Debate METAの`current_attempt_id`が対象FAILED attemptと一致することを確認する。
3. 対象Attempt METAが`FAILED`かつ`failed_from_phase`を持つことを確認する。
4. operation IDが未処理であることを条件に、あらかじめ生成したnew attempt IDとともに専用result itemへ記録する。
5. 期限切れまたは空きglobal slotを1つ取得し、新fencing tokenを割り当てる。
6. 同じdebate ID、同じthread、`retry_of=<failed-attempt-id>`、new attempt ID、phase=`failed_from_phase`のAttempt METAを`attribute_not_exists(PK) AND attribute_not_exists(SK)`条件付きでPutする。
7. Debate METAの`current_attempt_id`を条件付きでnew attemptへ更新する。

Guild日次quota itemは読み書きしない。空きslotがなければbusy responseとする。並行transactionがoperation ID条件で負けた場合はoperation recordをstrongly consistent readし、入力debate/attemptと一致する保存済みnew attempt IDだけを返す。一致しなければconflictとし、古いFAILED attemptからの分岐、attempt ID再利用、operation ID replayで複数attemptを作らない。

## 7. Lease・fencing

- leaseは60秒、処理中は20秒ごとにrenewする。
- acquireごとにfencing tokenを単調incrementする。
- Attempt META自身のphase更新はownerとfencing tokenを`Update`のconditionにする。別itemのartifact保存、outbox作成・完了はDebate METAのcurrent attemptとAttempt METAのowner/fencingを確認する`ConditionCheck`を同じ`TransactWriteItems`へ含め、旧workerのcross-item writeを拒否する。
- `COMPLETED`、`FAILED`、`CANCELLED`へのterminal遷移とslot解放は同一transactionで行う。graceful process終了時に進行中slotを無条件解放せず、強制終了時はexpiry後に後続taskが取得する。

## 8. Outbox algorithm

1. operation ID、Bot ID、22文字nonce、content hash、thread ID、chunk sequence、status=`PREPARED`、attempt=`0`を`ConditionCheck(META)+Put`で保存する。
2. publisherは`PREPARED`またはclaim期限切れだけを条件付きclaimし、claim owner、claim expiry、attempt、next retryを保存してDiscordへ送信する。
3. 成功時は`ConditionCheck(META)+Update`でmessage ID、sent_at、status=`SENT`を保存する。
4. 送信成功・更新失敗時、または数分を超える停止後はnonce、content hash、chunk sequence、thread履歴を照合し、既存messageを採用する。
5. 内容hashが異なる同一operation IDは`OUTBOX_CONFLICT`として停止する。chunkはsequence昇順で送信し、前chunkが`SENT`になるまで次chunkをclaimしない。

## 9. boto3 adapter

- serializerまではnative Python型を唯一のrecord表現とし、SDK境界で`TypeSerializer`/`TypeDeserializer`によりAttributeValueへ明示変換する。floatと非整数Decimalは拒否する。
- 複数item transactionが主要write pathであるため、STEP-04Bは1個の低level `DynamoDBClient`を再利用し、`GetItem`、paginated `Query`、`TransactWriteItems`を同じ型付き境界へ集約する。
- typed service exceptionをadapterでdomain errorへ変換し、`ClientError`はtop-level境界だけで扱う。
- Queryは1MB paginationを考慮し、Scanを通常pathで使用しない。
- floatを保存せず、必要な数値はintまたは`Decimal`を使用する。

STEP-04Aはboto3非依存のnative-value itemとschema検証を提供する。STEP-04Bはboto3 1.43.50、明示AttributeValue変換、typed transaction error mapping、`asyncio.to_thread`隔離、強整合read、1MB pagination、3-slot fencing、20秒heartbeat、outbox状態更新を実装した。phase更新はAttempt METAのlease属性を上書きしない条件付き`Update`とし、並行renew後のexpiryを古いsnapshotで巻き戻さない。DynamoDB Local 3.3.0でtransaction/GSI/outboxを、SDK StubberでLocalが再現しないtransaction cancelを検証する。

## 10. Schema migration

- readerは現行versionと直前versionを読めるようにし、旧recordを現行domain modelへup-convertする。
- writeは常に現行version。state-changing use case、特に新attempt retryの前に、必要なlazy migrationをexpected旧version条件付きで完了する。migration不能、競合、未対応versionはfail closedとし、旧`schema_version`を継承したnew itemを作らない。
- destructive migrationはbackup/PITR確認、dry-run、item count、rollback手順をADRへ記録する。

## 11. 公式資料確認記録

| 確認日 | 対象version/service | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-07-16 | DynamoDB core | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.CoreComponents.html | PK/SK、item分割 |
| 2026-07-16 | Transactions | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transactions.html | quota、slot、METAの原子性 |
| 2026-07-16 | TransactWriteItems API | https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html | cross-item fencing `ConditionCheck` |
| 2026-07-16 | GSI | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GSI.html | discoveryと排他の分離 |
| 2026-07-16 | PITR | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Point-in-time-recovery.html | 35日restore |
| 2026-07-16 | boto3 DynamoDB | https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb.html | resource/client、pagination |
| 2026-07-17 | DynamoDB data types・400KB | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.NamingRulesDataTypes.html | native value型、UTF-8、item事前上限検査 |
| 2026-07-17 | Item size calculation | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/CapacityUnitCalculations.html | 属性名と値を含む400KB境界をcontract test化 |
| 2026-07-17 | TransactWriteItems API | https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html | 同一item複数action禁止、10分tokenだけへ冪等性を依存しない |
| 2026-07-17 | DynamoDB Local差異 | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocal.UsageNotes.html | Localで再現しないtransaction conflictはSTEP-04BのSDK stub testへ分離 |
| 2026-07-17 | boto3/boto3-stubs 1.43.50 | https://pypi.org/project/boto3/、https://pypi.org/project/boto3-stubs/ | Python 3.14対応、client/型定義をlock |
| 2026-07-17 | Query API・pagination | https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_Query.html、https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Query.Pagination.html | 1MBごとのLastEvaluatedKey処理、base tableだけstrong consistency |
| 2026-07-17 | DynamoDB Local 3.3.0 | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocal.DownloadingAndRunning.html、https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocalHistory.html | 公式imageをdigest固定しCI persistence testへ使用 |
