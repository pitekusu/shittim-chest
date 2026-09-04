---
aliases:
  - The Shittim Chest アプリケーション詳細設計
tags: [project, shittim-chest, python, detailed-design]
status: production-1.0
created: 2026-07-16
updated: 2026-09-04
---

# アプリケーション・Python詳細設計

## 1. Architecture

| Layer | Ownership | Dependency |
|---|---|---|
| `domain` | identifier、phase、vote、純粋な不変条件 | 標準libraryのみ |
| `application` | use case、port、checkpoint、Outbox、scale-to-zero判断 | `domain` |
| `adapters` | Discord、OpenAI、DynamoDB、AWS SDK | `application`, `domain` |
| `config` | private inputのstrict validation | SDK非依存model |
| `runtime` | process lifecycle、signal、health、farewell | 上記をcomposition |
| `lambda_handlers` | Lambda eventとuse caseの薄い境界 | adapter／application |
| `bootstrap.py` | client生成、dependency wiring、close | 全componentのcomposition |

SDK型、HTTP response、DynamoDB AttributeValueをdomain／applicationへ漏らさない。
`import-linter`で`application → domain`の一方向contractを固定する。

## 2. Immutable models and identifiers

- 値objectは原則frozen／slotsとし、aware UTC datetimeだけを受ける。
- Debate ID、Attempt ID、lease owner、operation IDを用途別に型分離する。
- Attempt IDはUUIDv7。retryは新Attempt IDを作り、旧attemptを変更しない。
- persisted enum／schemaはunknown valueを拒否し、暗黙のdefaultで読み替えない。
- questionはtrim後に非空、1〜1,000文字とする。

## 3. Debate state machine

通常phaseは`accepted`から`completed`まで一方向である。`failed`と`cancelled`はterminalで、
terminal stateからphaseを戻さない。recovery checkpointはphaseと別fieldで管理し、terminal stateへ
残さない。

Serviceは次を順番に実行する。

1. fenced leaseを取得しsnapshotを読む。
2. 3人の質問評価を並列実行し、全件成功時だけprofile、討論評価、
   `preparing_evidence`遷移を同一transactionで確定する。
3. 共通Evidenceを準備または再利用する。
4. participant単位のgeneration checkpointをclaimし、3件を並列生成する。
5. 全output保存後にphase delivery planとOutboxを原子的にstageする。
6. 必須Outboxがすべて`SENT`になった場合だけ次phaseへ進む。
7. voteをPythonで集計しwinnerを固定する。
8. result／winner decisionのthread deliveryと、元channelへの親愛度独立deliveryを同一planにstageし、
   必須operationがすべて`SENT`になった後に`completed`へ確定する。

Generationはlogical outputごとに最大2 SDK callを許す。結果保存のCASに勝った1件だけを正とし、
再生成結果で上書きしない。

親愛度評価は討論ごと1回で、CAS競合時はproviderを再呼び出しせず、同じ評価値を
最新profileの0〜1,000範囲へclampして再適用する。討論評価が既にあるretryでは再評価や
二重加算を行わない。評価後の討論失敗／取消でもprofileの変更を戻さず、
元channelへの独立deliveryをterminal planに含める。

各cycleで未解放のprofileが初めて1,000点へ到達した場合は、変更前score、clamp前の質問評価の順で
候補を絞り、同点はAttempt由来のrandom seedで1人を選ぶ。選出結果、到達時刻、Debate ID、当時の
質問者表示名、cycleをprofileと討論評価へ同じtransactionで保存するため、CAS retryでも選出は変わらない。
v8からopaque v9へ移行する成功評価だけは、移行前から1,000点の人格も候補へ含める。通常のv9評価では
今回`before < 1000`から`after = 1000`になった人格だけを新規到達として扱う。
評価不能時は旧profileのopaque key移行も解放判定も行わない。

Core起動設定は`SHITTIM_RECORDS_MEMORIAL_URL`を必須とし、port、query、fragment、userinfoを持たない
canonicalな`https://<hostname>/memorial`だけを受理する。このURLは公開Discord messageへ描画する値であり、
Runtimeの`RecordsPublicHostname`から構成してtaskへ渡す。

### Memorial backend

Recordsのowner-only APIはSessionから導出したopaque requester keyだけをrepositoryへ渡し、別の質問者を
request bodyやpathから指定させない。upload予約、生成queue投入、cycle別履歴参照、resetは安定した
content-free errorへ変換し、POSTはOrigin、CSRF、idempotencyと必須の`expectedCycle`を検証する。
upload／generate／resetは現在cycleが`expectedCycle`と一致する場合だけ状態を変更し、遅延した
requestが次cycleを操作することを409でfenceする。

生成workerは1件のSQS messageから`requester key + cycle`だけを受け、Statistics checkpointを
`queued → generating → ready|failed`へ条件付き更新する。APIがcheckpoint更新後のqueue送信前に停止しても、
`queued`の同一jobは新しいidempotency keyでもSQSへ安全に再送できる。retryは同じcheckpointと
result asset keyを再利用し、既に`ready`のcycleを再生成しない。Archive GSI3から本人の直近10質問だけを読み、選出participantの
active runtime promptを完全検証して読み込む。active pointerがない場合だけReleaseが固定したlegacy
participant promptを使い、欠落・checksum不一致・不正revisionでは生成を開始しない。
worker claimはgeneration attemptをcheckpoint内でCAS incrementし、paid generationは3回を上限とする。Standard SQSの
`ApproximateReceiveCount`は参考情報に留め、再送や重複で変わる物理receive回数を終端判定の正本にしない。
そのclaimでpaid callをまだ一度も開始していないdeadline preflightで残時間不足を検出した場合は、claim固有tokenを
含む同じCAS条件でcheckpointを`queued`へ戻し、claim時のincrementを1だけ払い戻す。保存済みの文章や画像参照、
upload原本は維持する。一度でもpaid callを開始したclaimは払い戻さず、logical attempt上限を迂回させない。

workerは文章と最終画像を個別にcheckpointし、両方が永続化された後にだけ`ready`を確定する。
logical attempt 3回の後は新しいpaid provider callを開始しない。検証済み最終画像のobjectまたはcheckpointと
文章が残る場合だけ同じcycle／asset keyからcompletion-onlyで再開する。最終画像がない文章だけの成果は
terminal化時に破棄し、reset可能な`failed`へ収束させる。部分成果は`ready`までAPIへ公開しない。成功時または
再試行不能なterminal失敗時にupload原本を削除し、一時障害ではSQS retryへ委ねる。provider callは各120秒、
OpenAI SDKの自動retryは0回とし、Lambda hard deadlineからcleanup用15秒を差し引いた残時間が不足する場合は
そのclaimでpaid callを開始していない場合だけattemptを払い戻してqueueへ戻す。SQS redriveは最大4 receiveとするが、
paid可否は物理receive回数ではなく永続counterだけで判定する。`generation_attempt=3`のclaimがhard timeout／OOM／
runtime crashとなり次の配送が残る場合、再claimはattempt 4となり、保存済み成果のcompletion-only回復または
providerを呼ばないterminal化だけを行う。resetはAPIだけがsource v9 profileと
当該cycle checkpointを1 transactionで更新し、3スコアを500、reset回数とcycleを各+1へ進める。

## 4. Concurrency and cancellation

- process内のasync taskはownerを明確にし、cancel後に必ずawaitする。
- participant 3件の生成はTaskGroup相当のstructured concurrencyで並列化する。
- OpenAI共有limiterは最大6 request。vote公開とDiscord Outboxは永続順序で直列化する。
- 親愛度評価を含む1討論sessionは420秒、SDK retryを含む各logical generation phaseは120秒を上限とする。
  OpenAI transportの1試行60秒とは分離し、親愛度評価後のEvidence、3段階生成、winner decisionに余裕を残す。
- `CancelledError`を通常errorへ変換せず再送出する。
- Stopは新しいwork claimを閉じ、active attemptをcheckpointし、Discord clientとSDK clientを
  bounded timeoutでcloseする。

## 5. Runtime lifecycle

1. environmentとversion付きRuntime／PersonaConfigをstrictに検証する。
2. DynamoDB、Lambda、OpenAI、Discord clientをprocess単位で生成する。
3. moderatorと3 participantを起動し、4 identityのREADYとGuild／Channel境界を検証する。
4. command schema hashがdeploy時のprevious hashと異なる場合だけGuild Commandをsyncする。
5. recovery、ingress drain、status、heartbeat、farewell watcherを開始する。
6. SIGINT／SIGTERMまたはsupervisor異常でadmissionを閉じ、checkpointして終了する。

ECSのcontainer healthはprocess内のreadinessとheartbeatを読む。healthcheck自体が外部APIを呼ばず、
終了処理と競合しない。

## 6. Configuration

- RuntimeConfig schema v2をSSMのversion付きpathから読み、production pointerは`v0004`を使う。
- moderator＋participant 3 slot、Guild、allowed channels、farewell channel、OpenAI project、
  deployment parameterを過不足なく検証する。
- PersonaConfig schema v1を1 slotずつ4設定揃え、participant display nameの重複を拒否する。
- persona promptは非空、最大3,500 UTF-8 bytesで、repr／validation errorへ本文を出さない。
- secretは専用SSM pathからexact `GetParameters`で取得し、path走査をしない。
- RuntimeはRecordsと共通のidentity HMAC SecureStringを起動時にexact `GetParameters`で1回だけ読み、
  key長を検証してからrepositoryへ渡す。環境変数、repr、errorにはparameter名だけを保持し、値の欠落、
  取得失敗、短い値はclient構築前にcontent-free errorでfail closedとする。

## 7. Errors and telemetry

- domain／application errorは安定したcontent-free codeへ変換する。
- question、model output、persona、URL、query、requester／Discord ID、token値をlogやmetric dimensionへ
  入れない。opaque Debate IDとprovider response IDはcontent-freeな障害相関にだけ使用できる。
- expected provider error、conditional write conflict、lease lossを分類し、programming errorと混ぜない。
- Evidenceの既知provider／validation failureは承認済みdegraded stateへ変換する。
- 帰宅挨拶の失敗はbest-effort failureとして記録し、shutdownを妨げない。

## 8. Source of exact values

dependency、tool、timeout、field limitのexact値はcode、`uv.lock`、testを正とする。文書は所有権と
不変条件を示し、patch versionや一時的な実装行数を固定しない。

## 9. 公式資料確認記録

| 確認日 | 対象 | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-08-14 | Python asyncio | https://docs.python.org/3/library/asyncio-task.html | structured concurrency、timeout、cancellation |
| 2026-08-14 | Python typing | https://docs.python.org/3/library/typing.html | SDK非依存Protocol boundary |
| 2026-08-14 | Pydantic models | https://docs.pydantic.dev/latest/concepts/models/ | strict／frozen／extra-forbid validation |
| 2026-08-14 | uv project | https://docs.astral.sh/uv/concepts/projects/sync/ | frozen lockとreproducible environment |
