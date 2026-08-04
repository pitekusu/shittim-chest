---
aliases:
  - The Shittim Chest Discord詳細設計
tags: [project, shittim-chest, discord, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-07-28
---

# Discord詳細設計

## 1. Application構成

| Application | Slash Command | 発言role |
|---|---|---|
| `moderator` | `/shittim`をGuild Command登録 | 受付、進行、集計、終了 |
| participant-a | なし | 初回意見、最終案、投票、採択時決定 |
| participant-b | なし | 同上 |
| participant-c | なし | 同上 |

4 Applicationは個人所有＋2FA、Guild Install限定、Public Bot無効、OAuth2 Code Grant無効とする。Application IDと表示名はprivate `RuntimeConfig`/`PersonaConfig`から読み、public sourceへ固定しない。Fargate task稼働中だけ1 Python process内に4つの独立したDiscord Gateway client instanceを生成する。`desiredCount=0`では4 Botのオフライン表示を許容し、常駐Gateway専用processは追加しない。

STEP-06Aは`moderator`、`participant-a`、`participant-b`、`participant-c`を`DiscordBotSlot`としてapplication層に定義し、4 slotの過不足、Application ID重複、不正snowflake、空channel allowlistをDiscord接続前にfail closedとする。Bot token、実Application ID、表示名、persona本文はこの契約へ含めない。

## 2. Guild・channel境界

- version付き`RuntimeConfig.guild_id`だけを許可する。
- `RuntimeConfig.allowed_channel_ids`は非空の通常テキストchannel ID集合とし、未設定時はfail closedする。
- thread内でSlash Commandを直接開始せず、allowlist対象channelの起点messageからPublic Threadを作成する。
- 必要permissionは`View Channel`、`Send Messages`、`Create Public Threads`、`Send Messages in Threads`、`Read Message History`。不要なAdministrator権限を付けない。

## 3. HTTP Interaction・Gateway・Intent

- `/shittim`、Retry、Cancelを含むApplication Command/Componentの受付経路は常にDiscord Interactions Endpoint→API Gateway HTTP API→DiscordIngress Lambdaとする。Gateway callbackで同じ操作を受けない。
- Gateway Intentは`GUILDS`だけを有効にする。
- Message Contentを含むPrivileged Intentsを無効にする。
- HTTP受付はGatewayのREADY状態にかかわらず耐久FIFOへ保存できる。ECS Runtimeは4 client全てがREADY、command schema確認済み、recovery完了のときだけIngressをclaimして討論を開始する。
- 1 client切断時は新規Ingress claimを即時閉じる。60秒以内に4 READYへ戻れば進行を継続し、60秒連続で戻らなければ進行中sessionを同一phaseの`CHECKPOINTED`へ退避する。通信断だけでは`FAILED`へ遷移せず、4 READY復帰後にfenced leaseを再取得して自動resumeする。

## 4. Command schema

```text
name: shittim
type: CHAT_INPUT
scope: configured Guild
option:
  name: question
  type: STRING
  required: true
  min_length: 1
  max_length: 1000
```

schemaをcanonical JSONへ正規化してSHA-256を保存し、hashが変わったdeploy時だけ同期する。CommandはFargate停止中もGuildへ登録済みのままとし、オフライン表示を理由に削除・再登録しない。

STEP-06Cではcommandを設定済みGuildへだけlocal登録し、schema hashが前回値と異なると明示されたときだけ`CommandTree.sync(guild=...)`を呼ぶ。startupごとの自動同期は行わない。

## 5. Interaction処理

### 5.1 HTTP受付境界

1. API Gateway HTTP API payload v2からPOSTのraw bodyを復元する。base64の有無を厳密に扱い、raw bodyは64 KiBで事前拒否する。
2. header名をcase-insensitiveに一意化し、`X-Signature-Ed25519`、`X-Signature-Timestamp`、変更していないraw body bytesを使う。JSON parseより先にEd25519署名と現在UTCから前後5分以内のtimestampを検証し、欠落・重複・不正hex・長さ・署名・過去/未来replayを401で拒否する。
3. 署名検証後だけUTF-8 JSONをparseし、duplicate key、非有限数、未知Interactionをfail closedとする。`PING`はDynamoDB、Lambda invoke、ECSへ触れず即時`PONG` (`{"type":1}`) を返す。
4. `APPLICATION_COMMAND`と`MESSAGE_COMPONENT`をSDK非依存の型付きinputへ変換し、moderator Application ID、Guild、channel/thread、allowlist、question、component context、`requester_id`認可を検証する。
5. queue counter 20件上限、Ingress Request、active pointer、operation result、公開Status publicationをDynamoDB transactionで先に永続化する。永続化不明な場合は成功応答を返さない。
6. 永続化後だけStatus PublisherとRuntime Reconcilerをbest-effortで起動し、即時のInteraction callback type 4を`flags=64`、`allowed_mentions.parse=[]`で返す。停止中/起動中は起動中、READY/BUSYは受付済み、上限時は20件混雑を表示する。

DiscordのInteraction tokenは初回callbackに必要なhandler-scopeの一時値とし、domain/application model、DynamoDB、queue、Status publication、logへ渡さない。HTTP handlerはDiscord Gateway、discord.py client、participant token、OpenAIを初期化しない。Lambda入口から永続受付を終えるsoft deadlineは2.2秒とし、新しいAWS SDK callをその0.1秒前に閉じ、Discordの3秒初回応答までの余白を確保する。

### 5.2 Runtime処理と公開Status

Fargate Runtimeは4 client READY、command schema確認、recoverable debate列挙・初期復旧完了後だけFIFOをclaimする。新規討論では通常channelの起点message、Public Thread、control panelを作成し、thread/starter/panelの3 IDを`ACCEPTED`中に一括bindingする。部分bindingと異なるrebindは拒否する。

DiscordStatusPublisher Lambdaはmoderator Bot tokenによるRESTで公開Status messageを作成/更新する。状態は`PENDING`、`STARTING`、`READY`、`STARTUP_TIMEOUT`、`RECOVERED`、`ACCEPTED`、`COMPLETED`、`CANCELLED`、`REJECTED`、`TERMINAL_FAILED`とし、message IDとdesired/delivered stateを永続化する。Status PublisherはAI討論、Gateway接続、Runtime起動判定、Interaction token保存を行わない。

starter、thread、panel作成時はmentionsを無効にし、starterにInteraction ID、panelにAttemptId由来nonceを付与する。応答消失後の同一Interaction replayでは、moderator自身のauthor ID、nonce、完全一致contentを通常channel/thread履歴から照合して再利用する。同一nonceでcontentが異なる場合はfail closedとする。3 resourceの永続binding前に作成が失敗した場合は受付済みattemptをCANCELLEDへするbest-effort cleanupを行う。

## 6. 操作panel

panelはphase、active elapsed、recovery状態、開始者を表示する。component custom IDは`shittim:v1:<debate-id>:<panel-operation-id>:cancel|retry`とし、Discordの上限内に収める。

- Cancel/Retry componentはSlash Commandと同じ署名付きHTTP Interaction経路で受け、Gateway callbackと二重受付しない。
- Cancel: 開始userまたは`Manage Messages`保持者、かつ進行中状態だけ許可。
- Retry: 開始userまたは`Manage Messages`保持者、かつcurrent attemptが`FAILED`の場合だけ許可する。同じdebate/thread内に新attemptを作り、日次開始quotaへは加算せずglobal leaseを取得する。
- 永続化済みpanel operation ID、Guild、thread、message、debate ID、current attempt IDのいずれかが一致しない操作はephemeral拒否する。独自署名方式は導入しない。
- retry operation IDを冪等keyとし、二重clickは同じnew attemptを返す。new attempt作成後はpanel operation IDとcurrent attempt表示を更新し、古いFAILED panelからの分岐retryを拒否する。
- archived threadは保持し、locked threadは自動解除しない。
- Cancel/Retryのoperation IDはsource AttemptIdの32桁hexとaction suffixから決定的に作る。custom IDから復元したattemptがcurrent snapshotと一致しなければ、古いpanelのclickとして拒否する。
- componentはmoderator Application ID、Guild ID、永続thread ID、control panel message ID、debate IDを全て照合してからuse caseを呼ぶ。error responseへ例外本文、token、runtime値を含めない。

## 7. 投稿規則

- `allowed_mentions.parse=[]`相当を全投稿へ適用する。
- 2,000文字以下へ段落優先で決定的に分割し、複数時は`[n/m]`を付与する。
- outboxへprivate runtimeでApplication IDへ解決するgeneric Bot slot、nonce、content hash、chunk sequenceを保存してから送信する。DynamoDB型をapplication層へ置き、Discord adapterとDynamoDB adapterを相互依存させない。
- nonceはUUIDv7の16 byteをpaddingなしbase64urlへ変換した22文字とする。RESTで対応する投稿は`enforce_nonce=true`を使用し、送信後にmessage IDを保存する。
- Discordのnonce重複抑止は直近数分に限定される。長時間停止後やDiscord send成功・DB更新失敗時はnonce、content hash、chunk sequence、thread履歴で照合する。exactly-onceは主張せず、outboxとreconciliationによる表示上の重複抑止を保証する。
- 429はdiscord.pyと`Retry-After`へ従い、application側で同じrequestを独自retryしない。4 clientは`max_ratelimit_timeout=300`で生成し、値が異なるclientをpublisherがfail closedで拒否する。discord.py 2.7.1はresetまでの時間がこの値を超えると、bucketに残数があってもHTTP送信前に`RateLimited`を発生させるため、Discordのthread-createが返す300秒windowを下回る値へ戻してはならない。実際のapplication操作はingress context 45秒、panel refresh 30秒、outbox delivery 45秒の各timeoutで有界化する。

STEP-06Bはdiscord.py 2.7.1の公開`Thread.send()`を使用する。22文字nonceを渡すと同versionの`handle_message_parameters()`が`enforce_nonce=true`を設定することをcontract testで固定する。`AllowedMentions.none()`のpayloadは`{"parse":[]}`でなければならない。publisherはexactly 4つのdistinct client、expected leased snapshot、attempt内operation IDを受け、永続recordの`get → claim → send/reconcile → mark_sent`だけを実行する。

2回目以降のclaimでは、outbox作成時刻より後のthread履歴を古い順に最大500件調べ、同一Bot author、nonce、content、SHA-256が一致する最古messageを採用する。同一nonceで内容が異なる場合は`DISCORD_OUTBOX_CONFLICT`として送信せず停止する。discord.pyがRetry-Afterを用いた内部retryを使い切った`RateLimited`はその`retry_after`、HTTP 429はheader、408/409/5xxは30秒の既定値でoutboxを1回だけ再scheduleし、publisher自身は同じHTTP requestをloop retryしない。Discordのchannel解決、履歴照合、sendは45秒でtimeoutし、共有outbox claim 60秒より前に30秒後へ再scheduleする。DynamoDBの`mark_sent`はDiscord timeout外でfenced writeとして実行する。権限不足、thread消失、wrong Guild、locked thread、その他4xxは自動再送・自動unlockしない。

HTTP ingressからRuntimeがstarter message、thread、panelを準備する経路でも、discord.pyの`RateLimited.retry_after`またはHTTP 429の`Retry-After`をcontent-freeな秒数としてapplication境界へ渡す。Ingress Requestは固定5秒より長いprovider指定値を`next_attempt_at`へ保存し、その時刻より前に再claimしない。構造化logはerror code、delivery attempt、実際のretry delay、delay source、およびallowlist済みの`discord_operation`を記録する。rate-limitの直接原因を再試験する間は、同じoperationに対する成功応答と429応答から`X-RateLimit-Scope`、`X-RateLimit-Limit`、`X-RateLimit-Remaining`、`X-RateLimit-Reset-After`を厳格なenum／数値へ変換して記録し、`X-RateLimit-Bucket`は同一bucketの相関に必要なSHA-256だけを記録する。欠落headerは`null`、不正値はallowlist済みheader名だけで区別し、raw header値、response body、質問、token、URL、Discord IDは記録しない。

HTTP ingressの`STARTING`応答は、既存taskのIDLE再開を新規ECS起動と誤表示しないよう「依頼を受け付け、処理開始を準備中」と表示する。同期応答時点では将来のDiscord `Retry-After`を取得できないため、推定待ち時間は通常約1分以内、連続実行時はDiscord制限により約5分かかる場合があるという静的な目安とし、保証値または固定rate limitとして扱わない。

STEP-07Bの`DiscordOutboxRecovery`はlease取得済みattemptの全未送信operationを強整合Queryで取得し、`chunk_sequence`とoperation IDの保存済み順序で1件ずつpublisherへ渡す。`next_retry_at`未来値と未失効claimはその永続時刻までasync待機し、pollによるbusy loopを行わない。retryable provider errorはpublisherが保存したscheduleを再読込みし、同じHTTP requestを直接retryしない。非retryable errorは安定Discord codeを保持してattemptをFAILEDへする。shutdown cancellationは再送出し、送信途中recordを次回のclaim/reconciliationへ残す。

## 8. Error code

| Code | user表示 | 再試行 |
|---|---|---|
| `DISCORD_WRONG_GUILD` | このサーバーでは利用できません | 不可 |
| `DISCORD_CHANNEL_NOT_ALLOWED` | このチャンネルでは利用できません | 不可 |
| `DISCORD_BOTS_NOT_READY` | Botの準備が完了していません | 可 |
| `DISCORD_THREAD_CREATE_FAILED` | 討論スレッドを作成できませんでした | 可 |
| `DISCORD_THREAD_LOCKED` | スレッドがロックされています | 管理者対応後 |
| `DISCORD_PERMISSION_DENIED` | 必要な権限がありません | 管理者対応後 |
| `DISCORD_THREAD_UNAVAILABLE` | 討論スレッドを確認できません | 管理者対応後 |
| `DISCORD_OUTBOX_NOT_FOUND` | 投稿データを確認できません | 不可 |
| `DISCORD_OUTBOX_CONFLICT` | 投稿データの整合性を確認できません | 管理者対応後 |
| `DISCORD_RATE_LIMITED` | Discordの利用制限が継続しています | 可 |
| `DISCORD_UNAVAILABLE` | Discordへ接続できません | 可 |
| `DISCORD_DELIVERY_REJECTED` | Discordが投稿を受理しませんでした | 入力・設定確認後 |

## 9. 公式資料確認記録

| 確認日 | 対象version/service | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-07-16 | Interactions | https://docs.discord.com/developers/interactions/receiving-and-responding | 3秒deadline、follow-up |
| 2026-07-16 | Commands | https://docs.discord.com/developers/interactions/application-commands | Guild Command、option length |
| 2026-07-16 | Gateway | https://docs.discord.com/developers/events/gateway | READY、Intent |
| 2026-07-16 | Message | https://docs.discord.com/developers/resources/message | 2,000文字、nonce、allowed mentions |
| 2026-07-16 | Rate limits | https://docs.discord.com/developers/topics/rate-limits | `Retry-After` |
| 2026-07-16 | Threads | https://docs.discord.com/developers/topics/threads | Public Thread、archive/lock |
| 2026-07-17 | discord.py 2.7.1 | https://pypi.org/project/discord.py/ | 現行releaseとPython 3.14互換範囲を確認。SDK依存追加はSTEP-06B以降 |
| 2026-07-17 | Interactions | https://docs.discord.com/developers/interactions/receiving-and-responding | initial responseは3秒以内。STEP-06Aはerror codeとSDK非依存契約だけを実装 |
| 2026-07-17 | Application Commands | https://docs.discord.com/developers/interactions/application-commands | STRING optionのmin/max lengthを再確認 |
| 2026-07-17 | Message | https://docs.discord.com/developers/resources/message | content 2,000文字、nonce最大25文字、`enforce_nonce`は直近数分、allowed mentionsを再確認 |
| 2026-07-17 | Components | https://docs.discord.com/developers/components/reference | `custom_id` 1〜100文字、一message内一意。v1 codecを100文字以内に固定 |
| 2026-07-17 | Discord Message API | https://docs.discord.com/developers/resources/message | `allowed_mentions.parse=[]`、nonce最大25、`enforce_nonce`の直近数分重複抑止をpublisher contractへ反映 |
| 2026-07-17 | Discord rate limits | https://docs.discord.com/developers/topics/rate-limits | 429の`Retry-After`をhard codeせずdiscord.pyへ委譲し、SDK枯渇後だけoutbox再schedule |
| 2026-07-17 | Message Content Intent | https://docs.discord.com/developers/events/gateway#message-content-intent | privileged IntentなしでもApplication自身の投稿内容は取得可能なため履歴reconciliationへ使用 |
| 2026-07-17 | discord.py v2.7.1 source | https://github.com/Rapptz/discord.py/blob/v2.7.1/discord/http.py#L141-L208 | nonce指定時に`enforce_nonce=true`となるSDK shapeをoffline contract testで固定 |
| 2026-07-17 | discord.py client source | https://github.com/Rapptz/discord.py/blob/v2.7.1/discord/client.py | `max_ratelimit_timeout`を30秒へ明示し、無制限待機を禁止 |
| 2026-07-17 | discord.py errors source | https://github.com/Rapptz/discord.py/blob/v2.7.1/discord/errors.py | pre-emptive rate-limit上限超過時の`RateLimited.retry_after`をoutbox delayへ使用 |
| 2026-07-17 | Interactions | https://docs.discord.com/developers/interactions/receiving-and-responding | 当時のSTEP-06C Gateway callbackでinitial response 3秒を確認。2026-07-28以降は下記HTTP type 4応答に置換え、Gatewayでのdeferは現行受付に使用しない |
| 2026-07-17 | Application Commands | https://docs.discord.com/developers/interactions/application-commands | Guild command、STRING 1〜1000文字、deploy時明示syncを実装 |
| 2026-07-17 | Components | https://docs.discord.com/developers/components/reference | custom ID 100文字上限とcomponent context検証を再確認 |
| 2026-07-17 | discord.py v2.7.1 Interaction source | https://github.com/Rapptz/discord.py/blob/v2.7.1/discord/interactions.py | 当時のSTEP-06C Gateway callbackのdefer/edit contract。現行はHTTP-only commandがfail closedであることと、Ingress Runtimeのthread/panel作成だけをoffline検査 |
| 2026-07-17 | discord.py Client readiness | https://discordpy.readthedocs.io/en/stable/api.html#discord.Client.wait_until_ready | `is_ready()`をprocess gateと1秒監視へ使用し、4 client全てのREADYをRuntime claim/drain条件として維持。HTTP受付とは分離 |
| 2026-07-17 | Discord Message API | https://docs.discord.com/developers/resources/message | Get Channel Messagesの新しい順、100件/page、Read Message History要件とCreate Messageのnonce/enforce_nonceを再確認し、SDK履歴iteratorによるreconciliationを維持 |
| 2026-07-28 | Discord Interactions Endpoint | https://docs.discord.com/developers/interactions/receiving-and-responding | HTTP endpointのEd25519署名、timestamp、PING/PONG、3秒初回応答、ephemeral callbackをScale-to-Zero受付境界へ反映 |

## 10. STEP-06分割境界

- STEP-06A（完了、PR `#27`、merge commit `47af41f`）: SDK非依存runtime/identity/error/outbox/panel契約、決定的message split、UUIDv7 nonce、SHA-256、custom ID codec、Discord context binding、schema v5。
- STEP-06B（完了、PR `#30`、merge commit `96a1ace`）: discord.py 2.7.1 publisher、outbox claim/send/complete、`allowed_mentions`、`enforce_nonce`、SDK rate limit、長時間停止後reconciliation。
- STEP-06C（完了、PR `#31`、merge commit `9799cb9`）: 4 client、GUILDS-only Intent、READY gate、Guild Command、starter/Public Thread/panel、履歴reconciliation、attempt-bound Cancel/Retry、controller task ownershipの基盤。当時のGateway Interaction callbackはScale-to-Zero実装で署名付きHTTP受付へ置き換え、現在は二重dispatchしない。
- STEP-06D: interaction受付時に`interaction.user.name`と`interaction.user.display_name`をsnapshot保存する。Guild Memberではdisplay_nameにnickを反映する。`str(user)`やREST再取得は使わず、Interactionに含まれる値だけを用いる。Discord上の表示文言は変更しない。
- STEP-07A（完了、PR `#33`、merge commit `0f386f5`）: process signal、fail-closed受付gate、起動時`resume_recoverable`、60秒Gateway切断checkpoint、再接続resume、90秒graceful shutdown。
- STEP-07B（完了、PR `#34`、merge commit `04bbda0`）: pending全件取得、永続retry/claim待機、順序drain、lease heartbeat、nonretryable error/fencing/cancellation処理。
- STEP-07C（local実装済み）: strictなprivate runtime/persona設定からexactly 4 clientを生成し、共通gateway、READY gate、interaction controller、lifecycleへ注入するproduction composition。実process SIGTERM/SIGKILLをoffline検証済み。
- Scale-to-Zero（local/CI実装済み）: API Gateway v2 raw request復元、Ed25519/timestamp/replay検証、PING即時応答、token非永続化、耐久Status publication、稼働中だけのGatewayとIngress drainをofflineで検証済み。実Interactions Endpointの登録、実Bot token、Discord通信、AWS deploy/smokeは未実施。
