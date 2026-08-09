---
aliases:
  - Discord討論過程表示実装計画
tags: [project, discord, application, dynamodb, outbox]
status: planned
created: 2026-08-09
updated: 2026-08-09
---

# Discord討論過程表示実装計画

## 1. 目的

内部では生成・永続化済みである3人格の初回意見、最終案、投票と最終決定を、Discordの討論threadへ設計どおり表示する。

現在のproductionは討論を最後まで完了し、threadの操作panel、channelの公開Status Message、DynamoDBを`COMPLETED`へ収束できる。一方、通常の完了投稿はmoderatorによる最終決定だけで、途中の3人格の発言はDiscord Outboxへ作成されない。このため利用者からは討論過程が見えない。

本計画は表示責務だけを4本の独立PRへ分割する。1本ずつrequired CIとCodeQLを完了し、前のPRの結果を確認してから次へ進む。

## 2. 正本と現行契約

- [[01_要求仕様書・基本設計書#13. Discord上の表示仕様]]は、各人格Botが自分の初回意見、最終案、投票を投稿し、採択者だけが決定事項を投稿することを要求する。
- [[10_アプリケーション・Python詳細設計]]は、初回意見、最終案、投票、最終決定をPythonのphase順序で生成し、winnerをPythonだけで選ぶ。
- [[11_Discord詳細設計]]は、4 Bot identity、thread、2,000文字分割、mention無効化、nonce、Outbox、履歴reconciliationを定義する。
- [[13_DynamoDB・データ整合性詳細設計]]は、fenced lease、attempt-bound transaction、OutboxのPREPARED／CLAIMED／SENTと強整合readを正とする。
- [[18_試験・品質保証設計]]は、phase cancellation、Discord POST前後の停止、DynamoDB transaction、再送重複抑止を検証する。
- [[22_Discord受付・状態収束是正計画]]は受付と終端状態の是正を完了済みであり、本計画では再openしない。

## 3. 現行実装との差分

| 項目 | 現行 | 目標 |
|---|---|---|
| 初回意見 | 3件を生成・snapshot保存後、Discord投稿なしで次phaseへ進む | participant-a／b／cが自分の初回意見を1回ずつ投稿し、全件SENT後だけ次phaseへ進む |
| 最終案 | 3件を生成・snapshot保存後、Discord投稿なし | 各participantが自分の最終案を投稿する |
| 投票 | Pythonでwinnerを決めるために保存するがDiscord投稿なし | ballot確定後、採用した公開方式で投票先と理由を表示する |
| 最終決定 | moderator Botが投稿する | COMPLETEDだけはwinnerのparticipant Botが投稿し、失敗・取消はmoderatorのまま |
| 討論状況 | panelのphase表示だけ | panelを維持し、各phaseの実発言により進行を可視化する。追加のmoderator進捗投稿は作らない |
| recovery | terminal deliveryだけがstage→drain→finalizeを持つ | 各非terminal出力もstage→drain→advanceを持つ |

## 4. 採用方式

### 4.1 phase出力を先に永続化する

各phaseは次の固定順序で処理する。

```text
OpenAI出力を生成
→ snapshotのphase出力と対応Outbox operationsを同一transactionでstage
→ Outboxを既存publisherでdrain
→ 対応operationがすべてSENTであることをtransaction内で確認
→ 次phaseへadvance
```

生成結果だけを保存して先にphaseを進めない。Discord送信をDynamoDB transaction内で行わない。送信に失敗またはprocess停止した場合は同じphaseに留まり、保存済み出力とOutboxから再開する。

### 4.2 新しいaggregate schema fieldを増やさない

phase deliveryのidentityは、既存snapshot内のphase出力、attempt ID、phase、participant slot、chunk sequenceから決定的に再構築する。新しい`phase_delivery` fieldとDynamoDB schema migrationは追加しない。

repositoryへ非terminal phase専用のstage／finalize transactionを追加する。

- stageはexpected phase、lease fencing、出力未設定、operation ID不在を条件に、snapshot更新とOutbox作成を原子的に行う。
- replay時は保存済み出力から同じoperation ID、nonce、content hashを再構築し、完全一致だけを受理する。
- finalizeは全operationの`SENT`、attempt ID、phase、lease fencingを強整合条件として次phaseへ進める。
- 欠落、余分、content hash不一致、別attempt、未知slotはfail closedにする。

これによりPR-AをrevertしてもDynamoDB schema versionを戻す必要がない。ただしlive deployment前は既存Release gateどおりactive attempt、Ingress、Outboxが0の安全な停止状態を必須とする。

### 4.3 operation identityと投稿順

operation IDは次の要素から決定的に作る。

```text
phase + attempt ID + participant slot + chunk sequence
```

nonceもattempt IDからUUIDv7互換の決定的値として導出する。投稿順は生成完了順ではなく、`participant-a`、`participant-b`、`participant-c`、各messageのchunk sequence順に固定する。

OpenAI呼出しの並列性は変更しない。3件がすべて生成・検証された後にstageするため、一部の人格だけを先に公開しない。

### 4.4 message形式

各投稿は対応participant Botから次の形式で送る。

```text
**初回意見**
<本文>

**最終案**
<本文>

**投票**
投票先: <participant>
理由: <本文>
```

最終決定は既存の「最終決定」「実行案」「注意点」「AI生成に関する注意書き」を維持し、送信Botだけをwinnerへ変更する。

2,000文字制限、段落優先分割、`[n/m]`、`allowed_mentions.parse=[]`、content hash、履歴reconciliation、providerの`Retry-After`は既存契約を再利用する。質問、意見、投票理由、最終案、決定本文をstructured logやmetricへ追加しない。

## 5. PR分割

### PR-A: 初回意見の表示

最初に実装する。後続PRが再利用する最小の非terminal phase delivery primitiveと、初回意見3件の投稿だけを含める。

変更対象候補:

- `src/shittim_chest/application/discord.py`
- `src/shittim_chest/application/models.py`（既存型のvalidationが必要な場合だけ。schema fieldは追加しない）
- `src/shittim_chest/application/ports.py`
- `src/shittim_chest/application/service.py`
- `src/shittim_chest/adapters/dynamodb/repository.py`
- 対応するunit／DynamoDB Local／Discord contract tests

合格条件:

- 3件の初回意見とOutboxが同一transactionでstageされる。
- participant-a／b／cが自分の本文だけを投稿する。
- 3件の全chunkがSENTになるまで`DISCUSSING`へ進まない。
- transaction直前・直後、Discord POST直前・直後、mark-sent失敗、SIGTERM後も生成または表示が重複しない。
- pending／claimed Outboxがある間はscale-to-zeroしない。

### PR-B: 最終案の表示

PR-Aのprimitiveを変更せず再利用し、3人の最終案だけを追加する。

合格条件:

- 初回意見の全送信後だけ最終案生成へ進む。
- 3件の最終案の全chunkがSENTになるまで`SELECTING_WINNER`へ進まない。
- 初回意見のoperation、nonce、messageを変更しない。

### PR-C: 投票の表示

3票がすべて永続化されPythonによるwinner選択が確定した後に、投票表示をstageする。投票生成中に他者の票を見せず、公開はballot close後に行う。

合格条件:

- winnerは既存のPython `select_winner()`だけが選ぶ。
- DiscordまたはLLM出力からwinnerを再計算しない。
- 3票の全chunkがSENTになるまで`GENERATING_DECISION`へ進まない。
- 同票ruleと既存のdeterministic tie-breakを変更しない。

PR-C開始前に、7章の投票表示判断を確定する。

### PR-D: 採択者による最終発表

COMPLETEDのterminal Outboxだけをwinner participant slotへ割り当てる。CANCELLED、REJECTED、TERMINAL_FAILEDなどのsystem状態はmoderator投稿を維持する。

合格条件:

- 採択者だけが最終決定を投稿する。
- final decisionのwinnerは保存済み`VotingResult`と完全一致する。
- 現行terminal stage→drain→finalize、channel Status MessageのCOMPLETED収束、注意書きを維持する。
- failure/cancel時にparticipant Botがsystem errorを投稿しない。

## 6. 試験計画

| Layer | 必須確認 |
|---|---|
| pure unit | slot mapping、message形式、長文分割、決定的operation ID／nonce／hash、順序、unknown slot拒否 |
| application | stage前にadvanceしない、全SENT後だけadvance、保存済み出力を再生成しない、partial delivery recovery |
| repository | snapshot＋Outboxの原子stage、fencing、attempt/phase CAS、全SENT ConditionCheck、replay完全一致、欠落／余分fail closed |
| DynamoDB Local | crash boundary、transaction conflict、mark-sent不明、旧worker fencing、retry/resume、active counter不変 |
| Discord contract | 各Botが自分の投稿だけを送る、mention 0、history reconciliation、429／timeout、locked/deleted thread |
| lifecycle | pending／claimed Outbox中の停止抑止、SIGTERM checkpoint、4 READY復帰後のdrain |
| regression | terminal status convergence、HTTP受付時間、3 global lease、cancel/retry、status publisher、scale-to-zero |
| live acceptance | 1討論で投稿者、順序、内容種別、panel、channel、DynamoDB terminal、停止収束をcontent-free AWS evidenceと利用者確認で照合 |

focused testは各PRの直接変更範囲へ限定する。PR-Aではprimitiveのfault boundaryを厚く確認し、PR-B〜Dで同じ基盤試験を複製しない。local production／break-glass full image buildは行わず、canonical CIをimage identityの正とする。

## 7. 要判断事項

要求書は「匿名投票」としつつ、Discord表示仕様は「各人格Botが投票先と理由を発言」と定義している。

推奨解釈は、投票生成中は他者票を見せないsecret ballotとし、3票が確定したballot close後に各人格Bot名付きで投票先と理由を公開する方式である。これは独立投票を守りながら現行のDiscord表示仕様を満たす。

PR-AとPR-Bには影響しない。PR-C開始前にmanagerが次のどちらかを確定する。

1. 推奨: ballot close後にBot名、投票先、理由を公開する。
2. voter名を隠し、moderatorが匿名票として集約表示する。この場合は[[01_要求仕様書・基本設計書]]と[[11_Discord詳細設計]]の変更を先に行う。

## 8. CI・image baseline

各PRは`src/`を変更するため、image config digestを事前推測しない。

- 最初のcanonical CIでproductionとbreak-glassを同じbuild条件から実測する。
- 両imageのSBOM、verified VEX、risk gateとconfig digestの対応を確認する。
- 片方だけが変化しても、同じ測定で得た両baselineを同じPRで一括更新する。
- `fault-test`はbaseline対象に含めない。
- required CI、Grype、CodeQLがすべてgreenになるまでmergeしない。

Production ReleaseとEnvironment承認は各PRの実装・CIとは独立したmanager承認工程とする。Release前にRuntime `STOPPED`、ECS `0/0/0`、durable activity clear、deployment lock open、active Change Set 0を確認する。

## 9. Rollback・停止条件

- 各PRは後続PRを含めず単独revertできる状態にする。
- Discord送信済みmessageをrollback時に削除しない。
- phase delivery失敗時は同じphaseとOutboxを保持し、次回起動でreconcileする。
- unknown schema、別attempt、content hash不一致、不完全pagination、fencing喪失はfail closedにする。
- DynamoDB schema migration、新queue、SQS、DynamoDB Streams、新Lambda、CDK／IAM変更が必要になった場合は、そのPRへ追加せず再計画する。
- 1本のPRで隣接phaseまで変更する必要が出た場合は実装を停止する。
- CI failure、image digest再現性不一致、新規High/Critical residualはrerunやbaseline転記をせず直接原因を確定して停止する。

## 10. 明示的な非対象

- OpenAI prompt、model、reasoning effort、token budget
- Evidence生成、citation表示、Web archive
- `/shittim` command schema、HTTP ingress、SnapStart
- Runtime起動、30分idle、global 3 lease、FIFO 20件
- Status Publisher／Reconcilerの状態体系
- 新しいAWS resource、IAM、Discord Application設定
- 一般messageへのBot応答
- moderatorによる追加のphase実況message

## 11. 完了条件

- PR-A〜Dが独立して実装・review・mergeされる。
- 初回意見、最終案、投票、最終決定が要求どおりのBotから順序どおり表示される。
- 各phaseは全必須Outbox SENT後だけ次へ進む。
- retry、process停止、Discord timeout、DynamoDB conflictで重複表示しない。
- Pythonだけがwinnerを選ぶ。
- thread panel、channel Status Message、DynamoDBがCOMPLETEDへ収束する。
- 最後のdurable activity完了後30分でECS `desired/running/pending=0/0/0`へ収束する。
- required CI、Grype、CodeQL、live acceptanceが合格する。
- 未解決Critical／High issueがない。

## 12. 最初に実施する工程

PR-A「初回意見の表示」だけを実装する。投票表示方式の判断、最終案、投票、最終発表を同じPRへ含めない。
