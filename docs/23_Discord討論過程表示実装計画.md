---
aliases:
  - Discord討論過程表示実装記録
tags: [project, discord, application, dynamodb, outbox, retrospective]
status: completed
created: 2026-08-09
updated: 2026-08-14
---

# Discord討論過程表示 実装完了記録

## 1. Goal

最終結果だけでなく、3人の初回意見、再検討した最終案、投票を利用者へ順序どおり表示し、
途中deliveryが欠落したままCOMPLETEDにしないことを目的とした。

## 2. Implemented foundation

- participant／phase単位のGenerationCheckpoint
- PhaseDeliveryPlanとOutbox record schema v2
- attempt全体のglobal delivery sequence
- deterministic 22文字nonceとDiscord history reconciliation
- `ABANDONED`を含むbounded failure／cancel収束
- vote 3件確定前の公開禁止
- moderatorの票数／winner発表とwinner Botの最終発表

OpenAI callのprovider-level exactly-onceには依存せず、永続outputとDiscord表示の重複なしを保証する。
必須表示のpermission／content conflict／deadline failureはattemptをFAILEDへ収束させる。

## 3. Progressive acceptance

1. delivery safety foundation
2. 初回意見3件
3. 最終案3件
4. 匿名投票と確定後の3票公開
5. winner personaによる最終発表

各段階を個別Release／live acceptanceし、正しいBot、順序、重複なし、Python winner、Status terminal、
scale-to-zeroを確認した。

## 4. Current authority

現行のstate、Discord、OpenAI、DynamoDB、test契約は[[10_アプリケーション・Python詳細設計]]、
[[11_Discord詳細設計]]、[[12_OpenAI・プロンプト詳細設計]]、
[[13_DynamoDB・データ整合性詳細設計]]、[[18_試験・品質保証設計]]を正とする。

本書は完了した導入判断の記録であり、静的image baseline、過去PRのcommit budget、旧schemaなどを
current requirementとして再利用しない。
