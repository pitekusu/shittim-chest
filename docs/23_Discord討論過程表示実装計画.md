---
aliases:
  - Discord討論過程表示実装計画
tags: [project, discord, application, dynamodb, outbox]
status: in-progress
created: 2026-08-09
updated: 2026-08-09
---

# Discord討論過程表示 実装計画（反証反映版）

## 1. 反証判定

既存計画への **DENY** は概ね妥当であり、Draft PR #164の計画書を全面改訂する。

- B-01、B-03、B-04、C-02、C-03、C-06、C-07は設計変更が必要。
- B-02は指摘どおり計画の定義不足。ただし現行コードには22文字base64url nonce生成器があるため、operation全体からの導出を明記して再利用する。[Discordはnonceを25文字以下とし、enforce_nonceによる短期間の重複排除を提供する](https://docs.discord.com/developers/resources/message)。
- C-01の「出力が無制限」は現行Pydantic schemaと文字数上限に反する。ただしDynamoDBの100 actions、4 MB、1 item 400 KBを実測で証明する必要はある。[TransactWriteItems](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html)
- C-04のTOCTOUは、現行Releaseが読取値をConditionCheckしてdeployment lockを原子的に取得し、各producerもlock-open条件をtransactionへ含めるため成立しない。改訂計画ではこの原子的取得を正式なRelease gateとして明記する。
- B-06は正本で解決済み。投票中は候補名を匿名化し、3票確定後に投票者Bot名・投票先・理由を公開する。
- H-01について、保証対象を「phase確定時にDiscordが受理・照合したこと」とする。後から利用者や管理者がmessageを編集・削除しないことまでは保証しない。

## 2. Durable generation／delivery protocol

### 2.1 OpenAI generation checkpoint

provider-level exactly-onceには依存しない。[Responses API create契約](https://developers.openai.com/api/reference/resources/responses/methods/create)で本用途の永続的な冪等性を前提にせず、store=falseを維持する。

GenerationCheckpointをphase＋participant単位で永続化する。

    PLANNED
    → IN_FLIGHT(attempt=1)
    → COMPLETED

    IN_FLIGHT(attempt=1)でworker喪失
    → IN_FLIGHT(attempt=2)
    → COMPLETED

    IN_FLIGHT(attempt=2)で再び結果不明
    → FAILED

- OpenAI呼出し前にcheckpointをfenced transactionでclaimする。
- 各participantの結果は、取得でき次第、既存output itemとcheckpointを同一transactionで保存する。
- 3件の生成は並列のままだが、Discord公開は3件すべての保存後だけ行う。
- 応答後・保存前の停止では1回だけ再呼出しを許可する。1 logical outputにつき最大2 SDK callとする。
- SDK内部のmax_retries=2は維持するため、最悪時のHTTP attempt上限はlogical output当たり6回である。
- CASにより永続化されるoutputは1件だけとし、再生成内容による上書きを拒否する。
- OpenAI API呼出し自体のexactly-onceは完了条件から外し、「永続outputとDiscord表示の重複なし」を保証する。

### 2.2 Phase deliveryとOutbox v2

新しいrecord-level schemaとして以下を追加する。table全体のglobal schemaは変更せず、既存コードが完了済み履歴を無視できる互換形にする。

- GenerationCheckpoint
- PhaseDeliveryPlan
- Outboxのrecord_schema_version=2
- OutboxStatus.ABANDONED
- localなchunk_sequenceとは別のglobal delivery_sequence
- abandoned_atとallowlist済みabandon_reason

PhaseDeliveryPlanはSTAGED／TERMINATING／DELIVERED／ABANDONEDを持つ。

- 全output保存後、PhaseDeliveryPlanと全Outboxを1 transactionでstageする。
- 全必須OutboxがSENTになった場合だけplanをDELIVEREDにし、同じtransactionで次phaseへ進む。
- 非retryable error、3回のdelivery attempt消費、またはstageから15分のdeadline超過では、新規claimを停止して残件をABANDONEDへ収束させ、attemptをFAILEDにする。
- FAILED／CANCELLED通知はmoderator Botによるbounded best-effortとする。通知自体が送れなくてもABANDONし、Status Publisher、lease解放、scale-to-zeroを妨げない。
- COMPLETEDの最終決定は必須配送とし、送れなければCOMPLETEDにせずFAILEDへ変換する。

利用者判断どおり、途中表示は必須機能とする。表示欠落のまま討論をCOMPLETEDにはしない。

### 2.3 Identityと実配送順序

operation identityは次の完全な組で決める。

    attempt ID + phase + Bot slot + local chunk sequence

nonceはこの完全identityからUUIDv7互換値を導出し、22文字のunpadded base64urlへ変換する。

delivery_sequenceはattempt全体で次の固定範囲を使う。

- 初回意見: 0–23
- 最終案: 100–123
- 投票: 200–223
- 最終決定: 300–319
- FAILED／CANCELLED通知: 900–919

1 logical participant outputは最大8 chunks、3人phase全体は最大24 operations、最終決定は既存どおり最大20 chunksとする。

Outbox claimは、同じattempt内で小さいdelivery_sequenceがすべてSENTまたは正当にABANDONEDであり、現在のPhaseDeliveryPlanがSTAGEDの場合だけ成功させる。これにより、複数claimer、429、timeoutがあってもDiscordへのPOSTは常に1件ずつ順番に行われる。

### 2.4 Cancel／failure

partial delivery中のCancelは次の順序に固定する。

1. PhaseDeliveryPlanをTERMINATINGへ変更し、新しいclaimを禁止する。
2. 既存のCLAIMED operationは60秒のclaim期限まで待ち、新しいPOSTを行わないreconciliation-only処理をする。
3. 見つかった完全一致messageはSENT、存在しない残件はABANDONEDにする。
4. CANCELLED通知をstageし、最終状態、activity counter、leaseを収束させる。

Retryは旧attemptがFAILEDかつ全OutboxがSENTまたはABANDONEDになった後だけ新attemptを作る。旧messageは削除せず、新attempt IDによりoperation IDとnonceを完全に分離する。

### 2.5 Discord境界

各phaseのOpenAI呼出し前に、対象Botについて次を検証する。

- threadが存在し、対象Guild内で、archived／lockedでない
- VIEW_CHANNEL
- SEND_MESSAGES_IN_THREADS
- READ_MESSAGE_HISTORY

権限変更をコードやDiscord API writeで自動修正しない。Discordではthread送信に専用権限が必要である。[Discord Permissions](https://docs.discord.com/developers/topics/permissions)

モデル本文は次のdisplay-only正規化後にOutboxへ保存する。

- CRLF／CRをLFへ統一し、Unicode NFCへ正規化
- tabを空白へ変換
- Unicode noncharacterとCc／Cf／Cs／Co／Cnを拒否
- Discord Markdownをescape
- 固定見出しの下ですべての本文行を引用表示
- allowed_mentions=[]とembed抑止を維持

Discordが返したcontentまたはhistory上のcontentが保存済みcontentと異なる場合はDISCORD_OUTBOX_CONFLICTとして送信を成功扱いせず、planをABANDONしてattemptをFAILEDへ収束させる。Discordが一部文字を除去し得るため、完全一致失敗を無期限待機へ変換しない。[Discord Message Resource](https://docs.discord.com/developers/resources/message)

## 3. 順序付きPR構成

従来の「4本の独立PR」は撤回し、以下の依存順を持つ5本とする。

### PR-0: Delivery safety foundation

- GenerationCheckpoint、Outbox v2、ABANDONED、global ordering、bounded deadline、sanitizerを実装する。
- 現行の最終決定生成・terminal deliveryへ適用し、新しい途中投稿はまだ有効化しない。
- 通常討論が従来どおり完了・停止することをproductionで確認する。
- 実装Draft PRは`#165`。新しい途中投稿を有効化せず、最終決定生成とterminal deliveryだけへ安全基盤を適用した。最初のcanonical CIで得た両imageのSBOM、VEX、risk gate、config digestを対応付け、同じ測定の両baselineを一括更新する。Production Releaseとlive acceptanceはmerge後の独立工程とする。

### PR-A: 初回意見

- participant 3人のcheckpoint、権限preflight、PhaseDeliveryPlan、初回意見投稿を有効化する。
- 初回意見だけが表示される状態を明示的なprogressive rolloutとして受け入れる。
- local実装では、3 Botのthread／Guild／permission preflight後に3件を並列生成し、結果ごとにoutputとcheckpointを同じfenced writeで保存する。全3件保存後だけPhaseDeliveryPlanとOutbox v2を一括stageし、participant-a／b／cの順で全件SENTを確認してからDISCUSSINGへ進む。
- 実装Draft PRは`#171`。focused application test 100件、DynamoDB Localを含むfull pytest 1,830件により、participant Bot所有、delivery_sequence 0／8／16、22文字の再現可能nonce、結果ごとの永続化、successor leaseでの第2 logical call、3回目のlogical call禁止、active leaseを保持したphase finalizeを確認した。
- canonical CI run `31318794695`の同一測定で得たproduction／break-glassの両config digestへbaselineを一括更新した。両SBOMにfixable High／Criticalはなく、canonical risk validatorはproduction `vendor_vex=15`／local acceptance 0、break-glass `vendor_vex=33`／local acceptance 0で成功した。
- Production Release run `31320074255`はmain SHA `b5fd10cf89c2780c83423dc8e5c45f1ad83d0d68`で成功した。live acceptanceでは、利用者が初回意見3件のBot所有、participant-a／b／c順、重複なしを確認した。scale-to-zeroの追加確認は利用者側で実施するため、この記録ではCodexによる確認済みとは扱わない。

### PR-B: 最終案

- 同じprimitiveを変更せず、最終案3件を追加する。
- local実装では、`COLLECTING_FINAL_PROPOSALS`でparticipantごとのGenerationCheckpointを用い、3 Botのdelivery preflight後に3件を並列生成する。各結果はcheckpoint完了と同じfenced writeで個別保存し、全3件がdurableになった後だけ`final-proposals`のPhaseDeliveryPlanとOutbox v2をstageする。
- Discord配送はparticipant-a／b／cの順、固定delivery_sequence 100／108／116から開始し、1人最大8 chunks、phase全体最大24 operationsとする。全件SENT後だけ`SELECTING_WINNER`へ進み、provider失敗、participant不一致、preflight失敗、2回のlogical call消費では既存のbounded failureへ収束する。
- focused application test 107件、DynamoDB Local repository test 29件、DynamoDB Localを含むfull pytest 1,838件で、参加者Bot所有、reserved sequence、22文字の再現可能nonce、結果ごとの永続化、successor leaseでの第2 logical call、preflight前のprovider call 0、失敗時の成功済みoutput保持、active leaseを保持したphase finalizeを確認した。Production Releaseとlive acceptanceは未実施である。
- canonical CI run `31322069591`では、baseline不一致以外のrequired checkとCodeQL 3言語が成功した。同一測定のartifactからproduction config digest `sha256:0de66ace43dd8c8532b49a557b52512723a7f62e0d9bae2cf275750cea5e547e`、break-glass config digest `sha256:7d53e6ed500340b71db9d76f65cd8bd16d142cfdfb4dbd61a30dd94a92299246`を取得し、両baselineを一括更新した。両SBOMはcanonical validatorで有効、fixable High／Criticalは0であり、risk validatorはproduction `vendor_vex=15`／local acceptance 0、break-glass `vendor_vex=33`／local acceptance 0で成功した。

### PR-C: 投票

- 3票を非公開で生成・保存後、participant Bot名、投票先、理由を順番に公開する。
- winnerは既存Python select_winner()だけが決定する。

### PR-D: 採択者による最終発表

- COMPLETEDの最終決定Outboxだけを保存済みwinnerのBotへ割り当てる。
- FAILED／CANCELLEDなどsystem通知はmoderatorのままにする。
- prompt、model、決定生成内容は変更しない。

各PRをmerge後、manager承認を得て個別にProduction Releaseし、live確認後に次PRへ進む。部分表示期間は意図した段階投入として計画書と進捗記録に明記する。

PRは独立revert可能とは主張しない。rollbackはdeployment lock取得、Runtime停止、durable activity clearを確認して、PR-Dから逆順に行う。コードrollbackはDiscordへ送信済みmessageを削除しない。

## 4. 試験・Release gate

### 4.1 Focused tests

- crash境界: OpenAI call前、応答後・保存前、保存後、2回目の結果不明
- nonce: 全phase／slot／chunkで一意、22文字、enforce_nonce=true
- ordering: 複数claimer、先頭429、timeout、mark-sent失敗でもarrival順を維持
- cancellation: 0件／一部／全件SENTからCANCELLEDへ収束
- permanent failure: permission、locked／deleted thread、content mismatch、deadline超過
- activity: SENT／ABANDONED後にpending・claimed counterが0
- compatibility: Outbox v1読込、v2履歴を残した安全なreverse rollback
- winner: a／b／c、tie-breakの全経路で正しいBotを選ぶ
- vote: 3票確定前のDiscord writeが0
- renderer: Markdown、mention、bidi／control文字、multi-chunk
- pagination: 500件上限到達を不完全としてfail closed

DynamoDB Localでは最大24-operation phaseと最大20-chunk terminalを実際のserializer／transactionで構築し、次をassertする。

- action数100未満
- aggregate item size 4 MB未満
- 各item 400 KB以下
- stage／finalize／abandonが原子的
- fencing喪失時は書込み0

### 4.2 CIとimage baseline

各実装PRはcanonical CIでproduction／break-glassを同時に実測する。

- SBOM、VEX、risk gateと両config digestの対応を確認する。
- 片方だけ変わっても両baselineを同じ実測結果から一括更新する。
- required CI、Grype、CodeQLがgreenになるまでmergeしない。
- local full image buildは行わない。

### 4.3 Progressive live acceptance

- PR-0: 現行形式の通常討論1件とterminal／scale-to-zero
- PR-A: 初回意見の3 Bot・順序・重複なし
- PR-B: 初回意見後の最終案3件
- PR-C: ballot close前write 0、close後の3票公開
- PR-D: 保存済みwinnerと最終投稿Botの一致

各回、panel、channel Status、DynamoDB、Outbox activity、ECS 0／0／0を確認する。winner全組合せ、multi-chunk、障害注入はlocal／contract試験で行い、live OpenAI試験を不必要に増やさない。

## 5. 文書と境界

- 既存Draft PR #164を維持し、Obsidian正本の23_Discord討論過程表示実装計画.mdをこの内容へ置換してから既存手順でmirrorを同期する。新しい計画書は追加しない。
- 実装PRでは変更した契約に直接関係する正本だけを更新する。
- 新しいAWS resource、IAM、CDK stack、Discord Application設定は追加しない。
- OpenAI prompt、model、reasoning、token budget、winner規則、Runtime起動方式は変更しない。
- Release安全性は既存の原子的deployment lock取得を正とし、単なる事前readをdeploy許可には使わない。
- SENTは送信・照合時点の成功を意味する。後日の外部編集・削除を継続監視する機能は別課題とする。

## 6. 開始工程と停止点

計画PR #164の改訂後、最初に実装する工程はPR-0「Delivery safety foundation」とする。PR-0では新しい途中投稿を有効化しない。

PR-0の実装、merge、Production Release、live acceptanceは、それぞれ本計画の依存順とmanager承認境界に従う。PR #164の改訂作業ではPR-0のコード変更、Production Release、AWS／Discord／OpenAI writeを行わない。
