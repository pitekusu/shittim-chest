# The Shittim Chest

[![CI](https://github.com/pitekusu/shittim-chest/actions/workflows/ci.yml/badge.svg)](https://github.com/pitekusu/shittim-chest/actions/workflows/ci.yml)
[![CodeQL](https://github.com/pitekusu/shittim-chest/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/pitekusu/shittim-chest/security/code-scanning)
[![Infrastructure Drift](https://github.com/pitekusu/shittim-chest/actions/workflows/drift.yml/badge.svg?branch=main)](https://github.com/pitekusu/shittim-chest/actions/workflows/drift.yml)
[![License: MIT](https://img.shields.io/badge/Source-MIT-blue.svg)](LICENSE-SCOPE.md)

**シッテムの箱**は、Discordで3人のAI参加者がひとつの議題を話し合い、
投票で結論をまとめる、仲間内向けのマルチエージェント討論Botです。

`/shittim`へ質問を送ると、個性の異なる3人が初回意見を述べ、互いの案を踏まえて
最終案を作り、匿名で投票します。得票数と採点から勝者を決めるのはLLMではなく
Pythonです。最後は勝者が、自分の人格と口調で結論を発表します。

## できること

- 3人の初回意見、再検討した最終案、投票理由、集計、最終決定を順番に表示する
- 必要な議題では共通Evidence AgentがWeb検索し、全参加者へ同じ根拠を渡す
- 検索が不要、または一時的に利用できない場合も、推測をEvidenceへ混ぜず討論を続ける
- 受付・生成・Discord投稿をDynamoDBへ永続化し、中断後も安全に再開する
- アイドル停止予定の約5分前に参加者のひとりが帰宅挨拶を投稿し、約30分で実行環境を停止する
- Discord上の停止・再試行操作と、チャンネル／スレッドの状態表示を提供する

## 討論の流れ

```text
/shittim
  → 受付と公開スレッド作成
  → 共通Evidenceの準備（Web検索はAIが必要性を判断）
  → 3人の初回意見
  → 3人の最終案
  → 3票を確定してから投票先と理由を公開
  → Pythonが勝者を決定
  → 得票結果と、勝者本人による最終発表
```

参加者ごとの名前と人格は非公開設定です。投票中は候補を匿名化し、全票が確定する
まで他者の投票内容を見せません。

## しくみ

Discord Interactionは署名付きHTTP endpointで受け付けます。受付内容はDynamoDBへ
先に保存され、ARM64 On-Demand Fargateの実行環境が必要な間だけ1 task起動します。
平常時は`desiredCount=0`です。

Python applicationが討論の状態遷移、生成checkpoint、投票、勝者決定、順序付き
Discord Outboxを管理します。OpenAI Responses APIは各意見とEvidence生成を担当しますが、
進行や勝敗を決めません。

本番基盤はAWS CDKで管理し、GitHub ActionsのProduction ReleaseがimageのSBOM、VEX、
脆弱性gate、署名、attestation、CloudFormation Change Setを検証してdeployします。

## Privacy and security

- Bot token、API key、Guild／Channel／Application ID、persona本文はGitへ保存しません。
- Discordの署名は未加工bodyに対して検証してからJSONを解釈します。
- 質問、モデル出力、Bot／API tokenの値、署名、persona本文を運用logへ記録しません。
- user input、Web Evidence、モデル出力はすべてuntrusted dataとして扱います。
- production imageはdigest固定し、供給網とdeploy identityを検証します。

これは少人数のprivate Guild向けの個人プロジェクトです。外部向けSLAや一般公開Botの
提供は目的としていません。

## Documentation

設計文書は[ドキュメント索引](docs/00_シッテムの箱_ドキュメント索引.md)から参照できます。
現在の要求、実装境界、試験、運用、1.0の検証状態を領域別に分けています。

開発へ参加する場合は[CONTRIBUTING.md](CONTRIBUTING.md)、脆弱性の報告は
[SECURITY.md](SECURITY.md)を参照してください。

## License

source、IaC、tool、sampleはMIT Licenseです。`docs/`と`AGENTS.md`は対象外です。
詳細は[LICENSE-SCOPE.md](LICENSE-SCOPE.md)を参照してください。
