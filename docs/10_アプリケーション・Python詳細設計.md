---
aliases:
  - The Shittim Chest アプリケーション詳細設計
tags: [project, shittim-chest, python, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-07-28
---

# アプリケーション・Python詳細設計

## 1. 目的と適用範囲

The Shittim ChestのPython application内部構造、公開interface、状態遷移、非同期制御、設定、終了処理を定義する。Discord、OpenAI、DynamoDB、AWS SDK固有処理はadapterへ隔離し、本書と矛盾する実装を行わない。

2026-07-28時点で、Discord HTTP Interactionの署名付き受付、DynamoDBの耐久FIFO、On-Demand FargateのScale-to-Zero、稼働中だけ開くDiscord Gateway、Runtime Reconciler、完全終了・IDLE・graceful stopのコードとoffline試験を実装済みである。AWSとDiscord Applicationは未変更・未deployであり、実環境のInteraction Endpoint、Lambda、ECS scale-up/down、Gateway接続は未検証である。

## 2. Package構成

```text
src/shittim_chest/
├── __main__.py
├── bootstrap.py
├── lambda_handlers/
├── runtime/
├── config/
├── domain/
├── application/
├── adapters/
│   ├── aws/
│   ├── discord/
│   ├── discord_http.py
│   ├── openai/
│   └── dynamodb/
└── observability/
tests/{unit,contract,integration,fixtures}/
tools/
```

- `domain`: 標準libraryだけに依存する。状態、値object、投票規則、error codeを保持する。
- `application`: domainとProtocolだけに依存する。use caseとtransaction境界を定義する。
- `adapters`: 外部SDK型をdomain型へ変換する。SDK responseをapplicationへ渡さない。
- `lambda_handlers`: DiscordIngress、Status Publisher、Runtime Reconcilerの薄いcomposition rootとする。AI討論やOpenAI callを実行しない。
- `runtime`: Fargate taskの稼働中だけprocess signal、Discord Gateway readiness、Ingress Drainer、task ownership、recoveryをSDK非依存Protocolで統合する。
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
| `DiscordHttpOperation` | 署名検証後のcommand/componentをSDK型とInteraction tokenから切り離したimmutable input |
| `IngressRequest` / `IngressClaimFence` | 最大20件FIFOの耐久操作と、owner・claim expiry・delivery attemptに結び付くPII非保持のwrite fence |
| `RuntimeState` / `RuntimeActivity` | singleton runtimeのgeneration・desired count・STARTING/READY/BUSY/IDLE/STOPPING/STOPPED/DEGRADEDと、完全終了判定用counter snapshot |
| `DeploymentLock` | production deploy中のIngressとscale-down writeを閉じるowner・guard ID・fencing token付きlock |

domain modelは原則`@dataclass(frozen=True, slots=True)`とし、時刻はtimezone-aware UTC、永続化recordには`schema_version`を必須とする。

## 4. Application interface

```python
async def accept_debate(request: AcceptDebateRequest) -> AcceptedDebate: ...
async def bind_discord_context(command: BindDiscordContextCommand) -> BoundDiscordContext: ...
async def get_debate(debate_id: DebateId) -> DebateSnapshot: ...
async def run_debate(debate_id: DebateId) -> None: ...
async def cancel_debate(command: CancelDebateCommand) -> CancelledDebate: ...
async def retry_debate(command: RetryDebateCommand) -> AcceptedRetry: ...
async def resume_recoverable() -> None: ...
```

Protocolは`Clock`、`IdGenerator`、`Metrics`、`DiscordGateway`、`DiscordPublisher`、`DiscordOutboxRepository`、`EvidenceService`、`CandidateOrderer`、`OpenAIService`、`DebateRepository`とする。`EvidenceService`は質問ごとに最大1つのResponses requestでimmutableな共通Evidenceを準備し、`CandidateOrderer`は投票者ごとの候補順random化を注入可能にする。`DiscordPublisher.publish_persisted`はexpected leased `DebateSnapshot`とattempt内operation IDを受け、永続化・claim済みoutbox operation以外を投稿してはならない。既に`SENT`なら同じrecord、claim不能なら`None`、成功または履歴照合成功なら`SENT` recordを返す。必須Evidence取得不能は`required_evidence_unavailable`としてFAILEDへ保存し、任意取得不能は`optional_unavailable`を保存して続行する。

Scale-to-Zero用に`IngressRepository`、`StatusPublicationRepository`、`RuntimeStateRepository`、`RuntimeActivityInspector`、`EcsRuntimeControl`、`StatusPublicationTrigger`、`RuntimeReconciliationTrigger`、`ParameterReader`、`DebateLookup`をProtocol境界とする。`DiscordIngressApplication.accept()`は署名検証済みの`DiscordHttpOperation`を検証・永続化してからStatus/Reconcilerをbest-effortで起動する。`IngressDrainer`はRuntime READY/BUSYとrecovery完了をgateにFIFO claimを既存accept/retry/cancel use caseへ接続する。`RuntimeReconciler`は3分・15分deadline、lost wake、desired count、30分IDLEを収束させる。Lambda、boto3、API Gateway eventはこれらのProtocolの外側に限定する。

STEP-03の`DebateApplication`は外部SDKをimportせず、これらのProtocolとimmutable `DebateSnapshot`だけを扱う。STEP-04Aでは`DebateSnapshot`へGuild/channel、debate/attempt作成時刻、Discord starter/thread ID、`LeaseGrant`を追加した。STEP-06Aではstarter message、thread、control panel messageを別fieldに分離し、`ACCEPTED`中だけ3 IDを一括bindingでき、同一値の再送は同じ結果、部分bindingまたは別値へのrebindは副作用なしで拒否する。STEP-06Cはadapterがcurrent snapshotを参照するための`get_debate`を公開し、Cancel/Retry commandへoptionalな`expected_attempt_id`を追加した。panel由来操作は永続化済みoperation ID、debate、actorに加えてsource attemptも一致しなければ副作用前に拒否する。STEP-06Dでは`AcceptDebateRequest`と`DebateSnapshot`へ受付時点のimmutable snapshotとして`requester_username`と`requester_display_name`を追加する。`requester_id`だけが認可・request bindingの不変利用者キーであり、名前は将来の認証済みWebアーカイブ表示・検索用である。operation ID replayはquestion/Guild/channel/user IDに加え両name snapshotも一致しなければfail closedとする。retryは元討論のname snapshotを引き継ぎ、Discordから再解決しない。名前はstructured log、metrics、OpenAI request、persona promptへ出さない。

STEP-06CのInteraction controllerは開始したdebate taskをdebate IDごとに所有し、同一debateの重複taskを作らない。Cancelとcontroller closeではtaskをcancelして`gather`し、`CancelledError`をuse case側へ伝播させる。STEP-07Aの`RuntimeLifecycle`はsupervisor、interaction controller、`resume_recoverable`をstructural Protocolで所有し、起動・READY監視・signal・checkpoint・再開を統合する。STEP-07Bでphase前outbox drainを接続し、STEP-07Cで唯一のproduction composition root、実行entry point、process-scoped client所有を実装した。container境界はSTEP-08で実装する。

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

Fargateのscale-down、SIGTERM、Discord長時間切断、強制終了からの引継ぎは新しいphaseにせず、`recovery_state=checkpointed`とlease/fencingで表現する。後続taskが同じphaseを再開する。checkpointは非terminalかつ`recovery_state=none`の場合だけ許可し、checkpoint中のphase遷移、二重checkpoint、checkpointなしのresume、terminal checkpointを拒否する。state更新時刻はtimezone-aware UTCかつ直前時刻以上とし、同一時刻を許容する。`debate_id`と`schema_version`は全遷移で不変とする。

`FAILED`への遷移時は直前の進行phaseを`failed_from_phase`へ保存する。retryはFAILEDから元phaseへ戻すedgeではなく、同じ`DebateId`の下に新しい`AttemptId`を持つstateを作るfactory operationである。新attemptの`retry_of`は直前attempt ID、初期phaseは直前の`failed_from_phase`とし、元FAILED stateを変更しない。domainはsource attempt ID再利用とFAILED以外からのretryを拒否し、debate内の全attempt IDの一意性はrepositoryの条件付きPutで保証する。

遷移はexpected phaseとfencing tokenを条件にしたrepository operationで行い、不一致時は副作用を発生させない。本節を`DebatePhase`とretry aggregate境界の唯一の定義とする。

## 6. 非同期制御

- 3人格処理はphase単位の`asyncio.TaskGroup`で並列化する。
- session全体はactive processing 300秒、通常目標180秒とする。
- Discord HTTP受付の耐久FIFOは20件までとし、`PENDING`/`CLAIMED`/`RETRYING`だけを数える。20件時の21件目は永続化せず即時に混雑応答する。
- RuntimeはFIFO順にclaimするが、既存のglobal 3-slotにより討論は最大3件同時実行する。Ingress claimとDebate lease 60秒は別概念である。
- 各Ingressは受付時刻から3分で非terminalの起動失敗表示、15分でterminal `FAILED`とする。3分後も15分まで自動復旧を続け、実行開始と15分失敗のraceは条件付きwriteで一方だけを成立させる。
- OpenAI同時実行はprocess全体で`Semaphore(6)`に制限する。
- deadlineは`asyncio.timeout()`で管理し、残時間が次のattemptに不足する場合は再試行しない。
- 所有者不明の`asyncio.create_task()`と`CancelledError`の握り潰しを禁止する。
- 同期boto3処理は専用worker threadへ隔離し、client/resourceはbootstrapで一度だけ生成して再利用する。
- `run_debate`はphase taskと所有済みlease heartbeat taskを同時に監督し、20秒ごとにrenewする。phase終了時はheartbeatをcancelして必ずawaitし、heartbeat異常時はphaseをcancelする。
- phase timeoutは`phase_deadline_exceeded`、session timeoutは`session_deadline_exceeded`へ分離する。TaskGroupの1子失敗時は兄弟をcancelし、attemptを`FAILED`へ条件付き保存する。
- Fargate process開始時はIngress Drainer gateを閉じたまま4 Bot READYと必要なGuild Command syncを確認し、`resume_recoverable`を所有taskとして開始してからdrainを開く。HTTP Interaction受付自体はLambdaが常に担い、Gateway READYに依存しない。
- readinessは1秒周期で監視する。1 Botでも切断した時点でRuntimeのIngress claim/drainを即時停止し、60秒連続で復帰しなければinteraction-owned taskとstartup recovery taskをcancel・awaitして同一phaseを`CHECKPOINTED`へ保存する。HTTP永続受付はqueue上限まで継続し、通信断だけで`FAILED`へ遷移しない。
- 再接続時はCommandを再syncせず、4 READYを再確認して`resume_recoverable`を最大1 taskだけ再起動し、RuntimeのIngress claim/drainを再開する。owned taskの例外はdrain gateを閉じてprocess failureとして伝播する。HTTP永続受付はこのgateに依存しない。
- `run_debate`はlease heartbeatを開始した後、`DiscordOutboxDrainer.drain(expected)`をphase処理より先に実行する。outboxの永続retry時刻またはclaim期限を待つ時間はactive-processing 300秒へ含めず、drain完了後のphase処理だけをsession deadlineで囲む。
- outbox recovery中の`RepositoryConflict`はfencing喪失として扱い、旧workerから`FAILED`を保存せず終了する。非retryable Discord failureだけを安定Discord error code付き`FAILED`へ移し、retryable failureは永続再scheduleを待つ。

## 7. Cancel・retry・shutdown

- cancel可能者は開始userまたは`Manage Messages`保持者。新規OpenAI callと未送信outboxを止め、完了済み成果物を保持して`CANCELLED`へ遷移する。
- retry可能状態は`FAILED`だけ。元attemptをimmutableに保ち、同じdebate/threadへ`retry_of=<直前attempt-id>`の新attemptを作る。保存済みartifactを参照し、`failed_from_phase`の未完了operationだけを再実行する。日次開始quotaは増やさず、global leaseは新attempt用に取得する。
- repository readerは旧recordを現行domain schemaへup-convertし、必要な条件付きlazy migrationを完了してからstate-changing use caseへ渡す。`new_retry_attempt()`が継承する`schema_version`はこの現行versionであり、旧versionのまま新Attempt METAを書かない。
- SIGINT/SIGTERM handlerはmain-thread event loopの`loop.add_signal_handler()`で登録し、callbackでは同期的にRuntimeのclaim/drainとinteraction dispatchを閉じてshutdown eventだけを設定する。HTTP Lambdaの永続受付は独立して継続する。同一signalの再入は冪等とする。
- graceful shutdownは新規Ingress claimと新規Debate開始を即時停止し、readiness monitorとdrainerをcancel・awaitし、所有active stateをcheckpointし、outbox/interaction controller、4 Discord clientの順にcloseする。アプリ内部deadlineは90秒、ECS `stopTimeout`は120秒とし、30秒をruntime/OS/log終了余裕として残す。90秒超過は`RuntimeShutdownTimeout`として明示的に失敗する。
- debate checkpointが失敗した場合は成功扱いにせずprocess errorとして返す。ただしcontroller、Discord client、signal handlerの後始末は継続し、未回収taskを残さない。
- STEP-07Bは起動・再接続後のrecoverable attemptでpending outboxをphase再開より先にdrainする。SIGINT/SIGTERM受信後は新規送信せず、drain taskをcancel・awaitして未送信recordを次のfenced ownerへ残す。
- SIGKILLでflushできなくても、lease期限、fencing token、outbox reconciliationで回復できることを前提とする。
- STEP-07Cの実process試験はSIGTERMでRuntime admission停止、checkpoint、interaction/client cleanupを確認し、SIGKILLでcleanupが行われない場合もreplacement processが永続active stateを検出してresumeできることを確認する。container runtimeとECS境界の強制終了はSTEP-08で追加する。
- STEP-03ではprocess cancellationを受けた`run_debate`が現行phaseをcheckpointし、`CancelledError`を再送出する。user cancelは開始userまたはadapterが検証済みの`Manage Messages`権限だけを受理し、repositoryの条件付きterminal遷移で進行中workerの後続writeを拒否する。

### 7.1 完全終了・IDLE・Scale Down

Attemptの`COMPLETED`/`FAILED`/`CANCELLED`だけでは完全終了とみなさない。必須最終回答またはerror/cancel通知のOutbox `SENT`、必須Status updateの配送完了、application-owned task終了が必要である。Runtimeは次の全項目が0の場合だけ`IDLE`へ遷移する。

- `PENDING`/`CLAIMED`/`RETRYING` Ingress、active attempt、application/recovery/checkpoint task、active lease
- pending/claimed Outbox、pending Status update、pending panel refresh
- shutdown/checkpoint中の作業

`idle_since`は最初の完全終了時に一度だけ固定し、`stop_eligible_at=idle_since+30分`とする。Reconcilerは1分周期でactivityとgenerationを再確認し、不変の場合だけ`STOPPING`と`desiredCount=0`へ収束させる。STOPPING中の新規Requestはgenerationを進め、古い停止操作を無効化して`STARTING`/`desiredCount=1`へ戻す。正常なSTOPPING ownerだけがcleanup後に`STOPPED`へ遷移し、それ以外の予期せぬ終了は後続task用の新しい`STARTING`世代を残す。

Runtimeは`stop_eligible_at`の2分前から、同じIDLE generationにつき最大1回だけ帰宅挨拶を先行生成してprocess memoryへ保持する。新規request、generation変更、IDLE解除、生成完了時点の期限超過では候補を破棄する。SIGTERM後は新規workとdrainerを停止してcheckpointを保存した後、Discord clientを閉じる前にRuntime Stateを再取得し、同じgenerationが`STOPPING`、`stopping_at >= stop_eligible_at`、かつ期限到達済みの場合だけ候補を1回consumeして送信する。生成・送信・照合の失敗は安定codeだけを記録して無視し、通常の90秒以内shutdownとscale-to-zeroを継続する。

## 8. 設定と起動validation

ECSが環境変数へ注入する値は`SHITTIM_ENVIRONMENT=production`、`AWS_REGION=ap-northeast-1`、`SHITTIM_DYNAMODB_TABLE`、`SHITTIM_LOG_LEVEL`、任意の直前command schema hash、version付きruntime/persona JSON、OpenAI key、4つのDiscord tokenとする。runtimeからmodelを選択させず、本番Policyはコード上のLuna standardへ固定する。SDK clientを1つも作る前にPydantic strict modelで全値を一括検証し、欠落、未知field、`schema_version`/`config_version`不一致、slot欠落、重複Application ID/token、空allowlist、不正snowflake、promptのUTF-8 3,500 bytes超過があれば安定code `startup_configuration_invalid`で終了する。credential、display name、prompt、元validation messageを標準出力・log・例外へ含めない。

Lambdaは必要最小限の別設定を読み、DiscordIngressにはmoderator ApplicationのPublic Key、Guild/channel allowlist、table名、Status/Reconciler function名を注入する。Public KeyはBot tokenではない。Status Publisherだけが公開Status message用moderator tokenを必要とし、Ingress LambdaはInteraction tokenをhandler scopeから出さず、どのLambdaもOpenAI keyやpersona promptを読まない。

`RuntimeConfig` schema v2はGuild ID、allowed channel IDs、4 Application ID、allowed channel内の必須`farewell_channel_id`を保持する。`PersonaConfig`のslotは`moderator`、`participant-a`、`participant-b`、`participant-c`だけを許可し、display nameとsystem promptを保持する。公開sourceにはschemaと汎用sampleだけを置き、本番値をfileへfallbackしない。

`bootstrap.py`だけがproduction具体依存を組み立てる。processごとにDynamoDB client 1つ、`AsyncOpenAI` 1つ、共有Semaphore 1つ、Discord client 4つ、衝突しないlease owner IDを生成し、repository、publisher、recovery、application、interaction controller、lifecycleへ注入する。`ProductionRuntime.aclose()`はDiscord、OpenAI、DynamoDBの順に全所有clientを冪等にcloseする。`python -m shittim_chest`は設定errorとruntime errorを本文なしの安定codeで終了し、通常の終了は0、設定不正は2、runtime failureは1とする。`.env.example`は変数名とgeneric placeholderだけを公開し、本番IDやsecretを保持しない。

## 9. Coding規約

- Python 3.14.6通常GIL build、`requires-python = ">=3.14,<3.15"`、uv lock固定。開発・CI・releaseはuv 0.11.29を使い、`required-version = ">=0.11.8,<0.12"`で同一minorのDependabot updaterを許可する。`uv_build` lower boundは0.11.29を維持する。
- 全function、method、attributeを型付けし、`src`、`tests`、`tools`の全てで
  `ty check`を通す。`missing-type-argument=error`と
  `possibly-unresolved-reference=warn`を維持し、診断カテゴリ全体の無効化で
  passing resultを作らない。型注釈の網羅性を強めるRuff ANN/PYIは、既存コードへの
  影響を個別評価する将来の品質改善とする。
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
| 2026-07-17 | Python 3.14.6 asyncio Unix signal | https://docs.python.org/3.14/library/asyncio-eventloop.html#unix-signals | main-thread event loopへSIGINT/SIGTERM callbackを登録し、handler内からasyncio stateを安全に更新 |
| 2026-07-17 | Python 3.14.6 asyncio task ownership | https://docs.python.org/3.14/library/asyncio-task.html#task-groups | recovery/checkpoint子taskを所有し、cancel後に必ずawaitする境界を実装 |
| 2026-07-17 | Python 3.14.6 asyncio timeout/cancellation | https://docs.python.org/3.14/library/asyncio-task.html#timeouts | outbox待機をactive-processing timeout外へ分離し、cancelを再送出 |
| 2026-07-17 | Python 3.14.6 subprocess・signals | https://docs.python.org/3.14/library/subprocess.html#popen-objects、https://docs.python.org/3.14/library/asyncio-eventloop.html#unix-signals | 実child processへSIGTERM/SIGKILLを送り、graceful cleanupとreplacement recoveryを検証 |
| 2026-07-17 | Pydantic 2.13.4 model validation | https://pydantic.dev/docs/validation/latest/concepts/models/ | strict/frozen/extra-forbid設定modelとJSON境界を起動前validationへ採用 |
| 2026-07-17 | OpenAI Responses create | https://developers.openai.com/api/reference/resources/responses/methods/create | process-scoped `AsyncOpenAI`を再利用し、既存の`store=false` Responses requestを維持 |
| 2026-07-17 | ECS SSM Parameter injection | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-ssm-paramstore.html | task起動時にversion付きprivate設定とcredentialを環境変数へ注入し、source fallbackを禁止 |
| 2026-07-17 | boto3 DynamoDB client close | https://docs.aws.amazon.com/boto3/latest/reference/services/dynamodb/client/close.html | process-scoped clientをruntime ownerが明示的にclose |
