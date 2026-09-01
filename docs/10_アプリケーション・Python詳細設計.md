---
aliases:
  - The Shittim Chest アプリケーション詳細設計
tags: [project, shittim-chest, python, detailed-design]
status: production-1.0
created: 2026-07-16
updated: 2026-09-01
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
