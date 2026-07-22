---
aliases:
  - The Shittim Chest GitHub Discord通知運用設計
tags: [project, shittim-chest, github, discord, operations, notifications]
status: implementing
created: 2026-07-23
updated: 2026-07-23
---

# GitHub・Discord通知運用設計

## 1. 目的・境界

GitHub ActionsからprivateなDiscord Forumの固定投稿へ、CI、Pull Request、dependency、code scanningの運用情報をEmbed形式で集約する。通知からGitHub上の確認要否を判断できることを目的とし、DiscordからGitHubを操作する機能、自動修正、自動mergeは提供しない。

- Discord Bot、Discord Application、常駐process、AWS、外部通知SaaSを使用しない。
- 通常のDiscord Incoming Webhookを使用し、GitHub互換Webhook endpointは使用しない。
- GitHub repositoryは正本であり、Discord通知はat-least-onceの補助表示とする。
- Webhook受理後の応答喪失やworkflow手動再実行では同一内容が重複し得る。通知履歴databaseを追加せずexactly-onceを主張しない。
- Forumはoperatorだけが閲覧できるprivate channelとし、実Webhook URL、thread ID、role IDをrepository、Obsidian、log、artifactへ保存しない。

## 2. Discord論理構成

operatorが1つのForum channel、1つのIncoming Webhook、次の4固定投稿を手動作成する。Webhookは`thread_id` queryで固定投稿のthreadへ送信する。

1. `🚦 CI・定期実行`
2. `🔀 PR・マージ`
3. `🤖 Dependabot`
4. `🛡️ セキュリティ`

通常payloadは`allowed_mentions.parse=[]`とする。Critical/High、CI failure、timeout、`action_required`、監視停止だけ、明示したalert role IDを`allowed_mentions.roles`へ設定する。PR title、branch、commit message、GitHub userなどの外部入力はMarkdownとdisplay-control文字を無害化し、`@everyone`、`@here`、偽装linkを通知へ変換しない。

## 3. GitHub構成

### 3.1 Secret・Variable

Actions Secretは`DISCORD_WEBHOOK_URL`だけとする。Repository Variablesは次とする。

- `DISCORD_NOTIFICATIONS_ENABLED`
- `DISCORD_THREAD_CI`
- `DISCORD_THREAD_PR`
- `DISCORD_THREAD_DEPENDABOT`
- `DISCORD_THREAD_SECURITY`
- `DISCORD_ALERT_ROLE_ID`

初期状態は`DISCORD_NOTIFICATIONS_ENABLED`未設定または`false`とし、他の設定とmanual smoke test準備が完了した後だけ`true`へ変更する。Dependabotの`pull_request_target`ではSecretが公開されないため、WebhookをDependabot Secretへ複製しない。既存DHI registry用Dependabot Secretは用途を分離して維持する。

### 3.2 Workflow分離

- `Discord Workflow Notifications`: repository管理の`CI`、`Dependency Graph`、`Release Tool Versions`完了を`workflow_run`で受ける。workflow nameとpathを組でallowlistし、GitHub管理の同名`Dependency Graph`を除外する。
- `Discord Repository Events`: notification専用の限定`pull_request_target`と`main` pushを扱う。PR head、artifact、cache、PR由来scriptを取得・実行しない。
- `Discord Security Digest`: 毎日09:37 JSTとmanual dispatchでDependabot Alerts、Code Scanning Alerts、Dependabot PR、monitor freshnessを集約する。

通知workflowは元CIと独立し、通知失敗で元CIのconclusionを変更しない。各workflowは`cancel-in-progress=false`、5分または10分のtimeout、必要最小限のread permissionを指定する。

### 3.3 共通実装

`tools/github_discord_notifications/`を標準libraryだけで実装する。GitHub REST APIは`2026-03-10`を指定し、Link header paginationを使用する。Webhook送信はshell evaluationを使わない固定引数の`curl`とし、HTTP 429、5xx、限定した一時transport failureだけを最大4 attemptまで再試行する。Webhook URLとDiscord response本文はlog・exception・Step Summaryへ出力しない。

Discord Embedはtitle 256、description 4,096、field 25件、field name 256、field value 1,024、footer 2,048、全Embed合計6,000文字以内とする。上限超過時はUnicode code point単位で省略し、詳細はGitHub URLへ誘導する。

## 4. STEP-02D段階実装

1. STEP-02D-A: 共通package、workflow completion通知、bounded retry、unit test。local・PR CI合格。
2. STEP-02D-B: PR lifecycle、Dependabot分岐、merge由来push抑制、限定`pull_request_target` policy test。local実装済み。
3. STEP-02D-C: 日次security digest、scan停止検知、Discord/GitHub設定、manual smoke test。

STEP-02D-Aでは通知をdisabledのままmergeし、実Webhook通信、Discord ID、GitHub Secret/Variable変更を行わない。

## 5. 障害対応

- 通知workflow failure: Actions runとStep Summaryを確認する。元CIの結果は通知失敗と分離して扱う。
- Discord 400: thread ID、archived/locked状態、Embed上限、Webhook権限を確認する。自動retryしない。
- Discord 429/5xx: workflow内の有限retry後も失敗した場合だけ手動再実行する。重複可能性を理解してから実行する。
- GitHub API failure: 不完全な件数を正常Digestとして送らず、monitor failureとして扱う。
- Webhook漏えい疑い: DiscordでWebhookを削除・再作成し、Actions Secretだけを更新する。値をIssue、PR、logへ貼らない。
- 一時停止: `DISCORD_NOTIFICATIONS_ENABLED=false`とし、workflow fileやSecretを削除しない。

## 6. 公式資料

| 確認日 | 対象 | URL | 反映内容 |
|---|---|---|---|
| 2026-07-23 | GitHub Actions events | https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows | `workflow_run`と限定`pull_request_target`のdefault-branch trust boundary |
| 2026-07-23 | GitHub workflow permissions | https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax | `actions`、`security-events`、`vulnerability-alerts`等の最小権限 |
| 2026-07-23 | Dependabot on Actions | https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-on-actions | Dependabot起点eventのSecret制約 |
| 2026-07-23 | Discord Webhook | https://docs.discord.com/developers/resources/webhook | `thread_id`、`wait=true`、通常Webhook送信 |
| 2026-07-23 | Discord Message | https://docs.discord.com/developers/resources/message | Embed上限、allowed mentions |
