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

## 必要な場所だけ読む

| 対象 | 場所 |
|---|---|
| Core／討論runtime | `src/shittim_chest/`, `tests/` |
| Records API／認証／管理／親愛度／メモリアル | `services/records/`, `contracts/records/` |
| Records Web | `apps/records-web/` |
| AWS／CI／運用ツール | `infra/`, `.github/workflows/`, `tools/` |
| 仕様・運用・試験 | `docs/00_*`を索引に、該当節だけ参照 |

依存バージョンと実行コマンドは、各projectのlockfile・`pyproject.toml`・`package.json`を正とする。

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
