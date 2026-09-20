# SPDX-License-Identifier: MIT
"""A database cache is version-specific and never bypasses an explicit update."""

import subprocess
import sys
from pathlib import Path

import pytest
from tools import prepare_grype_db


def test_cache_key_is_shared_across_architectures_but_separated_by_tool_version() -> None:
    key, prefix = prepare_grype_db.cache_keys("0.118.0", "Linux", "2026-09-21")
    assert key.startswith(prefix)
    assert prepare_grype_db.cache_keys("0.119.0", "Linux", "2026-09-21")[1] != prefix
    assert prepare_grype_db.cache_keys("0.118.0", "Linux", "2026-09-22")[1] == prefix


@pytest.mark.parametrize("version", ["", "0.118.0\nkey=poisoned", "next"])
def test_invalid_version_cannot_change_the_cache_scope(version: str) -> None:
    with pytest.raises(ValueError, match="invalid Grype version"):
        prepare_grype_db.cache_keys(version, "Linux", "2026-09-21")


def test_database_update_failure_cannot_use_a_stale_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = str(tmp_path / "grype")
    monkeypatch.setattr(sys, "argv", ["prepare", "--binary", binary, "--update"])

    def fail(command: list[str], *, check: bool) -> None:
        assert command == [binary, "db", "update"]
        assert check
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(prepare_grype_db.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        prepare_grype_db.main()
