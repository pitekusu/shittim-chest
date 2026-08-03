---
aliases:
  - The Shittim Chest DynamoDB詳細設計
tags: [project, shittim-chest, dynamodb, data, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-07-28
---

# DynamoDB・データ整合性詳細設計

## 1. Table設定

- 単一table、on-demand、PK/SKはstring、PITR 35日、deletion protection有効、`RETAIN`とする。
- 全共有recordに`schema_version`を保存し、record種別に必要な`created_at`、`updated_at`はUTCとする。Scale-to-Zero実装後のcurrent shared schemaは`7`、直前versionは`6`とし、readerは構造検証後の`6 -> 7`だけをup-convertする。未知versionはfail closedとする。
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
| Ingress FIFO request | `CONTROL#INGRESS` | `REQUEST#<UTC-microseconds>#<interaction-id>` |
| Ingress active pointer | `CONTROL#INGRESS#ACTIVE` | requestと同じsort key |
| Interaction result | `INGRESS_OPERATION#<interaction-id>` | `RESULT` |
| component semantic binding | `INGRESS_SEMANTIC_OPERATION#<operation-id>` | `BINDING` |
| 公開Status publication | `INGRESS_OPERATION#<interaction-id>` | `STATUS_PUBLICATION` |
| Runtime wake result | `INGRESS_OPERATION#<interaction-id>` | `RUNTIME_WAKE` |
| Runtime state | `CONTROL#RUNTIME` | `STATE` |
| activity manifest marker | `CONTROL#RUNTIME` | `ACTIVITY_SCHEMA` |
| Ingress/status/panel/outbox/debate counter | 各`CONTROL#...` | 固定sort key |
| deployment lock | `CONTROL#DEPLOYMENT` | `LOCK` |
| deployment audit | `CONTROL#DEPLOYMENT#AUDIT#<guard-id>` | `ACQUIRE` / `RELEASE` |

## 3. GSI

### GSI1: thread lookup

Debate META itemへ`gsi1pk=THREAD#<thread-id>`、`gsi1sk=DEBATE#<uuid7>`を設定する。Discord component受信時のsession検索に使う。

### GSI2: recoverable discovery

進行中Attempt METAへ`gsi2pk=RECOVERABLE`、`gsi2sk=<updated-at>#<debate-id>#<attempt-id>`を設定する。terminal状態への遷移で属性を削除する。GSIはeventual consistencyのため候補発見だけに使い、再開権限はbase tableのlease条件付き更新で確定する。

## 4. META必須属性

Debate METAは`debate_id`、Guild/channel、starter message ID、thread ID、control panel message ID、`requester_id`（Discord user ID）、受付時点の`requester_username`と`requester_display_name`、question、`current_attempt_id`、schema versionを保存する。username/display nameは将来の認証済みWebアーカイブ表示・検索用の不変snapshotであり、認可・PK/SK/GSI/lease/fencingには使わない。Attempt METAやartifactへは重複保存しない。Attempt METAは`attempt_id`、`retry_of`、phase、`failed_from_phase`、recovery state、winner、model/prompt/schema version、active elapsed、lease owner、lease expiry、fencing token、error codeを保存する。400KB制限へ近づけないようartifactを別itemへ分離する。

### 4.1 Scale-to-Zero補助record

- Ingress Requestはquestion、requester snapshot、Guild/channel、command/component context、`PENDING/CLAIMED/RETRYING/ACCEPTED/COMPLETED/REJECTED/FAILED`、受付から3分/15分のdeadline、claim owner/expiry/delivery attempt、accepted debate/attempt IDを持つ。Interaction tokenは属性に含めない。
- active pointerはPIIを持たず、最大20件の非terminal requestをstrongly consistentなFIFO Queryで列挙する。terminal遷移と同じtransactionでpointer削除とcounter減算を行う。
- operation resultはInteraction IDごとの冪等結果、semantic bindingはRetry/Cancelの決定的operation IDを最初のcanonical Interaction IDへ結び付ける。どちらもstrongly consistent readを正本とする。
- Status publicationはdesired/delivered state、moderator nonce、content hash、message ID、claim/retry、履歴照合checkpointを持ち、Lambdaから冪等に公開messageを作成/更新する。Interaction tokenは使用しない。
- Runtime Stateは`STOPPED/STARTING/READY/BUSY/IDLE/STOPPING/DEGRADED`、generation、desired count、runtime instance ID、固定した`idle_since`/`stop_eligible_at`を持つ。Debate/lease/Outboxの代替正本にはしない。
- deployment lockはowner、UUIDv7 guard ID、fencing token、version、expiry、normal/break-glass modeを持つ。ACQUIRE/RELEASE auditはcommit SHA、actor、run ID、安定codeなどのcontent-free属性だけをimmutableに保存する。

### 4.2 固定control-record manifest v2

deployment toolingは次の11 recordをSHA-256で固定したmanifest v2として一度の`TransactWriteItems`でinstallする。

1. `CONTROL#RUNTIME / ACTIVITY_SCHEMA` manifest marker
2. Ingress queue counter
3. Status pending counter
4. Panel refresh pending counter
5. Outbox pending/claimed counter
6. Active attempt counter
7. global lease `SLOT#0`
8. global lease `SLOT#1`
9. global lease `SLOT#2`
10. Runtime State
11. deployment lock

markerがある場合は11 recordの完全性、current schema、未知属性、manifest hashを全件検証し、欠落・破損・旧schemaを自動repairしない。manifest v1は実AWSへdeployされていないが、v1 markerが存在する状態はv2へsilent migrationせずfail closedとする。markerのない旧tableはstrong readと最大4 page/400 itemの制限付きScanでactive workがないことを確認し、不活性で構造的に正しいv6固定recordだけをv7へ条件付き置換する。範囲超過、active work、未知形式はwriteせず、停止・バックアップ・dry-run付きのoffline migrationを必須とする。

## 5. HTTP IngressとDebate受付transaction

DiscordIngress Lambdaの受付は、deployment lockが`OPEN`であることを同じtransactionで検証し、Ingress queue counterが20未満の場合だけcounterとStatus pending counterをincrementし、Ingress Request、active pointer、Interaction operation result、Status publicationを条件付きPutする。Retry/Cancelはさらにsemantic operation bindingを同じtransactionでPutする。duplicateは新規item/counterを増やず、operation resultとrequest/status bundleの完全一致をstrongly consistent readで確認して再生する。

Request永続化後のRuntime wakeは別transactionとし、Runtime Stateのgeneration/desired count更新と`RUNTIME_WAKE`結果を条件付きで結び付ける。ECS/Lambda API呼出しはDynamoDB transactionの外でbest-effortに行い、失敗してもReconcilerが永続Requestから回復する。

ECS RuntimeのIngress Drainerは、正当なIngress claim owner・claim expiry・delivery attemptを`IngressClaimFence`として以下の既存Debate受付transactionへ渡す。HTTP受付とDebate/global slot受付は別段階であり、global slot不足はIngressをterminal化せず`RETRYING`へ戻す。

`TransactWriteItems`で次を原子的に実行する。

1. 日次quotaが30未満であることを条件にincrement。
2. 期限切れまたは空いている3 slotの1つへowner、expiry、fencing tokenを設定。
3. Debate METAをcurrent attempt付きで作成し、既存PKを拒否。
4. 初回Attempt METAを`ACCEPTED`、`retry_of=null`で作成する。
5. operation resultを専用itemへ条件付き作成し、debate/attempt/request bindingを保存する。HTTP由来の操作では同じtransactionに正確なlive `IngressClaimFence`のConditionCheckを含める。

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

deployment lockはDebate leaseと別のproduction deploy fenceである。guardは11固定recordを1回の`TransactGetItems`で読み、通常deployはRuntime `STOPPED`/`IDLE`かつ全activity counter 0、break-glassは明示mode/reasonがある場合だけ許可する。どちらも既存lockを上書きできない。acquireは読み取ったRuntime、counter、slot、lockを同じtransactionで条件照合し、owner・UUIDv7 guard ID・新fencing token付きlockとimmutable ACQUIRE auditを同時に作る。Ingress enqueue、Runtime wake、IDLE stopを含むactivity変更は同一transactionでlock `OPEN`をConditionCheckし、acquire/write raceは一方だけ成立させる。

`lock_expires_at`は監視・人手復旧判定用であり、期限到達でlockを自動開放・reclaimしない。releaseは取得時と完全一致するowner、guard ID、fencing tokenを指定し、lockを`OPEN`にするwriteとimmutable RELEASE auditを同じtransactionで行う。同じreleaseの再送は監査recordで冪等に返し、stale owner/guard/fenceは後続lockを開けない。汎用force-unlock API、期限切れlockの自動回収、別ownerによる強制解除は実装しない。回復不能な場合はproductionをfail closedのままとし、監査付きoffline recoveryを別Runbookで扱う。

## 8. Outbox algorithm

1. operation ID、generic Bot slot、22文字nonce、content hash、thread ID、chunk sequence、status=`PREPARED`、attempt=`0`を`ConditionCheck(META)+Put`で保存する。
2. publisherは`PREPARED`またはclaim期限切れだけを条件付きclaimし、claim owner、claim expiry、attempt、next retryを保存してDiscordへ送信する。claimはapplication共有定数`OUTBOX_CLAIM_SECONDS=60`を唯一の定義とし、Discord channel解決、履歴照合、sendの45秒timeoutより長くする。
3. 成功時は`ConditionCheck(META)+Update`でmessage ID、sent_at、status=`SENT`を保存する。
4. 送信成功・更新失敗時、または数分を超える停止後はnonce、content hash、chunk sequence、thread履歴を照合し、既存messageを採用する。
5. 内容hashが異なる同一operation IDは`OUTBOX_CONFLICT`として停止する。chunkはsequence昇順で送信し、前chunkが`SENT`になるまで次chunkをclaimしない。
6. recovery readerはattempt partitionをstrongly consistentかつ1 MB pagination付きでQueryし、`SENT`以外を全件返す。未来の`next_retry_at`と未失効`claim_expiry`も除外せず、adapter側owned taskが利用可能時刻まで待つ。GSI、Scan、process memoryだけをpending検出の正本にしない。

## 9. boto3 adapter

- serializerまではnative Python型を唯一のrecord表現とし、SDK境界で`TypeSerializer`/`TypeDeserializer`によりAttributeValueへ明示変換する。floatと非整数Decimalは拒否する。
- 複数item transactionが主要write pathであるため、STEP-04Bは1個の低level `DynamoDBClient`を再利用し、`GetItem`、paginated `Query`、`TransactWriteItems`を同じ型付き境界へ集約する。
- typed service exceptionをadapterでdomain errorへ変換し、`ClientError`はtop-level境界だけで扱う。
- Queryは1MB paginationを考慮し、Scanを通常pathで使用しない。
- `list_pending`は将来retryと現在claim中を含む未送信全件を返す。送信可否はfenced `claim` transactionが最終判定し、reader結果だけでownershipを判断しない。
- terminal finalizationの`TransactWriteItems`はrequest actionと同じ順序でcontent-freeなaction kindを保持する。`TransactionCanceledException.CancellationReasons`が全action分揃う場合だけordered indexからattempt CAS、outbox SENT check、slot release、active-attempt counter、panel-refresh counterを分類する。欠落、長さ不一致、不明codeは`unknown`としてfail closedにし、provider message、item、condition値はlogへ出さない。完全な`TransactionConflict`またはattempt CASの`ConditionalCheckFailed`だけを同一lease内のbounded retry対象とし、outbox/counter/fencing条件失敗はhot retryしない。
- floatを保存せず、必要な数値はintまたは`Decimal`を使用する。

STEP-04Aはboto3非依存のnative-value itemとschema検証を提供する。STEP-04Bはboto3 adapter、transaction、lease/fencing、outboxを実装した。STEP-05BはEvidenceを追加してschema v3へ更新した。STEP-05Cは`escalation_assessment` itemへrules version、3つのsignal、UTC評価時刻、再実行開始phase、実行有無、Policy ID、最大1回の実行回数を保存しschema v4へ更新した。STEP-06Aはcontrol panel message IDをstarter message IDから分離し、outboxの実Application ID依存をgeneric Bot slotへ置換してschema v5へ更新した。readerは直前v4の欠落panel IDを`None`、旧`bot_id`を対応するgeneric slotとしてup-convertした。STEP-06DはDebate METAへ受付時点の`requester_username`と`requester_display_name`を追加してschema v6へ更新した。v5 debate_metaは実Discord名を復元せず、決定的legacy fallbackとして両フィールドへ`requester_id`を入れる。v4以前の直接読込はfail closedとする。STEP-06Bはschema変更なしで、`DiscordOutboxRepository`の`get/claim/reschedule/mark_sent`をdiscord.py publisherから利用する。Discord処理はclaimより短い45秒で打ち切るが、`mark_sent`はtimeout外のfenced transactionとして行う。送信成功後の`mark_sent`競合ではmessageを再送せず、claim expiry後の次回claimでthread履歴を照合する。STEP-07Bはschema変更なしで`list_pending`を追加し、未来retryと未失効claimを含む未送信全件をlease heartbeat配下のdrainerへ渡す。

## 10. Schema migration

- readerは現行versionと直前versionを読めるようにし、旧recordを現行domain modelへup-convertする。現行は`6`、直前は`5`である。
- v5→v6では`debate_meta`へ`requester_username`と`requester_display_name`を追加する。欠落時は実名復元ではなく`requester_id`を非空fallbackとして設定する。他record typeは内容を変えず`schema_version`だけを`6`へ上げる。
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
| 2026-07-17 | Query API | https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_Query.html | base tableの`ConsistentRead=true`、1 MB `LastEvaluatedKey` pagination、filter前readを再確認しattempt PK/SK Queryを採用 |
| 2026-08-03 | TransactWriteItems API | https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html | `CancellationReasons`がrequest item順である契約をterminal finalizationのcontent-free action分類へ使用 |
