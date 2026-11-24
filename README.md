# THE SHITTIM CHEST — シッテムの箱

[![CI](https://github.com/pitekusu/shittim-chest/actions/workflows/ci.yml/badge.svg)](https://github.com/pitekusu/shittim-chest/actions/workflows/ci.yml)
[![Records CI](https://github.com/pitekusu/shittim-chest/actions/workflows/records-ci.yml/badge.svg)](https://github.com/pitekusu/shittim-chest/actions/workflows/records-ci.yml)
[![CodeQL](https://github.com/pitekusu/shittim-chest/actions/workflows/codeql.yml/badge.svg)](https://github.com/pitekusu/shittim-chest/actions/workflows/codeql.yml)

3人のAIに相談し、議論の過程や結果を友人同士で楽しむアプリです。3人が議題に意見と最終案を出して匿名投票します。Pythonが票を集計して勝者を決め、勝者が結論をまとめます。

[Webを開く](https://shittim.pitekusu.dev/) · [設計文書](docs/00_シッテムの箱_ドキュメント索引.md)

## できること

- **Discord** では `/shittim` で討論を依頼し、スレッドで議論を追えます。完了した討論への追加質問（更問）や、生徒ガチャでのカード獲得もできます。
- **Web** では議論の記録、ランキング、親愛度、概算費用を確認できます。週次のモモトークや、サーバーで獲得したカードの生徒図鑑も閲覧できます。親愛度で解放されるメモリアルロビーでは、自分の記念画像と思い出の文章を作れます。
- **Android** では討論や更問を依頼し、進行状況や議論の記録を確認できます。モモトークや生徒図鑑も閲覧でき、端末に同期した議事録は許可期間内にオフラインで読めます。

WebとAndroidでサーバーの記録を見るには、対象のDiscordサーバーへの参加とDiscordログインが必要です。

## 開発

| 構成 | コード |
| --- | --- |
| Discord受付・討論ランタイム（Python、OpenAI Responses API、AWS） | [Core](src/shittim_chest/) |
| 認証・記録・集計API（Python、AWS Lambda） | [Records API](services/records/)・[API仕様](contracts/records/v1/openapi.json) |
| Webクライアント（React、Vite Plus） | [Records Web](apps/records-web/) |
| Androidクライアント（Kotlin、Jetpack Compose） | [Records Android](apps/records-android/) |
| AWS構成とCI/CD | [AWS CDK](infra/)・[GitHub Actions](.github/workflows/) |

Pythonの依存関係はリポジトリのルートで `uv sync --frozen`（Core）と `uv sync --project services/records --frozen`（Records API）で準備します。Pythonのバージョンは [.python-version](.python-version) を参照してください。

Webは [.node-version](.node-version) と [package.json](apps/records-web/package.json) に合わせてツールを用意し、`apps/records-web/` で `vp install --frozen-lockfile`、`pnpm run dev` の順に実行します。Androidのセットアップとビルド手順は [AndroidのREADME](apps/records-android/README.md#開発環境) にあります。

## 文書と参加方法

公開仕様・設計・運用手順の正本は、GitHubのこのリポジトリにある [docs/](docs/) です。機能ごとの参照先は [ドキュメント索引](docs/00_シッテムの箱_ドキュメント索引.md)、変更・検証の手順は [文書の更新ガイド](docs/README.md) にあります。

[コントリビューション](CONTRIBUTING.md) · [脆弱性の報告](SECURITY.md) · [ライセンスの適用範囲](LICENSE-SCOPE.md)

ソースコード・インフラ構成・ツール・公開サンプルはMITライセンスです。`docs/` と `AGENTS.md` の権利は留保されています。
