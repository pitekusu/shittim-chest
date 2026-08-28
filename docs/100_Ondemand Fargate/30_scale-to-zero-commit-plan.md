---
title: Scale-to-Zero Commit Plan
aliases:
  - Scale-to-Zero 導入履歴
  - Scale-to-Zero Commit・Push計画
tags: [shittim-chest, git, github, checkpoint, scale-to-zero]
status: completed
created: 2026-07-28
updated: 2026-08-28
canonical_for: historical-implementation
related:
  - "[[10_scale-to-zero-goal]]"
  - "[[20_scale-to-zero-completion-checklist]]"
---

# Scale-to-Zero Commit Plan 完了記録

## 1. Implemented sequence

Scale-to-Zeroは次の依存順で導入し、各境界をtestとPRで固定した。

1. SDK非依存のRuntime state／activity／fencing model
2. DynamoDB codec、transaction、lease、FIFO
3. signed HTTP Interaction ingressとStatus publication
4. Runtime ReconcilerとECS desired 0／1
5. process lifecycle、Gateway readiness、recovery、health
6. CDK resource、IAM、monitoring、cost governance
7. Production Release lock、Change Set、structural smoke、cleanup
8. live acceptanceとincident correction

## 2. Completion policy

- 実装、test、docs、CI evidenceを同じPR boundaryで整合させる。
- source変更を含むimageはcanonical CI／Releaseでproduction targetを実測する。
- merge後は新しいmain SHAからProduction Releaseし、failed runをrerunしない。
- Environment承認、deploy、live acceptanceを独立した明示工程として扱う。
- failure時は直接原因とsafe stateを確認し、別SHAの修正へ切り替える。

## 3. Maintenance handoff

この導入計画は完了した。今後の変更順は[[19_実装計画・トレーサビリティ]]、Release契約は
[[15_GitHub・CI-CD詳細設計]]、運用は[[17_運用保守・監視・障害対応設計]]を正とする。

旧commit番号、budget、再開prompt、subagent指示はcurrent operationに不要なため本文から削除した。
完全な履歴はGit commit／Pull Request／Actionsを参照する。
