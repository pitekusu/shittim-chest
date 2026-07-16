# SPDX-License-Identifier: MIT

from pathlib import Path

import pytest
from tools.check_docs import (
    DocumentationError,
    validate_docs_directory,
    validate_license_scope,
    validate_official_sources,
)
from tools.sync_docs import ALLOWED_DESTINATION_EXTRAS, EXPECTED_FILES


def _note(*, body: str = "本文\n") -> str:
    return (
        "---\n"
        "aliases: [Test]\n"
        "tags: [test]\n"
        "status: decided\n"
        "created: 2026-07-16\n"
        "updated: 2026-07-17\n"
        "---\n\n"
        "# Test\n\n"
        f"{body}"
        "\n## 公式資料確認記録\n\n"
        "| 確認日 | 対象version | 公式資料 | 設計への反映 |\n"
        "|---|---|---|---|\n"
        "| 2026-07-17 | test | https://example.com/official | test |\n"
    )


def _docs_directory(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    for filename in EXPECTED_FILES:
        (docs / filename).write_text(_note(), encoding="utf-8")
    for filename in ALLOWED_DESTINATION_EXTRAS:
        (docs / filename).write_text("# Repository documentation\n", encoding="utf-8")
    return docs


def _license_files(tmp_path: Path) -> None:
    (tmp_path / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (tmp_path / "LICENSE-SCOPE.md").write_text(
        "# Scope\n\n`AGENTS.md` and `docs/` remain all rights reserved.\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "LICENSE.md").write_text(
        "# Documentation\n\nAll rights reserved; not licensed for copying.\n",
        encoding="utf-8",
    )


def test_complete_document_set_is_accepted(tmp_path: Path) -> None:
    docs = _docs_directory(tmp_path)

    assert validate_docs_directory(docs) == len(EXPECTED_FILES)


def test_block_list_frontmatter_is_accepted(tmp_path: Path) -> None:
    docs = _docs_directory(tmp_path)
    target = docs / EXPECTED_FILES[0]
    target.write_text(
        _note().replace("aliases: [Test]\n", "aliases:\n  - Test\n"),
        encoding="utf-8",
    )

    assert validate_docs_directory(docs) == len(EXPECTED_FILES)


def test_missing_frontmatter_key_is_rejected(tmp_path: Path) -> None:
    docs = _docs_directory(tmp_path)
    target = docs / EXPECTED_FILES[0]
    target.write_text(_note().replace("status: decided\n", ""), encoding="utf-8")

    with pytest.raises(DocumentationError, match="missing frontmatter keys"):
        validate_docs_directory(docs)


def test_missing_wiki_link_target_is_rejected(tmp_path: Path) -> None:
    docs = _docs_directory(tmp_path)
    target = docs / EXPECTED_FILES[0]
    target.write_text(_note(body="[[存在しない文書]]\n"), encoding="utf-8")

    with pytest.raises(DocumentationError, match="missing Wiki link target"):
        validate_docs_directory(docs)


def test_unclosed_fence_is_rejected(tmp_path: Path) -> None:
    docs = _docs_directory(tmp_path)
    target = docs / EXPECTED_FILES[0]
    target.write_text(_note(body="```python\nprint('test')\n"), encoding="utf-8")

    with pytest.raises(DocumentationError, match="unclosed code fence"):
        validate_docs_directory(docs)


def test_wiki_link_inside_code_is_ignored(tmp_path: Path) -> None:
    docs = _docs_directory(tmp_path)
    target = docs / EXPECTED_FILES[0]
    target.write_text(
        _note(body="`[[存在しない文書]]`\n\n```text\n[[存在しない文書]]\n```\n"),
        encoding="utf-8",
    )

    assert validate_docs_directory(docs) == len(EXPECTED_FILES)


def test_missing_wiki_heading_is_rejected(tmp_path: Path) -> None:
    docs = _docs_directory(tmp_path)
    target = docs / EXPECTED_FILES[0]
    linked_stem = Path(EXPECTED_FILES[1]).stem
    target.write_text(_note(body=f"[[{linked_stem}#存在しない見出し]]\n"), encoding="utf-8")

    with pytest.raises(DocumentationError, match="missing Wiki link heading"):
        validate_docs_directory(docs)


def test_wiki_path_traversal_is_rejected(tmp_path: Path) -> None:
    docs = _docs_directory(tmp_path)
    target = docs / EXPECTED_FILES[0]
    target.write_text(_note(body="[[../secret]]\n"), encoding="utf-8")

    with pytest.raises(DocumentationError, match="invalid flat Wiki link target"):
        validate_docs_directory(docs)


def test_invalid_calendar_date_is_rejected(tmp_path: Path) -> None:
    docs = _docs_directory(tmp_path)
    target = docs / EXPECTED_FILES[0]
    target.write_text(_note().replace("2026-07-16", "2026-99-99"), encoding="utf-8")

    with pytest.raises(DocumentationError, match="invalid created date"):
        validate_docs_directory(docs)


def test_unclosed_fence_in_readme_is_rejected(tmp_path: Path) -> None:
    docs = _docs_directory(tmp_path)
    (docs / "README.md").write_text("# Docs\n\n```text\nunclosed\n", encoding="utf-8")

    with pytest.raises(DocumentationError, match="unclosed code fence"):
        validate_docs_directory(docs)


def test_official_source_row_requires_https_url() -> None:
    text = _note().replace("https://example.com/official", "no-url")

    with pytest.raises(DocumentationError, match="lacks an HTTPS URL"):
        validate_official_sources(text, "10_test.md")


def test_license_scope_is_accepted(tmp_path: Path) -> None:
    _docs_directory(tmp_path)
    _license_files(tmp_path)

    validate_license_scope(tmp_path)


def test_missing_reserved_scope_is_rejected(tmp_path: Path) -> None:
    _docs_directory(tmp_path)
    _license_files(tmp_path)
    (tmp_path / "LICENSE-SCOPE.md").write_text("# Scope\n", encoding="utf-8")

    with pytest.raises(DocumentationError, match="lacks required marker"):
        validate_license_scope(tmp_path)


def test_unexpected_file_is_rejected(tmp_path: Path) -> None:
    docs = _docs_directory(tmp_path)
    (docs / "private.md").write_text("private\n", encoding="utf-8")

    with pytest.raises(DocumentationError, match=r"unexpected: private\.md"):
        validate_docs_directory(docs)


def test_symlinked_note_is_rejected(tmp_path: Path) -> None:
    docs = _docs_directory(tmp_path)
    target = docs / EXPECTED_FILES[0]
    source = tmp_path / "source.md"
    source.write_text(_note(), encoding="utf-8")
    target.unlink()
    target.symlink_to(source)

    with pytest.raises(DocumentationError, match="non-symlink"):
        validate_docs_directory(docs)
