"""Nested canonical specification handling for the documentation mirror."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from tools.sync_docs import (
    EXPECTED_DOCUMENT_PATHS,
    EXPECTED_FILES,
    MIRRORED_DIRECTORIES,
    SyncError,
    check_mirror,
    source_documents,
    write_mirror,
)


def _canonical_source(tmp_path: Path) -> Path:
    source = tmp_path / "canonical"
    source.mkdir()
    for name in EXPECTED_FILES:
        (source / name).write_text(f"# {name}\n", encoding="utf-8")
    for directory_name, filenames in MIRRORED_DIRECTORIES.items():
        directory = source / directory_name
        directory.mkdir()
        for name in filenames:
            (directory / name).write_text(f"# {name}\n", encoding="utf-8")
    return source


def test_exact_nested_specification_directory_is_validated_and_selected(
    tmp_path: Path,
) -> None:
    source = _canonical_source(tmp_path)

    documents = source_documents(source)

    assert tuple(documents) == EXPECTED_DOCUMENT_PATHS
    assert "100_Ondemand Fargate/10_scale-to-zero-goal.md" in documents


def test_nested_specifications_reach_the_written_mirror(tmp_path: Path) -> None:
    source = _canonical_source(tmp_path)
    destination = tmp_path / "docs"

    documents = source_documents(source)
    write_mirror(documents, destination)
    check_mirror(documents, destination)

    assert {entry.name for entry in destination.iterdir()} == {
        *EXPECTED_FILES,
        *MIRRORED_DIRECTORIES,
    }
    nested = destination / "100_Ondemand Fargate"
    assert {entry.name for entry in nested.iterdir()} == set(
        MIRRORED_DIRECTORIES["100_Ondemand Fargate"]
    )


def test_unexpected_nested_destination_file_is_rejected(tmp_path: Path) -> None:
    source = _canonical_source(tmp_path)
    destination = tmp_path / "docs"
    destination.mkdir()
    nested = destination / "100_Ondemand Fargate"
    nested.mkdir()
    (nested / "unexpected.md").write_text("# unexpected\n", encoding="utf-8")

    with pytest.raises(SyncError, match="unexpected files"):
        write_mirror(source_documents(source), destination)


def test_unexpected_nested_source_file_is_rejected(tmp_path: Path) -> None:
    source = _canonical_source(tmp_path)
    directory = source / "100_Ondemand Fargate"
    (directory / "unexpected.md").write_text("# unexpected\n", encoding="utf-8")

    with pytest.raises(SyncError, match="unexpected files"):
        source_documents(source)


def test_missing_nested_source_file_is_rejected(tmp_path: Path) -> None:
    source = _canonical_source(tmp_path)
    directory = source / "100_Ondemand Fargate"
    (directory / "10_scale-to-zero-goal.md").unlink()

    with pytest.raises(SyncError, match="required file is missing"):
        source_documents(source)


def test_nested_source_directory_symlink_is_rejected(tmp_path: Path) -> None:
    source = _canonical_source(tmp_path)
    directory = source / "100_Ondemand Fargate"
    external = tmp_path / "external-specifications"
    directory.rename(external)
    directory.symlink_to(external, target_is_directory=True)

    with pytest.raises(SyncError, match="symlink directory"):
        source_documents(source)


def test_nested_source_file_symlink_is_rejected(tmp_path: Path) -> None:
    source = _canonical_source(tmp_path)
    target = source / "100_Ondemand Fargate" / "10_scale-to-zero-goal.md"
    external = tmp_path / "external.md"
    external.write_text("# external\n", encoding="utf-8")
    target.unlink()
    target.symlink_to(external)

    with pytest.raises(SyncError, match="symlink file"):
        source_documents(source)


@pytest.mark.parametrize(
    ("label", "payload"),
    (
        ("AWS access key", "AK" + "IA" + "A" * 16),
        ("GitHub token", "gh" + "p_" + "a" * 30),
        ("OpenAI-style key", "s" + "k-" + "a" * 20),
        ("Discord token", "a" * 24 + "." + "b" * 6 + "." + "c" * 27),
        ("private key", "-----BEGIN " + "PRIVATE KEY-----"),
        ("absolute home path", "/" + "home" + "/operator/private"),
        ("Discord snowflake", "123456789" + "012345678"),
        ("email address", "operator" + "@" + "example.com"),
    ),
)
def test_nested_files_receive_the_same_secret_scan(
    tmp_path: Path,
    label: str,
    payload: str,
) -> None:
    source = _canonical_source(tmp_path)
    target = source / "100_Ondemand Fargate" / "10_scale-to-zero-goal.md"
    target.write_text(payload, encoding="utf-8")

    with pytest.raises(SyncError, match=label):
        source_documents(source)


@pytest.mark.parametrize(
    "operation",
    (write_mirror, check_mirror),
    ids=("write", "check"),
)
def test_destination_extra_symlink_is_rejected(
    tmp_path: Path,
    operation: Callable[[dict[str, bytes], Path], None],
) -> None:
    source = _canonical_source(tmp_path)
    documents = source_documents(source)
    destination = tmp_path / "docs"
    write_mirror(documents, destination)
    external = tmp_path / "external-readme.md"
    external.write_text("# external\n", encoding="utf-8")
    (destination / "README.md").symlink_to(external)

    with pytest.raises(SyncError, match="symlink file"):
        operation(documents, destination)


@pytest.mark.parametrize(
    "operation",
    (write_mirror, check_mirror),
    ids=("write", "check"),
)
def test_destination_extra_private_data_is_rejected(
    tmp_path: Path,
    operation: Callable[[dict[str, bytes], Path], None],
) -> None:
    source = _canonical_source(tmp_path)
    documents = source_documents(source)
    destination = tmp_path / "docs"
    write_mirror(documents, destination)
    payload = "-----BEGIN " + "PRIVATE KEY-----"
    (destination / "LICENSE.md").write_text(payload, encoding="utf-8")

    with pytest.raises(SyncError, match="private key"):
        operation(documents, destination)
