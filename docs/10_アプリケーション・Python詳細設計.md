---
aliases:
  - The Shittim Chest アプリケーション詳細設計
tags: [project, shittim-chest, python, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-07-16
---

# アプリケーション・Python詳細設計

## 1. 目的と適用範囲

The Shittim ChestのPython application内部構造、公開interface、状態遷移、非同期制御、設定、終了処理を定義する。Discord、OpenAI、DynamoDB固有処理はadapterへ隔離し、本書と矛盾する実装を行わない。

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
| `DebateId` | `uuid.UUID`。`uuid.uuid7()`で生成する |
| `DebatePhase` | `StrEnum`。状態遷移表以外から変更しない |
| `DebateErrorCode` | user向け表示と再試行可否を分離した安定code |
| `BotIdentity` | private runtime config由来のapplication ID、slot、表示名、role |
| `PersonaSpec` | slot、config version、schema version、prompt hash。prompt本文は保持しない |
| `EvidenceBundle` | immutable tuple。source URL/title/metadata、取得時刻、content hashを含む |
| `OutboxOperation` | operation ID、Bot ID、22文字nonce、content hash、claim/retry/chunk状態 |

domain modelは原則`@dataclass(frozen=True, slots=True)`とし、時刻はtimezone-aware UTC、永続化recordには`schema_version`を必須とする。

## 4. Application interface

```python
async def accept_debate(request: AcceptDebateRequest) -> AcceptedDebate: ...
async def run_debate(debate_id: DebateId) -> None: ...
async def resume_recoverable() -> None: ...
```

Protocolは`Clock`、`IdGenerator`、`Metrics`、`DiscordGateway`、`DiscordPublisher`、`OpenAIService`、`DebateRepository`とする。`DiscordPublisher`は永続化済みoutbox operation以外を投稿してはならない。

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

`SELECTING_WINNER`は匿名投票の収集、検証、tie-breakを含む。任意の進行状態から`CANCELLED`、回復不能errorから`FAILED`へ遷移できる。Spot停止は状態ではなく`recovery_state=checkpointed`として保存し、後続taskが同じphaseを再開する。遷移はexpected phaseとfencing tokenを条件にしたrepository operationで行い、不一致時は副作用を発生させない。本節を`DebatePhase`の唯一の定義とする。

## 6. 非同期制御

- 3人格処理はphase単位の`asyncio.TaskGroup`で並列化する。
- session全体はactive processing 300秒、通常目標180秒とする。
- OpenAI同時実行はprocess全体で`Semaphore(6)`に制限する。
- deadlineは`asyncio.timeout()`で管理し、残時間が次のattemptに不足する場合は再試行しない。
- 所有者不明の`asyncio.create_task()`と`CancelledError`の握り潰しを禁止する。
- 同期boto3処理は専用worker threadへ隔離し、client/resourceはbootstrapで一度だけ生成して再利用する。

## 7. Cancel・retry・shutdown

- cancel可能者は開始userまたは`Manage Messages`保持者。新規OpenAI callと未送信outboxを止め、完了済み成果物を保持して`CANCELLED`へ遷移する。
- retry可能状態は`FAILED`だけ。保存済みartifactを再利用し、未完了operationだけを再実行する。
- SIGTERM受信時はREADY gateを閉じ、TaskGroupをcancelし、checkpointとoutboxをflushして120秒以内に終了する。
- SIGKILLでflushできなくても、lease期限、fencing token、outbox reconciliationで回復できることを前提とする。

## 8. 設定と起動validation

bootstrapへ渡す非秘密設定はenvironment、model ID、table名、log level、version付きruntime/persona Parameter名、5つのcredential Parameter名とする。ECSが注入した`RuntimeConfig`と4つの`PersonaConfig`をPydanticで検証し、`schema_version`/`config_version`不一致、slot欠落、重複Application ID、空allowlist、不正Guild ID、promptのUTF-8 3,500 bytes超過があればDiscord接続前に終了する。値、display name、promptをvalidation errorやlogへ含めない。

`RuntimeConfig`はGuild ID、allowed channel IDs、4 Application IDを保持する。`PersonaConfig`のslotは`moderator`、`participant-a`、`participant-b`、`participant-c`だけを許可し、display nameとsystem promptを保持する。公開sourceにはschemaと汎用sampleだけを置き、本番値をfileへfallbackしない。

## 9. Coding規約

- Python 3.14.6通常GIL build、`requires-python = ">=3.14,<3.15"`、uv lock固定。
- 全function、method、attributeを型付けし`mypy --strict`を通す。
- Ruffだけをformatter/import sorter/linterとして使い、100文字、double quote、`E,F,I,UP,B,SIM,ASYNC,RUF,S`を基準とする。
- cyclomatic complexityは10以下。naive datetime、mutable default、application層の`dict[str, Any]`は禁止する。
- import-linterで`domain <- application <- adapters`の方向を検証する。

## 10. 公式資料確認記録

| 確認日 | 対象version/service | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-07-16 | Python 3.14.6 | https://www.python.org/downloads/ | runtime基準version |
| 2026-07-16 | asyncio | https://docs.python.org/3/library/asyncio-task.html | TaskGroup、timeout、cancellation |
| 2026-07-16 | uv | https://docs.astral.sh/uv/concepts/projects/sync/ | lockと`--frozen` |
| 2026-07-16 | boto3 1.43.49 | https://boto3.amazonaws.com/v1/documentation/api/latest/index.html | client再利用、typed exception、thread隔離 |
