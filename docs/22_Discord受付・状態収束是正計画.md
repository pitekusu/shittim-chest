---
aliases:
  - Discord受付・状態収束是正記録
tags: [project, discord, lambda, dynamodb, scale-to-zero, retrospective]
status: completed
created: 2026-08-04
updated: 2026-08-14
---

# Discord受付・状態収束是正 完了記録

## 1. Background

production受入で次の3問題を確認し、独立した変更として是正した。

1. Runtime起動済みの2件目も`STARTING`と表示され、別debateの遅延更新が混在した。
2. cold invocationがDiscordのinitial response期限を超え、永続受付後も利用者へ失敗表示が出た。
3. thread panelがterminalでもchannel Statusが`ACCEPTED`に残った。

## 2. Resolution

- 公開StatusをDebate ID／Attempt IDでfenceし、共有Runtime状態から直接上書きしない。
- signed HTTP ingressを短い受付に限定し、SnapStart aliasでcold pathを短縮した。
- Status Publisherをdesired／observed stateの冪等reconcilerとし、terminalを優先して収束させた。
- stale event、別attempt、missing message、Discord errorを分類し、無関係なdebateを更新しない。

## 3. Acceptance

- 初回Interactionで「アプリケーションが応答しませんでした」を表示しない。
- 連続した2件が互いのStatusを変更しない。
- thread panelとchannel Statusが同じCOMPLETED／FAILED／CANCELLEDへ収束する。
- 失敗時もdurable activityとECSが最終的にsafe stateへ戻る。

現行契約は[[11_Discord詳細設計]]、[[13_DynamoDB・データ整合性詳細設計]]、
[[17_運用保守・監視・障害対応設計]]を正とする。本書へ新仕様を追記しない。
