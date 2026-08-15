---
aliases:
  - The Shittim Chest DynamoDB詳細設計
tags: [project, shittim-chest, dynamodb, data, detailed-design]
status: production-1.0
created: 2026-07-16
updated: 2026-08-14
---

# DynamoDB・データ整合性詳細設計

## 1. Table policy

- single table、on-demand、string PK／SK、deletion protection、`RETAIN`、PITR 35日とする。
- debate本文とDiscord thread mappingへTTLを設定しない。PITR期間と保存期間は別概念である。
- TTLは期限切れ補助recordだけに使い、lease解放やsecurity判断へ依存しない。
- itemは400 KB以下とする。大規模なdebate／Outbox transactionは100 action／4 MBをpreflightし、
  ingress等はbounded fieldとaction数で上限内に収める。

## 2. Schema version

current shared schemaは**v7**、読み込み可能なprevious schemaは**v6**である。readerは構造を
検証後にv6をv7へmemory上でup-convertする。v5以前、future version、unknown field／enumは
fail closedとする。record固有schemaは次のとおりである。

| Record | Current contract |
|---|---|
| Debate／Attempt／Output／Vote | shared schema v7 |
| Runtime control | manifest schema v2 |
| Outbox | record schema v2、v1 history read互換 |
| Status publication | record schema v3 |
| Generation checkpoint／Phase delivery | v7 record family |

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

exact PK／SK、attribute名、codecは`adapters/dynamodb`とcontract testを正とする。

## 4. Transaction invariants

- state更新はexpected schema、phase、Attempt ID、lease owner、fencing tokenをConditionCheckする。
- output保存とgeneration checkpoint completionを同一transactionにする。
- 全participant output保存後にPhaseDeliveryPlanとOutboxを1 transactionでstageする。
- 全必須Outboxが`SENT`のときだけplan deliveryと次phaseを同一transactionで確定する。
- vote 3件確定前に公開Outboxをstageしない。
- winner結果、terminal Outbox、activity counterを矛盾なく確定する。
- deployment lockがclosedならRuntime producer writeを条件式で拒否する。
- transaction cancellation reasonはrequest action順で分類し、本文をlogへ出さない。

## 5. Lease and fencing

- leaseはownerとgeneration／fencing tokenを持ち、期限内のownerだけがrenew／writeする。
- GSIはdiscoveryに使えるが、排他判断はbase tableのstrong readとtransaction conditionを使う。
- process停止時にleaseをbest-effort解放するが、正しさは期限とfenceに依存する。
- stale owner、別attempt、別generation、closed deployment lockからのwriteを拒否する。

## 6. Ordered Outbox

Outbox v2は`PENDING / CLAIMED / SENT / ABANDONED`を持つ。同attempt内で小さい
`delivery_sequence`がすべてterminalになるまで次をclaimできない。claim timeout後は再取得可能だが、
Discord history reconciliationで既送信を検出する。

nonretryable error、最大3 delivery attempt、stageから15分のdeadlineで新claimを止め、残件を
`ABANDONED`へ収束する。必須displayのabandonはattemptをFAILEDにし、欠落したままCOMPLETEDへ
進めない。

## 7. Query and pagination

- base tableの整合性判断は`ConsistentRead=true`を用いる。
- Query／Scanは`LastEvaluatedKey`が消えるまで処理し、上限到達を完全結果とみなさない。
- GSIのeventual consistencyをlockやwinner判断へ使わない。
- repositoryはnative Python valueをapplicationへ返し、SDK AttributeValueを漏らさない。

## 8. Privacy and telemetry

DynamoDBにはrecoveryに必要なquestion／outputを保存するが、CloudWatch logへ本文を複製しない。
token、signature、Interaction token、persona本文、OpenAI keyをitemへ入れない。operation count、phase、
stable error codeだけをmetricに用い、Debate ID等をdimensionにしない。

## 9. 公式資料確認記録

| 確認日 | 対象 | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-08-14 | DynamoDB core | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.CoreComponents.html | single-table item ownership |
| 2026-08-14 | Transactions | https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html | fencingとatomic phase update |
| 2026-08-14 | Query pagination | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Query.Pagination.html | complete pagination |
| 2026-08-14 | PITR | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Point-in-time-recovery.html | 35日restore boundary |
| 2026-08-14 | Item size | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/CapacityUnitCalculations.html | 400 KB validation |
