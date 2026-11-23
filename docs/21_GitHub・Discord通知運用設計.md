---
aliases:
  - The Shittim Chest GitHub Discord通知運用設計
tags: [project, shittim-chest, github, discord, operations, notifications]
status: current
created: 2026-07-23
updated: 2026-11-23
---

# GitHub・Discord通知運用設計

[ドキュメント索引へ戻る](00_シッテムの箱_ドキュメント索引.md)

## 1. 目的と位置付け

GitHub ActionsはPR、対象ワークフロー、セキュリティ情報を専用のIncoming Webhookで非公開Discordフォーラムへ通知する。
通知は重複し得る。結果の確認と対応はGitHubのChecks、Security、Actionsで行う。
配信は[CI/CD設計](15_GitHub・CI-CD詳細設計.md)、障害調査は[運用保守設計](17_運用保守・監視・障害対応設計.md)を参照する。

## 2. 通知するイベント

| ワークフロー | 起動条件 | 内容・除外 |
|---|---|---|
| [discord-repository-events.yml](https://github.com/pitekusu/shittim-chest/blob/main/.github/workflows/discord-repository-events.yml) | PRの開始・再開・レビュー準備完了・終了、`main`へのプッシュ | PR状態・作成者・参照先。Dependabot PRは個別通知から除外 |
| [discord-workflow-run.yml](https://github.com/pitekusu/shittim-chest/blob/main/.github/workflows/discord-workflow-run.yml) | 許可リスト内のワークフロー完了 | 結果、実行ID、SHA、実行者、URLを用途別投稿へ振分け |
| [discord-security-digest.yml](https://github.com/pitekusu/shittim-chest/blob/main/.github/workflows/discord-security-digest.yml) | 日次、または`main`から手動実行 | Dependabot、CodeQL、シークレット検査状態の集約 |

PRに由来するmainへのプッシュは関連PRを調べて重複通知を抑止する。
直接プッシュまたは由来を確認できないプッシュは、その区別を付けて通知する。
IssueやGitHub Releaseのライフサイクルを直接通知する設定は持たない。

ワークフロー完了通知の対象は `workflow_run.workflows` を参照する。全通知は `DISCORD_NOTIFICATIONS_ENABLED=true` のときに実行する。

## 3. 入力と配送の保護

| 境界 | 保護 |
|---|---|
| 実行するコード | 信頼された既定ブランチから取得。PR側コードを秘密付きで実行しない |
| Webhook URL | GitHub Actionsのシークレットに保存。ログ・例外へ値や応答本文を出さない |
| 投稿先 | 設定済みフォーラム投稿/スレッドへ固定し、元IDをリポジトリへ保存しない |
| PRのタイトル等 | 信頼できない文字列として整形し、コマンドとして解釈しない |
| 埋め込みメッセージとリンク | 文字数・URLを検証し、GitHub URL以外をリンクとして採用しない |
| メンション | 利用者/ロールへの通知を抑止 |
| セキュリティ情報 | 秘密値、非公開アラート本文、コード断片は送らない |

## 4. 配送失敗の扱い

一時的な通信失敗、HTTP 429、5xxの場合に再試行し、送信は最大4試行とする。
各試行は接続10秒、リクエスト全体30秒が上限。待機は `Retry-After` を最大30秒まで使い、
取得できなければ指数的に延ばす。

通知ワークフローは元のCI・配信を参照する読み取り権限で動かす。
日次集約のページ取得未完了、未知スキーマ、権限不足は成功扱いしない。

## 5. 変更と動作確認

- ワークフローの試験では権限、イベント許可リスト、信頼されたソース取得、秘密の扱い、メンション、URLを確認する。
- Webhook交換が依頼された場合はGitHubのシークレットを更新し、許可された通知確認を1回だけ行う。
  手動起動できるのは日次集約ワークフローであり、通知経路の確認にアプリケーションやOpenAIを呼ばない。
- フォーラム構造を変更した場合は投稿先メタデータだけを変更する。Webhook URLや実IDを文書へ転記しない。
- 新しいワークフローの通知を追加するときは、対象リストと振分け、必要な権限、通知ループの防止を確認する。

## 実装への入口

| 対象 | 実装 |
|---|---|
| 通知の整形・分類・配送 | [github_discord_notifications](https://github.com/pitekusu/shittim-chest/tree/main/tools/github_discord_notifications) |
| ワークフロー契約検査 | [check_notification_workflows.py](https://github.com/pitekusu/shittim-chest/blob/main/tools/check_notification_workflows.py) |
