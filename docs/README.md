# Design document mirror

このdirectoryは、operatorが管理するpublic-safeなObsidian正本20文書の一方向mirrorです。
要求、現行設計、運用、試験、1.0の検証状態を領域別に分けています。変更履歴の完全な
複製ではなく、現在の契約を短時間で確認できることを目的とします。

入口は[00_シッテムの箱_ドキュメント索引.md](00_シッテムの箱_ドキュメント索引.md)です。
完了済みの是正計画とScale-to-Zero資料は、現行設計を重複させない短い完了記録として
残しています。

番号付き文書を直接編集しないでください。正本を更新後、repository rootで同期します。

```sh
export SHITTIM_DOCS_SOURCE="<public-obsidian-project-folder>"
python tools/sync_docs.py --write --source "$SHITTIM_DOCS_SOURCE"
python tools/sync_docs.py --check --source "$SHITTIM_DOCS_SOURCE"
uv run --frozen python -m tools.check_docs
```

同期toolは17個のroot文書と`100_Ondemand Fargate/`配下3文書をexact setとして扱い、
symlink、余分なfile、代表的なcredential、Discord snowflake、email address、local home pathを
拒否します。production identifierとprivate personaはmirror対象外です。

文書はrepositoryのMIT License対象外です。[LICENSE.md](LICENSE.md)を参照してください。
