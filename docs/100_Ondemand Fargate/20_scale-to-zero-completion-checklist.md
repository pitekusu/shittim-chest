---
title: Scale-to-Zero Completion Checklist
aliases:
  - Scale-to-Zero 完了判定
  - Scale-to-Zero Acceptance Checklist
tags: [shittim-chest, checklist, acceptance, scale-to-zero]
status: completed
created: 2026-07-28
updated: 2026-08-14
canonical_for: historical-completion
related:
  - "[[10_scale-to-zero-goal]]"
  - "[[30_scale-to-zero-commit-plan]]"
---

# Scale-to-Zero Completion Checklist

## 1. Software and infrastructure

- [x] HTTP Interactionのfreshness／raw-body署名検証
- [x] DynamoDB durable FIFO、deduplication、queue上限
- [x] Runtime state、generation fence、lease、activity counter
- [x] desired countを0／1へ限定するRuntime Reconciler
- [x] ARM64 On-Demand Fargate、通常0、最大1 task
- [x] 4 Bot READY後のrecovery／ingress drain
- [x] graceful SIGTERM、checkpoint、bounded close
- [x] public Statusとthread panelのterminal convergence
- [x] deployment lockとRelease中のproducer fence
- [x] CloudWatch alarm／dashboard／abnormal stop notification

## 2. Verification

- [x] domain／application unit and property tests
- [x] Discord／AWS SDK contract tests
- [x] DynamoDB Local transaction／crash／migration tests
- [x] container health／signal process tests
- [x] CDK assertion、cdk-nag、strict synth
- [x] required CI、CodeQL、supply-chain gates
- [x] signed HTTP受付のproduction acceptance
- [x] task 0→1、通常討論、1→0のproduction acceptance
- [x] duplicate request、continuous request、Status convergence

## 3. Safe terminal state

- [x] 5 stack stable
- [x] ECS desired／running／pending 0／0／0
- [x] Runtime STOPPED、durable activity clear
- [x] deployment lock open
- [x] active／unexecuted release Change Set 0

## 4. Separate operator drills

次はScale-to-Zero implementationの完了条件ではなく、productionへ影響する独立drillである。

- [ ] Bot token rotation
- [ ] DynamoDB PITR restore to a separate table
- [ ] break-glass ECS Exec

current stateは[[20_実装・試験・検証記録]]を参照し、このhistorical checklistへ新featureを追加しない。
