# 作業ガイド

`shittim_chest`の作業方針。詳細仕様・バージョン・進捗はここへ複製せず、実装と正本文書を参照する。

## 進め方と承認

- 依頼の目的と完了条件を押さえ、必要な修正・検証まで進める。調査だけの依頼では編集しない。
- 会話中の許可・制約を引き継ぐ。許可済みの実装、commit、PR、merge、releaseで承認を求め直さない。
- 読み取り調査、通常の編集、必要な試験、依頼に含まれる不具合修正は追加承認なしで進める。
  確認は、判断に不可欠な情報が不足する場合や、未承認の破壊的操作・範囲拡大が必要な場合に限る。
- 既存の変更を保護する。dirty worktreeだけを理由に止まらず、衝突を避けるか別worktreeを使う。
- 失敗時は原因と実行済みの副作用を確認し、依頼範囲内なら修正して続ける。
  外部writeやworkflowは、前回の状態・cleanupを確認せず重複実行しない。
- 無関係な改善・refactorは同梱しない。P2以下のレビュー指摘だけを理由に、作業を止めたり完了条件を増やさない。
- 報告は変更点・確認結果・残件を簡潔に示す。追加確認が必要なら、その具体的な理由を伝える。

## 守る設計

- 討論はmoderator 1体とparticipant 3体を1 processで動かし、winnerはPythonが決定する。
- user input、Evidence、model outputはuntrusted data。未検証の値を命令や正しい永続recordとして採用しない。
- 依存は`adapters → application → domain`。domainへ外部SDKを持ち込まない。
- Discord署名は未加工bodyで検証し、成功後にJSONを解釈する。
- secret、token、質問、persona本文、署名、private Discord IDをGit・log・artifactへ残さない。
  opaque Debate ID／provider response IDは本文を含まない障害相関に限って使用する。
- 本番はARM64 On-Demand Fargate、平常`desiredCount=0`、最大1 task。

## Android・関連Pythonの実装方針

Androidアプリと、それを支えるPythonの認証・閲覧APIに適用する。

- 実装前に、既採用ライブラリ・標準SDKの高レベルAPIを確認する。適切なものがなければ、保守されている外部ライブラリを検討する。
- 独自実装は、既存APIで必要な安全条件・互換性・性能・端末機能を満たせない部分に限定する。
- 採用時は保守状況・ライセンス・脆弱性・既存バージョンとの互換性・依存の増加・安全な設定の可否を確認する。
- 行数削減や抽象化した見た目を目的に、層・汎用フレームワーク・ラッパーを増やさない。
- 不採用や低レベル処理を残す理由は、該当設計またはコードへ短く記載する。専用の承認工程や大量の比較資料は要求しない。
- 未使用の依存は先行追加せず、必要になるC工程で導入・固定する。用途ごとの採用方針は`docs/29_Androidアプリ設計.md`を参照する。

## Android内部テスト配布の認証と署名

- 配布手順の詳細は`apps/records-android/README.md`の「C20：本人向け内部テスト」を参照する。
  この端末ではPlay Developer API用のサービスアカウントJSONを
  `${XDG_DATA_HOME:-$HOME/.local/share}/shittim-chest/play-api/service-account.json`に保管する。
  JSONはAPI認証専用であり、AABの署名パスワードではない。存在・権限・JSON形式だけを確認し、秘密値を表示しない。
- upload keyは同じくリポジトリ外の
  `${XDG_DATA_HOME:-$HOME/.local/share}/shittim-chest/android-upload/upload-key.p12`に保管する。
  このPKCS12は空パスワードでは開けない。署名には別途その鍵のパスワードが必要。
  この端末ではSecret Serviceの`application=shittim-chest, purpose=android-upload-password`、
  ラベル`Shittim Chest Android upload key`に保存済み。検索結果や秘密値をstdoutへ表示せず、
  プロセス内で取得して鍵を開けるか検証する。`service=...`や`purpose=android-upload`では見つからない。
  見つからなければ「本人が設定したはず」と決めつけず、鍵の作成経緯・安全な保管先を調べる。
  復旧不能なら勝手に別の鍵で署名せず、Playのupload key再設定を含む対応を本人と決める。
  パスワードをチャット・Git・コマンド引数・ログ・サービスアカウントJSONへ書かない。
- Play APIで全trackと提出済みbundleの`versionCode`を確認し、その最大値より大きい番号で現行`main`からAABを作る。
  同じupload keyで署名されたことを確認して内部テストだけへ提出し、Play APIで反映後のtrackを再取得する。
  API認証だけ成功しても署名可能とは判断しない。署名情報が不足すれば提出を止め、未配布と報告する。

## 必要な場所だけ読む

| 対象 | 場所 |
|---|---|
| Core／討論runtime | `src/shittim_chest/`, `tests/` |
| Records API／認証／管理／親愛度／メモリアル | `services/records/`, `contracts/records/` |
| Records Web | `apps/records-web/` |
| Records Android | `apps/records-android/` |
| AWS／CI／運用ツール | `infra/`, `.github/workflows/`, `tools/` |
| 仕様・運用・試験 | `docs/00_*`を索引に、該当節だけ参照 |

依存バージョンと実行コマンドは、各projectのVersion Catalog・lockfile・`pyproject.toml`・`package.json`を正とする。

## 必要十分な検証

- 変更箇所と影響範囲に絞って確認する。文書だけなら差分・参照・公開情報の確認を行い、全テストやbuildを回さない。
- テストは振る舞い・境界条件・不具合の再発を確かめるものに限る。実装の写しや軽微な文言変更用のテストを増やさない。
- Pythonは対象projectで`uv run --frozen`を使い、関連するlint・型・pytestを選ぶ。
  coverageの数値目標は設けず、必要な調査時だけ`--cov`で確認する。
- Webは対象のcheck／testと必要なbuildを選ぶ。見た目・操作の変更は該当画面をPlaywrightで確認する。
  IaCは関連testと対象stackのsynthを選ぶ。全Core infra検証が必要な場合は`npm run check:infra`を使う。
- 成功済みの検証は、新しい変更・失敗・未解決の懸念がなければ繰り返さない。
  full test、DynamoDB Local、image build、auditは影響範囲と既存CIの必須条件に従う。
- 友人同士で使う個人開発アプリとして、実害のある不具合を防ぐ試験を優先する。
  装飾の細部やテスト件数の維持を目的にせず、重い障害訓練は通常CIへ混ぜない。
- 必須CIを勝手に弱めない。外部障害・未実施・skipを成功扱いせず、最後に`git diff --check`を行う。

## 文書とGitHub・本番

- 仕様・動作・運用を変えたら該当文書も更新する。Obsidianが正本、`docs/`はbyte単位のmirror。
  mirrorを直接編集せず、`tools/sync_docs.py`の`--write`と`--check`で同期・確認する。正本の指定は`--source "$SHITTIM_DOCS_SOURCE"`。
  文書変更時は`uv run --frozen python -m tools.check_docs`と`uv run --frozen python tools/check_public_surface.py`を実行する。
- 公開文書にはsecret、private ID、障害相関ID、local絶対pathを含めない。`AGENTS.md`自体はこのrepoで編集する。
- GitHub write前に`gh auth status`と`gh api user --jq '.login'`を確認する。
  `codex/` branch → commit → 通常PR。PRのtitle・本文は日本語、mergeは必須CI・CodeQL成功後のsquashとする。
- release対象は実際の差分と`tools/classify_ci_paths.py`、`docs/15_*`、workflowで決める。
  Core Releaseには同一SHAのRecords Release成功が必要。image変更時はdigest・SBOM・VEX・risk gateの整合を確認する。
- 本番変更・Environment承認はユーザーが許可した範囲で行う。リリース完了まで承認済みなら、承認待ちで止めず検証まで進む。
  Releaseのsmokeに、有料生成・Discord投稿・親愛度resetなど未依頼の受入操作を追加しない。
- CI／Releaseは1 watcherで原則60秒以上の間隔で終端まで監視する。変化のない状態報告を繰り返さない。
