"""Tests for deterministic installed-wheel RECORD ordering."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from tools.canonicalize_wheel_records import canonicalize_wheel_records


def _record(venv: Path, distribution: str, rows: list[list[str]]) -> Path:
    record = venv / "lib" / "python3.14" / "site-packages" / f"{distribution}.dist-info" / "RECORD"
    record.parent.mkdir(parents=True)
    with record.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\r\n").writerows(rows)
    return record


def test_canonicalize_sorts_records_and_normalizes_line_endings(tmp_path: Path) -> None:
    first = _record(
        tmp_path,
        "example-1.0",
        [
            ["example/data,one.txt", "sha256=bbb", "2"],
            ["example/__init__.py", "sha256=aaa", "1"],
            ["example-1.0.dist-info/RECORD", "", ""],
        ],
    )
    second = _record(
        tmp_path,
        "another-2.0",
        [
            ["another.py", "sha256=ddd", "4"],
            ["../../../bin/another", "sha256=ccc", "3"],
        ],
    )

    assert canonicalize_wheel_records(tmp_path) == 2
    assert first.read_bytes() == (
        b"example-1.0.dist-info/RECORD,,\n"
        b"example/__init__.py,sha256=aaa,1\n"
        b'"example/data,one.txt",sha256=bbb,2\n'
    )
    assert second.read_bytes() == (b"../../../bin/another,sha256=ccc,3\nanother.py,sha256=ddd,4\n")
    assert canonicalize_wheel_records(tmp_path, check=True) == 2


def test_check_rejects_noncanonical_record(tmp_path: Path) -> None:
    _record(
        tmp_path,
        "example-1.0",
        [
            ["z.py", "sha256=bbb", "2"],
            ["a.py", "sha256=aaa", "1"],
        ],
    )

    with pytest.raises(ValueError, match="non-canonical"):
        canonicalize_wheel_records(tmp_path, check=True)


def test_canonicalize_removes_uv_cache_metadata_and_record_row(tmp_path: Path) -> None:
    record = _record(
        tmp_path,
        "example-1.0",
        [
            ["example.py", "sha256=aaa", "1"],
            ["example-1.0.dist-info/uv_cache.json", "sha256=unstable", "140"],
            ["example-1.0.dist-info/RECORD", "", ""],
        ],
    )
    uv_cache = record.parent / "uv_cache.json"
    uv_cache.write_text('{"timestamp":"checkout-specific"}\n', encoding="utf-8")

    assert canonicalize_wheel_records(tmp_path) == 1
    assert not uv_cache.exists()
    assert b"uv_cache.json" not in record.read_bytes()
    assert canonicalize_wheel_records(tmp_path, check=True) == 1


def test_check_rejects_uv_cache_metadata_even_when_record_is_sorted(tmp_path: Path) -> None:
    record = _record(
        tmp_path,
        "example-1.0",
        [
            ["example-1.0.dist-info/RECORD", "", ""],
            ["example-1.0.dist-info/uv_cache.json", "sha256=unstable", "140"],
            ["example.py", "sha256=aaa", "1"],
        ],
    )
    (record.parent / "uv_cache.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="non-canonical"):
        canonicalize_wheel_records(tmp_path, check=True)


def test_canonicalize_rejects_unexpected_uv_cache_record_path(tmp_path: Path) -> None:
    _record(
        tmp_path,
        "example-1.0",
        [["other-1.0.dist-info/uv_cache.json", "sha256=unstable", "140"]],
    )

    with pytest.raises(ValueError, match="unexpected uv cache path"):
        canonicalize_wheel_records(tmp_path)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [["one.py", "sha256=aaa"]],
        [["", "sha256=aaa", "1"]],
        [["one.py", "sha256=aaa", "1"], ["one.py", "sha256=bbb", "2"]],
    ],
)
def test_canonicalize_rejects_invalid_records(tmp_path: Path, rows: list[list[str]]) -> None:
    _record(tmp_path, "example-1.0", rows)

    with pytest.raises(ValueError, match="RECORD"):
        canonicalize_wheel_records(tmp_path)


def test_canonicalize_requires_at_least_one_record(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="contains no wheel RECORD"):
        canonicalize_wheel_records(tmp_path)
