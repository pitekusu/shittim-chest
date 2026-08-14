---
title: Scale-to-Zero Goal
aliases:
  - シッテムの箱 Scale-to-Zero 完了記録
  - Discord HTTP Interaction / Fargate Scale-to-Zero
tags: [shittim-chest, aws, discord, ecs, fargate, dynamodb, scale-to-zero]
status: completed
created: 2026-07-28
updated: 2026-08-14
---

# Scale-to-Zero Goal 完了記録

## 1. Outcome

Discord Gateway常駐のFargate Spot serviceを廃止し、署名付きHTTP ingressと耐久FIFOを入口にして、
ARM64 On-Demand Fargateを通常0、必要時1、最大1 taskで動かす構成をproductionへ導入した。

## 2. Enduring invariants

- Discordはtask 0でもAPI Gateway／Ingress LambdaへInteractionを送れる。
- Ingressはraw-body署名検証後、DynamoDBへdurable acceptanceして短く応答する。
- Reconcilerはdurable work、Runtime state、lease、deployment lockからdesired 0／1を決める。
- accepted workはFargate process停止後も新ownerがcheckpointから再開できる。
- queueは最大20、startup warningは3分、terminal deadlineは15分。
- 全討論、Outbox、Status、leaseがclearになってから30分idleで停止する。
- normal taskはOn-Demand Fargate、ARM64、512 CPU／1,024 MiB、public IP、最大1 task。
- task 0とBot offlineはSTOPPED時の正常状態である。30分のIDLE待機中はtaskと4 Botが稼働する。

## 3. Safety boundaries

- user question、Interaction token、signature、raw bodyをlogへ出さない。
- runtime generationとfencing tokenが一致しないwriteを拒否する。
- deployment lock中はproducer writeとscale mutationを止める。
- shutdownはnew admissionを閉じ、checkpoint、lease、client closeをboundedに実行する。
- manual ECS desired count変更を通常運用に使わない。

## 4. Current authorities

current requirementと実装は次を正とする。

- overall requirement: [[01_要求仕様書・基本設計書]]
- Python lifecycle: [[10_アプリケーション・Python詳細設計]]
- DynamoDB／fencing: [[13_DynamoDB・データ整合性詳細設計]]
- ECS／CDK: [[14_AWS・CDK詳細設計]]
- operations: [[17_運用保守・監視・障害対応設計]]
- test: [[18_試験・品質保証設計]]

本書へ新しいfeatureや作業手順を追記しない。過去のsubagent指示、commit分割、旧Fargate Spot案は
Git historyを参照する。
