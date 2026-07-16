"""Tests for public repository safety checks."""

from pathlib import Path

import pytest
from tools.check_public_surface import DENY_PATTERNS, candidate_files


def test_snowflake_pattern_detects_delimited_id_but_not_registry_hash() -> None:
    pattern = DENY_PATTERNS["Discord snowflake"]
    digits = b"1" * 17

    assert pattern.search(b'guild_id = "' + digits + b'"')
    assert pattern.search(b"/guilds/" + digits + b"/commands")
    assert pattern.search(b"abc" + digits + b"def") is None


def test_candidate_files_follow_the_git_supplied_file_set(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("safe = True\n", encoding="utf-8")
    environment = tmp_path / ".venv"
    environment.mkdir()
    (environment / "lib64").symlink_to(environment, target_is_directory=True)

    assert candidate_files(tmp_path, relative_paths=(Path("source.py"),)) == [source]


def test_candidate_files_include_tracked_files_under_generated_names(tmp_path: Path) -> None:
    distribution = tmp_path / "dist"
    distribution.mkdir()
    tracked = distribution / "force-added.txt"
    tracked.write_text("must be scanned\n", encoding="utf-8")

    assert candidate_files(
        tmp_path,
        relative_paths=(Path("dist/force-added.txt"),),
    ) == [tracked]


def test_candidate_files_reject_source_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("safe = True\n", encoding="utf-8")
    link = tmp_path / "linked.py"
    link.symlink_to(source)

    with pytest.raises(RuntimeError, match="symlink is not allowed"):
        candidate_files(tmp_path, relative_paths=(Path("linked.py"),))
