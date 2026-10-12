"""Tests for public repository safety checks."""

from pathlib import Path

import pytest
from tools.check_public_surface import (
    DENY_PATTERNS,
    REPOSITORY_ROOT,
    candidate_files,
    denied_labels,
)


def test_aws_account_pattern_detects_real_id_but_allows_documented_dummy() -> None:
    pattern = DENY_PATTERNS["AWS account ID"]
    account_id = b"123456" + b"789012"

    assert pattern.search(b'account = "' + account_id + b'"')
    assert pattern.search(b'account = "000000000000"') is None
    assert pattern.search(b"sha256:abc123456789012def") is None


def test_snowflake_pattern_detects_delimited_id_but_not_registry_hash() -> None:
    pattern = DENY_PATTERNS["Discord snowflake"]
    digits = b"1" * 17

    assert pattern.search(b'guild_id = "' + digits + b'"')
    assert pattern.search(b"/guilds/" + digits + b"/commands")
    assert pattern.search(b"abc" + digits + b"def") is None


def test_email_pattern_detects_address_but_allows_package_versions() -> None:
    pattern = DENY_PATTERNS["email address"]

    assert pattern.search(b"operator" + b"@" + b"example.com")
    assert pattern.search(b"vite-plus@0.2.9") is None
    assert pattern.search(b"voidzero-dev/vite-plus-core@0.2.9") is None


def test_user_approved_contact_is_allowed_only_in_its_designated_source() -> None:
    contact_path = Path("apps/records-web/src/legalContact.ts")
    contact_source = (REPOSITORY_ROOT / contact_path).read_bytes()

    assert DENY_PATTERNS["email address"].search(contact_source)
    assert denied_labels(contact_path, contact_source) == []
    assert denied_labels(Path("apps/records-web/src/routes/PrivacyPage.tsx"), contact_source) == [
        "email address"
    ]
    assert denied_labels(Path("docs/contact.md"), contact_source) == ["email address"]


def test_unapproved_email_is_rejected_even_beside_the_approved_contact() -> None:
    contact_path = Path("apps/records-web/src/legalContact.ts")
    contact_source = (REPOSITORY_ROOT / contact_path).read_bytes()
    other_email = b"private" + b"@" + b"example.com"

    assert denied_labels(contact_path, other_email) == ["email address"]
    assert denied_labels(contact_path, contact_source + b"\n" + other_email) == ["email address"]


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
