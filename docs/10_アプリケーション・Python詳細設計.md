---
aliases:
  - The Shittim Chest アプリケーション詳細設計
tags: [project, shittim-chest, python, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-07-17
---

# アプリケーション・Python詳細設計

## 1. 目的と適用範囲

The Shittim ChestのPython application内部構造、公開interface、状態遷移、非同期制御、設定、終了処理を定義する。Discord、OpenAI、DynamoDB固有処理はadapterへ隔離し、本書と矛盾する実装を行わない。

2026-07-16の最初の実装sliceではPython/uv project基盤とdomainの識別子・attempt単位の状態機械を実装した。Application interface、外部adapter、async orchestration、shutdown連携は後続sliceで実装する。

## 2. Package構成

```text
src/shittim_chest/
├── __main__.py
├── bootstrap.py
├── config/
├── domain/
├── application/
├── adapters/
│   ├── discord/
│   ├── openai/
│   └── dynamodb/
└── observability/
tests/{unit,contract,integration,fixtures}/
tools/
```

- `domain`: 標準libraryだけに依存する。状態、値object、投票規則、error codeを保持する。
- `application`: domainとProtocolだけに依存する。use caseとtransaction境界を定義する。
- `adapters`: 外部SDK型をdomain型へ変換する。SDK responseをapplicationへ渡さない。
- `bootstrap.py`: 設定検証、client生成、dependency組立て、lifecycle所有を行う唯一のcomposition root。
- DI framework、service locator、global mutable state、汎用`utils/`は禁止する。

## 3. 主要domain型

| 型 | 仕様 |
|---|---|
| `DebateId` | UUIDv7だけを受理するfrozen/slots value object。`uuid.uuid7()`で生成する |
| `AttemptId` | 1回のimmutable実行attemptを識別するUUIDv7 value object。同じdebate内でもretryごとに新規生成する |
| `DebatePhase` | `StrEnum`。状態遷移表以外から変更しない |
| `DebateState` | debate/attempt ID、phase、`recovery_state`、`retry_of`、`failed_from_phase`、UTC更新時刻、正の`schema_version`を持つimmutable state |
| `InvalidPhaseTransition` | 未定義phase edgeを副作用前に拒否する安定code付きdomain error |
| `InvalidRecoveryTransition` | checkpoint/resume不変条件違反を拒否する安定code付きdomain error |
| `InvalidRetryTransition` | FAILED以外からのretryとsource attempt ID再利用を拒否する安定code付きdomain error |
| `DebateErrorCode` | user向け表示と再試行可否を分離した安定code |
| `BotIdentity` | private runtime config由来のapplication ID、slot、表示名、role |
| `PersonaSpec` | slot、config version、schema version、prompt hash。prompt本文は保持しない |
| `EvidenceBundle` | immutable。要約、`none/optional/required`、router rules version/reason、検索状態、response ID、source URL/title/canonical metadata、UTC取得時刻、metadata SHA-256を含む |
| `OutboxOperation` | operation ID、generic Bot slot、22文字nonce、content hash、claim/retry/chunk状態 |

domain modelは原則`@dataclass(frozen=True, slots=True)`とし、時刻はtimezone-aware UTC、永続化recordには`schema_version`を必須とする。

## 4. Application interface

```python
async def accept_debate(request: AcceptDebateRequest) -> AcceptedDebate: ...
async def bind_discord_context(command: BindDiscordContextCommand) -> BoundDiscordContext: ...
async def run_debate(debate_id: DebateId) -> None: ...
async def cancel_debate(command: CancelDebateCommand) -> CancelledDebate: ...
async def retry_debate(command: RetryDebateCommand) -> AcceptedRetry: ...
async def resume_recoverable() -> None: ...
```

Protocolは`Clock`、`IdGenerator`、`Metrics`、`DiscordGateway`、`DiscordPublisher`、`DiscordOutboxRepository`、`EvidenceService`、`CandidateOrderer`、`OpenAIService`、`DebateRepository`とする。`EvidenceService`は質問ごとに最大1つのResponses requestでimmutableな共通Evidenceを準備し、`CandidateOrderer`は投票者ごとの候補順random化を注入可能にする。`DiscordPublisher.publish_persisted`はexpected leased `DebateSnapshot`とattempt内operation IDを受け、永続化・claim済みoutbox operation以外を投稿してはならない。既に`SENT`なら同じrecord、claim不能なら`None`、成功または履歴照合成功なら`SENT` recordを返す。必須Evidence取得不能は`required_evidence_unavailable`としてFAILEDへ保存し、任意取得不能は`optional_unavailable`を保存して続行する。

STEP-03の`DebateApplication`は外部SDKをimportせず、これらのProtocolとimmutable `DebateSnapshot`だけを扱う。STEP-04Aでは`DebateSnapshot`へGuild/channel、debate/attempt作成時刻、Discord starter/thread ID、`LeaseGrant`を追加した。STEP-06Aではstarter message、thread、control panel messageを別fieldに分離し、`ACCEPTED`中だけ3 IDを一括bindingでき、同一値の再送は同じ結果、部分bindingまたは別値へのrebindは副作用なしで拒否する。cancel/retryも永続化済みoperation IDで再実行結果を返し、別request/debateへのoperation ID再利用を拒否する。

`DebateRepository.create`はoperation IDとlease ownerを受け、quota・slotを含む原子的受付後のpersisted snapshotを返す。`replace`はexpected snapshotと任意のoperation ID、`create_retry`はexpected FAILED snapshot、operation ID、lease ownerを受ける。`claim_recoverable`はlease取得済みsnapshotだけを返し、`renew_lease`はowner/fencingを維持した新expiryを返す。競合は`RepositoryConflict`、slot枯渇は`RepositoryBusy`、日次上限は`RepositoryQuotaExceeded`へ変換する。STEP-04BでDynamoDB API呼出しとtransactionをadapterに実装済みである。

## 5. 状態遷移

```text
ACCEPTED
  -> PREPARING_EVIDENCE
  -> COLLECTING_INITIAL_OPINIONS
  -> DISCUSSING
  -> COLLECTING_FINAL_PROPOSALS
  -> SELECTING_WINNER
  -> GENERATING_DECISION
  -> COMPLETED
```

`SELECTING_WINNER`は匿名投票の収集、検証、tie-breakを含む。1 attempt内では7つの進行状態それぞれから`CANCELLED`または`FAILED`へ遷移できるため、通常7 edgeとcancel/fail 14 edgeの合計21 edgeだけを許可する。terminal状態からの遷移、自己遷移、phaseの飛び越し、逆行を禁止する。

Spot停止は状態ではなく`recovery_state=checkpointed`として保存し、後続taskが同じphaseを再開する。checkpointは非terminalかつ`recovery_state=none`の場合だけ許可し、checkpoint中のphase遷移、二重checkpoint、checkpointなしのresume、terminal checkpointを拒否する。state更新時刻はtimezone-aware UTCかつ直前時刻以上とし、同一時刻を許容する。`debate_id`と`schema_version`は全遷移で不変とする。

`FAILED`への遷移時は直前の進行phaseを`failed_from_phase`へ保存する。retryはFAILEDから元phaseへ戻すedgeではなく、同じ`DebateId`の下に新しい`AttemptId`を持つstateを作るfactory operationである。新attemptの`retry_of`は直前attempt ID、初期phaseは直前の`failed_from_phase`とし、元FAILED stateを変更しない。domainはsource attempt ID再利用とFAILED以外からのretryを拒否し、debate内の全attempt IDの一意性はrepositoryの条件付きPutで保証する。

遷移はexpected phaseとfencing tokenを条件にしたrepository operationで行い、不一致時は副作用を発生させない。本節を`DebatePhase`とretry aggregate境界の唯一の定義とする。

## 6. 非同期制御

- 3人格処理はphase単位の`asyncio.TaskGroup`で並列化する。
- session全体はactive processing 300秒、通常目標180秒とする。
- OpenAI同時実行はprocess全体で`Semaphore(6)`に制限する。
- deadlineは`asyncio.timeout()`で管理し、残時間が次のattemptに不足する場合は再試行しない。
- 所有者不明の`asyncio.create_task()`と`CancelledError`の握り潰しを禁止する。
- 同期boto3処理は専用worker threadへ隔離し、client/resourceはbootstrapで一度だけ生成して再利用する。
- `run_debate`はphase taskと所有済みlease heartbeat taskを同時に監督し、20秒ごとにrenewする。phase終了時はheartbeatをcancelして必ずawaitし、heartbeat異常時はphaseをcancelする。
- phase timeoutは`phase_deadline_exceeded`、session timeoutは`session_deadline_exceeded`へ分離する。TaskGroupの1子失敗時は兄弟をcancelし、attemptを`FAILED`へ条件付き保存する。

## 7. Cancel・retry・shutdown

- cancel可能者は開始userまたは`Manage Messages`保持者。新規OpenAI callと未送信outboxを止め、完了済み成果物を保持して`CANCELLED`へ遷移する。
- retry可能状態は`FAILED`だけ。元attemptをimmutableに保ち、同じdebate/threadへ`retry_of=<直前attempt-id>`の新attemptを作る。保存済みartifactを参照し、`failed_from_phase`の未完了operationだけを再実行する。日次開始quotaは増やさず、global leaseは新attempt用に取得する。
- repository readerは旧recordを現行domain schemaへup-convertし、必要な条件付きlazy migrationを完了してからstate-changing use caseへ渡す。`new_retry_attempt()`が継承する`schema_version`はこの現行versionであり、旧versionのまま新Attempt METAを書かない。
- SIGTERM受信時はREADY gateを閉じ、TaskGroupをcancelし、checkpointとoutboxをflushして120秒以内に終了する。
- SIGKILLでflushできなくても、lease期限、fencing token、outbox reconciliationで回復できることを前提とする。
- STEP-03ではprocess cancellationを受けた`run_debate`が現行phaseをcheckpointし、`CancelledError`を再送出する。user cancelは開始userまたはadapterが検証済みの`Manage Messages`権限だけを受理し、repositoryの条件付きterminal遷移で進行中workerの後続writeを拒否する。

## 8. 設定と起動validation

bootstrapへ渡す非秘密設定はenvironment、model ID、table名、log level、version付きruntime/persona Parameter名、5つのcredential Parameter名とする。ECSが注入した`RuntimeConfig`と4つの`PersonaConfig`をPydanticで検証し、`schema_version`/`config_version`不一致、slot欠落、重複Application ID、空allowlist、不正Guild ID、promptのUTF-8 3,500 bytes超過があればDiscord接続前に終了する。値、display name、promptをvalidation errorやlogへ含めない。

`RuntimeConfig`はGuild ID、allowed channel IDs、4 Application IDを保持する。`PersonaConfig`のslotは`moderator`、`participant-a`、`participant-b`、`participant-c`だけを許可し、display nameとsystem promptを保持する。公開sourceにはschemaと汎用sampleだけを置き、本番値をfileへfallbackしない。

## 9. Coding規約

- Python 3.14.6通常GIL build、`requires-python = ">=3.14,<3.15"`、uv lock固定。開発・CI・releaseはuv 0.11.29を使い、`required-version = ">=0.11.8,<0.12"`で同一minorのDependabot updaterを許可する。`uv_build` lower boundは0.11.29を維持する。
- 全function、method、attributeを型付けし`mypy --strict`を通す。
- Ruffだけをformatter/import sorter/linterとして使い、100文字、double quote、`E,F,I,UP,B,SIM,ASYNC,RUF,S`を基準とする。
- cyclomatic complexityは10以下。naive datetime、mutable default、application層の`dict[str, Any]`は禁止する。
- import-linterで`domain <- application <- adapters`の方向を検証する。

## 10. 公式資料確認記録

| 確認日 | 対象version/service | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-07-16 | Python 3.14.6 | https://www.python.org/downloads/release/python-3146/ | runtime基準version |
| 2026-07-16 | Python 3.14 UUID/enum/dataclass/datetime | https://docs.python.org/3.14/library/uuid.html、https://docs.python.org/3.14/library/enum.html、https://docs.python.org/3.14/library/dataclasses.html、https://docs.python.org/3.14/library/datetime.html | UUIDv7、StrEnum、frozen/slots、aware UTCの実装境界 |
| 2026-07-16 | asyncio | https://docs.python.org/3/library/asyncio-task.html | TaskGroup、timeout、cancellation |
| 2026-07-16 | uv/uv_build 0.11.29 | https://docs.astral.sh/uv/concepts/projects/sync/、https://docs.astral.sh/uv/concepts/build-backend/ | lock、`--frozen`、pure Python package build |
| 2026-07-17 | uv `required-version`・versioning | https://docs.astral.sh/uv/reference/settings/#required-version、https://docs.astral.sh/uv/reference/policies/versioning/ | PEP 440互換範囲を使用し、0.11.8 updaterと0.11.29開発基準を両立 |
| 2026-07-17 | Python 3.14.6 asyncio | https://docs.python.org/3.14/library/asyncio-task.html | TaskGroupの兄弟cancel、`asyncio.timeout()`、`CancelledError`再送出を実装 |
| 2026-07-17 | Python 3.14.6 typing Protocol | https://docs.python.org/3/library/typing.html | runtime判定を行わないstructural typing boundaryを採用 |
| 2026-07-17 | pytest-asyncio 1.4.0 | https://pypi.org/project/pytest-asyncio/ | strict modeでasync use caseを試験 |
| 2026-07-17 | import-linter 2.13 | https://pypi.org/project/import-linter/ | `application -> domain`の一方向contractをCI必須化 |
| 2026-07-17 | boto3/boto3-stubs 1.43.50 | https://boto3.amazonaws.com/v1/documentation/api/latest/index.html、https://pypi.org/project/boto3/ | client再利用、typed exception、thread隔離、Python 3.14対応 |
| 2026-07-17 | DynamoDB data type・item制限 | https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.NamingRulesDataTypes.html | SDK非依存native-value契約、UTF-8、400KB事前拒否 |
