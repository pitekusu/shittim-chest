---
aliases:
  - The Shittim Chest Discord詳細設計
tags: [project, shittim-chest, discord, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-07-17
---

# Discord詳細設計

## 1. Application構成

| Application | Slash Command | 発言role |
|---|---|---|
| `moderator` | `/shittim`をGuild Command登録 | 受付、進行、集計、終了 |
| participant-a | なし | 初回意見、最終案、投票、採択時決定 |
| participant-b | なし | 同上 |
| participant-c | なし | 同上 |

4 Applicationは個人所有＋2FA、Guild Install限定、Public Bot無効、OAuth2 Code Grant無効とする。Application IDと表示名はprivate `RuntimeConfig`/`PersonaConfig`から読み、public sourceへ固定しない。1 Python process内に4つの独立したDiscord client instanceを生成する。

STEP-06Aは`moderator`、`participant-a`、`participant-b`、`participant-c`を`DiscordBotSlot`としてapplication層に定義し、4 slotの過不足、Application ID重複、不正snowflake、空channel allowlistをDiscord接続前にfail closedとする。Bot token、実Application ID、表示名、persona本文はこの契約へ含めない。

## 2. Guild・channel境界

- version付き`RuntimeConfig.guild_id`だけを許可する。
- `RuntimeConfig.allowed_channel_ids`は非空の通常テキストchannel ID集合とし、未設定時はfail closedする。
- thread内でSlash Commandを直接開始せず、allowlist対象channelの起点messageからPublic Threadを作成する。
- 必要permissionは`View Channel`、`Send Messages`、`Create Public Threads`、`Send Messages in Threads`、`Read Message History`。不要なAdministrator権限を付けない。

## 3. Gateway・Intent

- Gateway Intentは`GUILDS`だけを有効にする。
- Message Contentを含むPrivileged Intentsを無効にする。
- 4 client全てがREADYのときだけ新規討論を受け付ける。
- 1 client切断時は新規受付を閉じる。進行中sessionはcheckpointし、再接続deadline内に戻らなければ`FAILED`へ遷移する。

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

schemaをcanonical JSONへ正規化してSHA-256を保存し、hashが変わったdeploy時だけ同期する。

## 5. Interaction処理

1. Interaction受信から3秒以内にephemeral deferする。
2. Guild、channel種別、allowlist、question、日次quota、lease空き、4 Bot READYを検証する。
3. 失敗時はephemeral follow-upで安定error codeと説明を返す。
4. 成功時は通常channelへ起点messageを投稿し、そこからPublic Threadを作る。
5. thread ID、starter message ID、control panel message IDを別fieldとしてDynamoDBへ保存する。`ACCEPTED`中だけ3 IDを一括bindingし、同じ値の再送は冪等、部分bindingとrebindは拒否する。
6. ephemeral follow-upでthread linkを依頼者へ返す。

## 6. 操作panel

panelはphase、active elapsed、recovery状態、開始者を表示する。component custom IDは`shittim:v1:<debate-id>:<panel-operation-id>:cancel|retry`とし、Discordの上限内に収める。

- Cancel: 開始userまたは`Manage Messages`保持者、かつ進行中状態だけ許可。
- Retry: 開始userまたは`Manage Messages`保持者、かつcurrent attemptが`FAILED`の場合だけ許可する。同じdebate/thread内に新attemptを作り、日次開始quotaへは加算せずglobal leaseを取得する。
- 永続化済みpanel operation ID、Guild、thread、message、debate ID、current attempt IDのいずれかが一致しない操作はephemeral拒否する。独自署名方式は導入しない。
- retry operation IDを冪等keyとし、二重clickは同じnew attemptを返す。new attempt作成後はpanel operation IDとcurrent attempt表示を更新し、古いFAILED panelからの分岐retryを拒否する。
- archived threadは保持し、locked threadは自動解除しない。

## 7. 投稿規則

- `allowed_mentions.parse=[]`相当を全投稿へ適用する。
- 2,000文字以下へ段落優先で決定的に分割し、複数時は`[n/m]`を付与する。
- outboxへprivate runtimeでApplication IDへ解決するgeneric Bot slot、nonce、content hash、chunk sequenceを保存してから送信する。DynamoDB型をapplication層へ置き、Discord adapterとDynamoDB adapterを相互依存させない。
- nonceはUUIDv7の16 byteをpaddingなしbase64urlへ変換した22文字とする。RESTで対応する投稿は`enforce_nonce=true`を使用し、送信後にmessage IDを保存する。
- Discordのnonce重複抑止は直近数分に限定される。長時間停止後やDiscord send成功・DB更新失敗時はnonce、content hash、chunk sequence、thread履歴で照合する。exactly-onceは主張せず、outboxとreconciliationによる表示上の重複抑止を保証する。
- 429はdiscord.pyと`Retry-After`へ従い、application側で同じrequestを独自retryしない。4 clientは`max_ratelimit_timeout=30`で生成し、値が異なるclientをpublisherがfail closedで拒否する。

STEP-06Bはdiscord.py 2.7.1の公開`Thread.send()`を使用する。22文字nonceを渡すと同versionの`handle_message_parameters()`が`enforce_nonce=true`を設定することをcontract testで固定する。`AllowedMentions.none()`のpayloadは`{"parse":[]}`でなければならない。publisherはexactly 4つのdistinct client、expected leased snapshot、attempt内operation IDを受け、永続recordの`get → claim → send/reconcile → mark_sent`だけを実行する。

2回目以降のclaimでは、outbox作成時刻より後のthread履歴を古い順に最大500件調べ、同一Bot author、nonce、content、SHA-256が一致する最古messageを採用する。同一nonceで内容が異なる場合は`DISCORD_OUTBOX_CONFLICT`として送信せず停止する。discord.pyがRetry-Afterを用いた内部retryを使い切った`RateLimited`はその`retry_after`、HTTP 429はheader、408/409/5xxは30秒の既定値でoutboxを1回だけ再scheduleし、publisher自身は同じHTTP requestをloop retryしない。Discordのchannel解決、履歴照合、sendは45秒でtimeoutし、共有outbox claim 60秒より前に30秒後へ再scheduleする。DynamoDBの`mark_sent`はDiscord timeout外でfenced writeとして実行する。権限不足、thread消失、wrong Guild、locked thread、その他4xxは自動再送・自動unlockしない。

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

## 10. STEP-06分割境界

- STEP-06A（完了、PR `#27`、merge commit `47af41f`）: SDK非依存runtime/identity/error/outbox/panel契約、決定的message split、UUIDv7 nonce、SHA-256、custom ID codec、Discord context binding、schema v5。
- STEP-06B（local実装・試験済み、PR前）: discord.py 2.7.1 publisher、outbox claim/send/complete、`allowed_mentions`、`enforce_nonce`、SDK rate limit、長時間停止後reconciliation。
- STEP-06C（未実装）: 4 client、READY gate、Guild Command、3秒以内defer、starter/thread/panel、Cancel/Retry Interaction。
