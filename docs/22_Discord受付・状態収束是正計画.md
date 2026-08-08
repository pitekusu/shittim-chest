---
aliases:
  - Discord受付・状態収束是正計画
tags: [project, discord, aws, lambda, dynamodb, scale-to-zero]
status: pr-c-in-ci
created: 2026-08-04
updated: 2026-08-08
---

# Discord受付・状態収束是正計画

## 1. 目的

実環境の連続した`/shittim`受入で確認した次の3問題を、原因とrollback境界を混在させず3本のPRで順番に是正する。

1. 起動済みRuntimeで受けた依頼にも公開Status Messageが`STARTING`と表示され、前の依頼の遅延更新が次の依頼と同時に見える。
2. 初回cold invocationがDiscordの3秒initial response期限を超え、永続受付と処理は成功しても利用者側に「アプリケーションが応答しませんでした」と表示される。
3. thread内の操作panelは`completed`でも、channelの公開Status Messageが`ACCEPTED`のまま残る。

各PRは単独でrevert可能にし、required CIとCodeQLが成功してから次のPRへ進む。複数問題を同じPRで同時修正しない。

## 2. 確定済み原因

| 問題 | 直接原因 | 性質 |
|---|---|---|
| 誤った`STARTING`と複数依頼の同時更新 | 新規Ingressが常に`STARTING`でstatus publicationを作り、Fargateの`mark_accepted`後は即時通知せず1分Reconcilerまたは次の受付triggerでまとめて収束する | 表示状態と通知timing |
| 初回Interaction timeout | Discord Ingress Lambdaのcold initと同期受付処理の合計がDiscordの3秒期限を超える。Lambda/APIは成功しているため処理は継続する | serverless cold start |
| channelが`ACCEPTED`のまま | debate終端時はthread panelを収束させるが、元Ingress Requestをterminal化するproduction callerがない | 永続状態整合性 |

## 3. 採用決定

- cold start対策はDiscord Ingress LambdaだけにAWS Lambda SnapStartを使用する。
- API Gatewayはpublished versionを指す固定aliasへ統合し、`$LATEST`を本番受付に使用しない。
- Runtime taskからDiscord Status Publisher Lambdaへの`lambda:InvokeFunction`を、当該function ARNだけに限定して許可する。
- Invokeは低遅延化のhintであり、DynamoDBのdesired statusを正本とする。Invoke失敗時は既存の1分Reconcilerで回復する。
- Interaction token、raw body、署名、質問本文は永続化・log出力しない。
- Provisioned Concurrencyは採用しない。SnapStartと同時使用しない。

## 4. 実装順序

### PR-A: terminal status convergence

最初に、threadとchannelで終端状態が分裂する正しさの問題を修正する。

- terminal Outboxの全件`SENT`確認後、debate terminal化、元Ingress terminal化、公開Status publicationのrearm、lease/counter解放を同じDynamoDB transactionで確定する。
- `origin_ingress_interaction_id`、debate ID、attempt ID、現在のIngress statusを条件式で照合する。
- `COMPLETED`、`FAILED`、`CANCELLED`を既存の公開状態へ決定的に写像する。
- transaction commit後、該当Interaction IDだけをStatus Publisherへ非同期Invokeする。
- Invoke失敗はterminal commitをrollbackしない。pending publicationをReconcilerが再取得できる状態に保つ。
- crash、transaction replay、Invoke失敗でも二重counter更新や別Ingress更新を発生させない。

PR-AはPR `#155`で実装・merge・Production Release済みである。2026-08-05の連続live受入では、1件目と2件目のchannel／threadがそれぞれ`COMPLETED`へ収束し、2件目の受付が1件目の公開状態を変更しなかった。channel反映には約1分を要したため、正しさとInteraction分離は合格、低遅延化はPR-Bの対象として残す。

### PR-B: runtime-aware initial status

PR-Aの安定後、公開初期状態とACCEPTED通知timingを修正する。

- `READY`、`BUSY`、`IDLE`は起動済みRuntimeとして扱い、`STARTING`を表示しない。
- Runtime不在、`STOPPED`、実際の起動収束中だけ`STARTING`を使用する。
- Status Publisherの初回kick前に、該当Ingressだけのdesired stateを確定する。
- Fargateが`mark_accepted`した直後に、PR-Aで追加した限定Invoke経路で該当statusを通知する。
- 別Ingressの状態やpublication versionを変更しない。

PR-BはPR `#157`で実装・merge・Production Release済みである。2026-08-08の連続live受入では、2件目受付時にRuntime Stateが`BUSY`、ECSがdesired/running/pending `1/1/0`のまま、別Status Messageを約2秒で作成した。両publicationは各1回の配送で独立して`COMPLETED`へ収束し、Status Publisher失敗、Discord API 429、Reconciler失敗は0件だった。

### PR-C: Discord Ingress SnapStart

PR-Bの安定後、初回Interactionの応答期限を修正する。

- Discord Ingress LambdaだけSnapStartをpublished versionへ適用する。
- content-addressed Lambda bundleの変更ごとに新versionが作成され、固定aliasがそのversionへ移る構成にする。
- API Gateway integrationとLambda permissionはaliasを参照する。
- Ingress handlerのaggregate package importを直接module importへ縮小する。
- snapshot作成時にAWS SDK client、認証情報、SSM parameter値、request dataを保持しない。
- 現行のEd25519検証、type 4 callback、永続受付前の成功応答禁止、2.2秒application soft deadlineを維持する。
- content-freeなcold/restore区分と処理区間時間だけを記録し、質問、token、署名、raw bodyを含めない。

PR-CはDraft PR `#158`で実装し、shared Lambda ZIPの実測SHA-256をCloudFormation Parameterとして渡し、Discord IngressだけにSnapStartを設定したpublished versionを作成する。固定`live` aliasとAPI Gateway permission/integrationは同versionだけを参照し、bundle checksum変更時にversionを置換する。Ingress moduleはaggregate adapter importを廃止し、SDK client、SSM値、request dataをhandler開始前に生成しない。第1 canonical CIで両image config digest、SBOM、VEX、risk gateの対応を確認し、両baselineを同じPRで一括更新した。transitive `nanoid`の新規High findingはaudit例外を追加せず、安全版へのlockfile更新で解消して最終CIを行う。

## 5. 状態契約

| 観測／処理 | 公開状態 |
|---|---|
| Runtime不在、`STOPPED`、起動収束中 | `STARTING` |
| Runtime `READY`、`BUSY`、`IDLE`で未claim | `READY` |
| requestをclaimしdebate/attemptへ束縛済み | `ACCEPTED` |
| debate正常終了 | `COMPLETED` |
| debate失敗 | `TERMINAL_FAILED` |
| debate取消 | `CANCELLED` |

Status Publisherの配送失敗はdesired stateを巻き戻さない。次の`/shittim`受付をstatus収束の前提にしない。

## 6. IAM境界

Runtime taskへ追加できる権限は、production Discord Status Publisher Lambdaのexact ARNに対する`lambda:InvokeFunction`だけとする。

- wildcard Action、wildcard Resource、managed policyを追加しない。
- Status Publisher以外のLambda、ECS、Discord、OpenAI権限を追加しない。
- Invoke payloadはInteraction IDだけとし、質問本文、Discord token、署名、raw bodyを含めない。
- Invoke失敗時の正しさはDynamoDBとReconcilerで維持する。

## 7. 試験境界

### PR-A

- debateと元Ingressのterminal化が同じtransactionに含まれる。
- origin、debate、attemptの不一致をfail closedで拒否する。
- transaction replayでcounterとpublicationが重複しない。
- commit直前・直後の障害からterminal statusへ回復する。
- Invoke失敗後もReconcilerがpending publicationを配信できる。
- unrelated Ingressを更新しない。

### PR-B

- `IDLE`、`READY`、`BUSY`で2件目が`STARTING`にならない。
- inactive Runtimeでは`STARTING`を維持する。
- ACCEPTEDが次のcommandを待たずに通知される。
- 各Interactionのstatus publicationが独立する。

### PR-C

- SnapStartはDiscord Ingress Lambdaだけに設定される。
- API Gatewayはaliasを呼び、`$LATEST`を呼ばない。
- bundle変更でversion identityが変わる。
- snapshot時にSDK client、secret、parameter値を生成しない。
- initial callbackのdeadline、署名検証、privacy contractを維持する。

各PRでfocused Python/DynamoDB Local/CDK/cdk-nag/workflow policyを実行し、required CIとCodeQLをterminalまで確認する。実Discord、Production Release、AWS live変更は各PR実装とは別工程とする。

## 8. Image baseline

`src/`変更でimage config digestが変わるPRは、canonical CIと同じbuild条件でproductionとbreak-glassを同時に実測する。片方だけが変わっても同じ測定の両baselineを同じPRで更新する。manifest digestから推測せず、過去runや別exporterの値を流用しない。更新前に両imageのSBOM、VEX、risk gateとの対応を確認する。CI専用`fault-test`は対象外とする。

## 9. 停止条件

- 1本のPRで別問題の修正が必要になる。
- DynamoDB schema migrationまたは既存データの手動書換えが必要になる。
- wildcard IAMまたはInteraction token保存が必要になる。
- required CI、CodeQL、risk gateに直接原因不明の失敗が出る。
- image baselineをcanonical CIの実測なしで更新する必要が出る。
- Production Release、AWS live write、Discord live writeが必要になる。

停止時は追加修正、rerun、再dispatchをせず、直接原因と安全状態を報告する。

## 10. 完了条件

- PR-A、PR-B、PR-Cが別々のDraft PRとして実装される。
- 各PRが単一問題に限定され、required CIとCodeQLが成功する。
- thread、channel、DynamoDBのterminal stateが一致する。
- 起動済みRuntimeの受付を`STARTING`と表示しない。
- cold invocationでもDiscord initial response期限を満たす。
- 状態通知は低遅延hintとdurable reconciliationの両方を持つ。
- AWS／Discord／OpenAI live操作とProduction Releaseは各PR実装中0回とする。
