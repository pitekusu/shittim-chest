---
aliases:
  - The Shittim Chest GitHub Discord通知運用設計
tags: [project, shittim-chest, github, discord, operations, notifications]
status: production-1.0
created: 2026-07-23
updated: 2026-08-14
---

# GitHub・Discord通知運用設計

## 1. Purpose and boundary

GitHub Actions、Pull Request、dependency、code scanningの運用情報をprivate Discord Forumの
固定postへEmbed通知する。GitHubが正本であり、Discordはat-least-onceの補助表示である。

通知からGitHubを操作する機能、自動修正、自動merge、AWS／application Botの利用は行わない。

## 2. Workflows

| Workflow | Event | Content |
|---|---|---|
| `discord-workflow-run.yml` | selected workflow completion | conclusion、run、SHA、actor、URL |
| `discord-repository-events.yml` | PR／Issue／release等 | event、state、author、URL |
| `discord-security-digest.yml` | daily／manual | Dependabot、CodeQL、secret status summary |

対象workflowのexact listはYAMLを正とする。通知workflow自身の失敗は別workflowの再実行理由にしない。

## 3. Delivery contract

- GitHub Incoming Webhook用secretをActions secretとして保持し、runner logへ出さない。
- Forum post／thread targetを固定し、untrusted title／bodyをmessage commandとして解釈しない。
- Discord content、embed field、URL、mentionをallowlist形式へ整形する。
- user／role mentionを抑止し、GitHub URL以外をリンクとして採用しない。
- event deliveryは重複し得る。Discord通知をexactly-onceの台帳として使わない。

## 4. Failure behavior

- GitHub check／Security tab／Actions logを最終判断に使う。
- 429／5xx／network failureはworkflowのbounded retryに従う。
- notification failureでCI／Release本体の結論を変更しない。
- digest取得のpagination incomplete、unknown schema、permission failureをsuccessにしない。
- secret、private alert detail、code snippetをDiscordへ送らない。

## 5. Verification

- workflow policy testでpermission、event allowlist、secret handling、mention抑止、URLを検証する。
- manual workflowは通知経路だけを試し、AWS／application／OpenAIへwriteしない。
- 監視停止はGitHub Actionsのworkflow statusから検知し、Discordだけを観測源にしない。

## 6. Operations

webhook rotation時はGitHub secretを更新してmanual notificationを1回確認する。Forum構造を変更した場合は
target metadataだけを更新し、repositoryへ実IDを保存しない。
