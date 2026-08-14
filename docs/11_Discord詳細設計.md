---
aliases:
  - The Shittim Chest Discord詳細設計
tags: [project, shittim-chest, discord, detailed-design]
status: production-1.0
created: 2026-07-16
updated: 2026-08-14
---

# Discord詳細設計

## 1. Identity and permissions

| Slot | Command | Public role |
|---|---|---|
| moderator | Guild `/shittim` | 受付、Status、票数、winner、system notice |
| participant-a/b/c | なし | 初回意見、最終案、投票、winner発表、帰宅挨拶 |

4 Botは1 processで動くが、token、Application ID、display name、personaをslot間で混同しない。
Guildとallowed channelを起動時／操作時に検証し、participantへcommandを登録しない。

## 2. HTTP Interaction ingress

- moderator ApplicationのInteraction EndpointだけをAPI Gatewayへ向ける。
- timestamp freshnessとEd25519署名を未加工bodyへ検証してからJSON parseする。
- `PING`へPONG、既知の`/shittim`とcontrol componentだけを受ける。
- command questionは1〜1,000文字。Interaction IDの条件付きwriteで重複を除く。
- durable acceptance後、Discordの期限内に短い受付結果を返す。Lambda cold startはSnapStartで抑える。
- Gatewayへcommandが誤配信された場合は処理せずfail closedとする。

## 3. Thread and public status

- 通常text channelにdebate単位のpublic threadを作る。
- channel Statusは受付、起動、処理、terminalを表し、thread panelと同じattemptへ収束する。
- panelのcustom IDはversion、operation、Debate ID、Attempt IDを100文字以内で保持する。
- Stopはcurrent nonterminal attemptだけ、Retryはcurrent FAILED attemptだけに適用する。
- stale button、別attempt、別Guild／Channel、権限不足をwrite前に拒否する。

## 4. Debate messages

表示順序は次で固定する。

1. participant 3人の初回意見
2. participant 3人の最終案
3. 3票確定後の投票者名、投票先、理由
4. moderatorの票数とwinner
5. winnerの勝利の言葉、最終決定、実行案、注意点

投票中はcandidateを匿名IDにし、3票確定前のvote Discord writeを0にする。最終結果のwinnerは
保存済みPython resultと一致するparticipantだけが投稿する。

## 5. Ordered Outbox delivery

- messageは2,000文字以内へsanitization後chunkし、participant phaseは1人最大8 chunks、
  terminalは最大20 chunksとする。
- operation identityはAttempt ID、phase、Bot slot、local chunk sequenceの組で一意にする。
- 22文字base64url nonceを同じidentityから導出する。
- global delivery sequenceの小さい未完了operationだけをclaimし、1件ずつPOSTする。
- Discord clientの`max_ratelimit_timeout`は300秒。429はSDKの`Retry-After`処理後に分類する。
- timeout後はhistoryをnonce、author、channel、contentで照合し、完全一致なら`SENT`にする。
- content mismatch、unknown message、permission failureは成功扱いしない。
- 最大3 delivery attemptまたはdeadline後は残件を`ABANDONED`へ収束し、attemptをFAILEDにする。

user／role mentionは`AllowedMentions.none()`で無効化し、embedを抑止する。model本文は改行、
Unicode、Markdownをdisplay-safeに正規化する。

## 6. Command synchronization

Runtimeは4 Bot READY後、deployから渡されたprevious command schema hashとlocal hashを比較し、
差分がある場合だけmoderator Guild Commandをsyncする。participant command、global command、
permission APIは使用しない。

## 7. Farewell exception

帰宅挨拶はdebate Outboxへ入れないbest-effort例外である。

- stop予定約5分前に、選ばれたparticipantが設定済み通常text channelへ1件送る。
- 同じIDLE identityであることを生成後とretry前に確認する。
- 同じnonceで最大2回送信し、timeout時はhistoryを照合して重複を避ける。
- permission、channel／author／content conflictではretryしない。
- new work、generation変更、IDLE解除では未送信結果を破棄する。
- 失敗してもSIGTERMやscale-downを遅らせない。

## 8. Logging boundary

token、Interaction token、raw body、signature、question、message content、URL、query、Guild／Channel IDを
logへ出さない。stable error code、attempt count、rate-limit headerの数値的状態などcontent-freeな
diagnosticだけを残す。

## 9. 公式資料確認記録

| 確認日 | 対象 | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-08-14 | Discord Interactions | https://docs.discord.com/developers/interactions/receiving-and-responding | 署名、PING、initial response |
| 2026-08-14 | Application Commands | https://docs.discord.com/developers/interactions/application-commands | Guild commandとoption制約 |
| 2026-08-14 | Message Resource | https://docs.discord.com/developers/resources/message | content、nonce、allowed mentions |
| 2026-08-14 | Rate Limits | https://docs.discord.com/developers/topics/rate-limits | Retry-Afterとbounded retry |
| 2026-08-14 | Threads | https://docs.discord.com/developers/topics/threads | public threadと権限境界 |
