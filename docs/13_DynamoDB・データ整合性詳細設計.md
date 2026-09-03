---
aliases:
  - The Shittim Chest DynamoDB詳細設計
tags: [project, shittim-chest, dynamodb, data, detailed-design]
status: production-1.0
created: 2026-07-16
updated: 2026-09-03
---

# DynamoDB・データ整合性詳細設計

## 1. Table policy

- single table、on-demand、string PK／SK、deletion protection、`RETAIN`、PITR 35日とする。
- debate本文とDiscord thread mappingへTTLを設定しない。PITR期間と保存期間は別概念である。
- TTLは期限切れ補助recordだけに使い、lease解放やsecurity判断へ依存しない。
- itemは400 KB以下とする。大規模なdebate／Outbox transactionは100 action／4 MBをpreflightし、
  ingress等はbounded fieldとaction数で上限内に収める。

## 2. Schema version

current shared schemaは**v9**、読み込み可能なprevious schemaは**v8**である。readerは構造を
検証後にv8をv9へmemory上でup-convertする。v7以前、future version、unknown field／enumは
fail closedとする。record固有schemaは次のとおりである。

固定Runtime control 11件はRelease境界でも全件が同じshared schemaであることを要求する。
current v9またはprevious v8の完全なmanifestだけをread-only preflightで受理し、混在、欠落、
marker hash不一致はfail closedとする。v8からのReleaseでは、10件のcontrol更新、v9のclosed
deployment lock、immutable acquire auditを1 transactionで書き、v8/openまたはv9/closed以外の
途中状態を公開しない。

| Record | Current contract |
|---|---|
| Debate／Attempt／Output／Vote／Affection | shared schema v9 |
| Runtime control | manifest schema v2 |
| Outbox | record schema v2、v1 history read互換 |
| Status publication | record schema v3 |
| Generation checkpoint／Phase delivery | v9 record family |

旧migrationの作業履歴はGit historyを正とし、本書へ累積しない。

## 3. Partition families

| Family | Purpose |
|---|---|
| Debate／Attempt | question、phase、retry relation、terminal result |
| Output／Vote | participant generation、anonymous ballot、winner input |
| GenerationCheckpoint | `PLANNED / IN_FLIGHT / COMPLETED / FAILED` |
| PhaseDeliveryPlan | `STAGED / TERMINATING / DELIVERED / ABANDONED` |
| Outbox | Discord operation、sequence、nonce、claim、delivery state |
| Ingress | durable FIFO、Interaction deduplication、startup deadline |
| Runtime state／activity | generation、fence、lease、pending／claimed count |
| Status publication | channel Statusのdesired／observed state |
| Deployment guard／lock | Release前提、owner、TTL、stack fence |
| `ADMIN#PROMPT` | current revision、content-free revision audit、hashed idempotency state |
| `AFFECTION#REQUESTER#<opaque requester key> / PROFILE` | 3人の0〜1,000点、CAS version、reset回数、cycle、解放状態 |
| `DEBATE#<Debate ID> / AFFECTION` | 評価状態、変更前、質問評価、実増減、変更後、今回のメモリアル解放 |
| `MEMORIAL#REQUESTER#<opaque requester key> / CYCLE#<cycle>` | owner別のupload予約、生成checkpoint、画像参照、思い出文、reset metadata |

exact PK／SK、attribute名、codecは`adapters/dynamodb`とcontract testを正とする。

## 4. Transaction invariants

- state更新はexpected schema、phase、Attempt ID、lease owner、fencing tokenをConditionCheckする。
- output保存とgeneration checkpoint completionを同一transactionにする。
- 全participant output保存後にPhaseDeliveryPlanとOutboxを1 transactionでstageする。
- 全必須Outboxが`SENT`のときだけplan deliveryと次phaseを同一transactionで確定する。
- vote 3件確定前に公開Outboxをstageしない。
- winner結果、terminal Outbox、activity counterを矛盾なく確定する。
- deployment lockがclosedならRuntime producer writeを条件式で拒否する。
- v7 controlを移行したReleaseでは、lock取得直後の11件をcanonical化したcontent-free SHA-256を
  immutable acquire auditへ保存する。candidate Runtimeが有効にならなかった場合は、rollback直前の
  11件がこの取得時fingerprintと一致し、各recordのexact conditionも満たす場合だけ、open lockとともに
  1 transactionでv7へ戻す。不一致またはcandidateの適用状態が曖昧な場合はrollbackもlock解放も行わず、
  fail closedのままoperator判断へ委ねる。
- transaction cancellation reasonはrequest action順で分類し、本文をlogへ出さない。
- prompt publish／rollbackはpending audit、idempotency、`CURRENT`をtransactionで整合させる。SSMの
  active pointer切替後にaudit完了が失敗した場合は、次回読込でmanifestとpointerを検証して回復する。
- 親愛度は3人のprofile更新、討論評価、`scoring_affection`から次phaseへの遷移を同一transactionで
  反映する。評価は3件すべてが成功した場合だけ全員へ適用し、同じ討論の再試行では二重加算しない。
  profile CAS競合は同じ評価値で再計算し、OpenAIを再実行しない。
- v8のraw-ID profileは本人の次回成功評価時だけopaque keyのv9 profileへPutし、旧profileを同一transactionで
  Deleteする。その成功評価では移行前から1,000点の人格も解放候補へ含める。評価不能時は旧rowを
  read／condition-checkするだけで移行しない。新規v9 profileへraw IDを保存しない。
- 未解放cycleで1人以上が新たに1,000点へ達した場合、またはv8移行時に既存1,000点がある場合は、
  選出した1人の解放metadataをprofileと討論評価へ
  同じtransactionで保存する。通常到達かv8遡及解放かをcontent-freeなbooleanで保持し、通常v9の
  到達条件を緩めない。既に解放済みのcycleでは後続到達を無視する。
- メモリアル生成はStatisticsのowner partitionへcycle checkpointとhashed idempotency stateを保存する。
  upload予約、queue投入、resetの全POSTはrequestの`expectedCycle`とsource profileのcurrent cycleを
  transaction conditionで一致させ、stale requestを409でfenceする。DynamoDB expressionでは`cycle`を
  `#cycle`でaliasし、LocalとAWSで同じtransaction contractを使う。
- queue投入済みの`queued`は不変のresult asset keyを保持したままSQSへ再送でき、APIとSQSの間の
  crashでjobを失わない。worker claim、生成完了／失敗はowner、cycle、stateを条件付き更新し、
  claimごとにcheckpointのgeneration attemptをCAS incrementする。paid generationは3回を上限とし、Standard SQSの
  物理receive countを正本にしない。counterはfailed後のupload予約更新とqueue再投入でも引き継ぐ。
  SQS再配送で画像や文章を二重生成・上書きしない。上限後は
  paid callを禁止し、検証済み最終画像と文章が残る場合だけ同じcycle／result asset keyの
  completion-only回復を許可する。最終画像がない文章だけのpartial checkpointはterminal化時に除去する。
  生成物を公開できるのは画像参照と文章が
  揃った`ready`だけであり、partial outputはowner APIへ返さない。
- resetはsource v9の`AFFECTION#REQUESTER#... / PROFILE`とStatisticsのcycle状態を同一transactionで
  fenceする。解放済みcycleはcheckpointがない`unlocked`、回復可能なpartial outputがない終端の`failed`、
  または`ready`で
  resetでき、`queued`または`generating`は409で拒否する。初回の`locked`は未解放のためresetできず、
  文章と検証済み最終画像を持つ`failed`はcompletion-only回復が必要なためreset／上書きを409で拒否する。
  reset完了後の次cycleが`locked`の場合は同じidempotency keyによる完了済みresetの再試行だけを
  受理する。次cycleが再解放済みなら過去cycleのreset再送は409とする。3スコアを500へ戻し、
  reset回数とmemorial cycleを各1増やし、解放metadataを除去する。
  source profileだけ、またはcheckpointだけが進む状態や二重加算を許可しない。

## 5. Lease and fencing

- leaseはownerとgeneration／fencing tokenを持ち、期限内のownerだけがrenew／writeする。
- GSIはdiscoveryに使えるが、排他判断はbase tableのstrong readとtransaction conditionを使う。
- process停止時にleaseをbest-effort解放するが、正しさは期限とfenceに依存する。
- stale owner、別attempt、別generation、closed deployment lockからのwriteを拒否する。

## 6. Ordered Outbox

Outbox v2は`PENDING / CLAIMED / SENT / ABANDONED`を持つ。同attempt内で小さい
`delivery_sequence`がすべてterminalになるまで次をclaimできない。claim timeout後は再取得可能だが、
Discord history reconciliationで既送信を検出する。

Discordが受理したmessage IDの`SENT`確定は、同じmessage ID、claim、確定時刻から同一の
idempotency tokenを導出する。完全な`TransactionConflict`またはSDK呼出結果不明だけを短時間で
bounded retryし、条件不一致やidentity conflictは再試行しない。DynamoDB SDK例外はadapter境界で
`RepositoryUnavailable`へ変換し、provider messageをapplication／logへ漏らさない。

nonretryable error、最大3 delivery attempt、stageから15分のdeadlineで新claimを止め、残件を
`ABANDONED`へ収束する。必須displayのabandonはattemptをFAILEDにし、欠落したままCOMPLETEDへ
進めない。

## 7. Query and pagination

- base tableの整合性判断は`ConsistentRead=true`を用いる。
- Query／Scanは`LastEvaluatedKey`が消えるまで処理し、上限到達を完全結果とみなさない。
- GSIのeventual consistencyをlockやwinner判断へ使わない。
- メモリアルの直近質問はArchive GSI3を降順で最大10件だけ読む。生成可否、owner認可、cycleの正しさは
  GSI結果へ依存せず、source profileとStatistics checkpointの条件付きbase-table操作で決める。
- repositoryはnative Python valueをapplicationへ返し、SDK AttributeValueを漏らさない。

## 8. Privacy and telemetry

DynamoDBにはrecoveryに必要なquestion／outputを保存するが、CloudWatch logへ本文を複製しない。
token、signature、Interaction token、persona本文、OpenAI keyをitemへ入れない。operation count、phase、
stable error codeだけをmetricに用い、Debate ID等をdimensionにしない。

prompt auditにはrevision、時刻、操作、base／source revision、aggregate／request checksum、
pending／completed状態だけを保存する。prompt本文、差分、idempotency keyそのものは保存しない。

## 9. 公式資料確認記録

| 確認日 | 対象 | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-08-14 | DynamoDB core | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.CoreComponents.html | single-table item ownership |
| 2026-08-14 | Transactions | https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html | fencingとatomic phase update |
| 2026-08-14 | Query pagination | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Query.Pagination.html | complete pagination |
| 2026-08-14 | PITR | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Point-in-time-recovery.html | 35日restore boundary |
| 2026-08-14 | Item size | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/CapacityUnitCalculations.html | 400 KB validation |
