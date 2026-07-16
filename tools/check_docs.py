#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate the public documentation mirror without accessing the private Vault."""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

from tools.sync_docs import ALLOWED_DESTINATION_EXTRAS, EXPECTED_FILES

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCS_DIRECTORY = REPOSITORY_ROOT / "docs"
REQUIRED_FRONTMATTER_KEYS = {"aliases", "tags", "status", "created", "updated"}
OFFICIAL_SOURCE_FILES = {
    name for name in EXPECTED_FILES if name[:3] in {f"{number:02d}_" for number in range(10, 20)}
}
WIKI_LINK = re.compile(r"\[\[([^\]]+)\]\]")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^#{1,6}\s+(.+?)(?:\s+#+)?$")
INLINE_CODE = re.compile(r"(`+)(.+?)\1")


class DocumentationError(RuntimeError):
    """Raised when the repository documentation mirror is structurally invalid."""


def read_markdown(path: Path) -> str:
    """Read one regular UTF-8/LF Markdown file with a final newline."""

    if path.is_symlink() or not path.is_file():
        raise DocumentationError(f"Markdown must be a regular non-symlink file: {path}")
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise DocumentationError(f"cannot read UTF-8 Markdown {path}: {error}") from error
    if b"\r" in data:
        raise DocumentationError(f"Markdown must use LF line endings: {path.name}")
    if not text.endswith("\n"):
        raise DocumentationError(f"Markdown must end with a newline: {path.name}")
    return text


def parse_frontmatter(text: str, filename: str) -> dict[str, str]:
    """Parse the intentionally simple top-level YAML frontmatter contract."""

    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise DocumentationError(f"missing opening frontmatter delimiter: {filename}")
    try:
        closing_index = lines.index("---", 1)
    except ValueError as error:
        raise DocumentationError(f"missing closing frontmatter delimiter: {filename}") from error
    if closing_index == 1:
        raise DocumentationError(f"empty frontmatter: {filename}")

    values: dict[str, str] = {}
    frontmatter_lines = lines[1:closing_index]
    index = 0
    while index < len(frontmatter_lines):
        line = frontmatter_lines[index]
        line_number = index + 2
        if line.startswith((" ", "\t", "-")):
            raise DocumentationError(
                f"unexpected frontmatter continuation at {filename}:{line_number}"
            )
        key, separator, value = line.partition(":")
        if not separator or not key:
            raise DocumentationError(f"invalid top-level frontmatter at {filename}:{line_number}")
        if key in values:
            raise DocumentationError(f"duplicate frontmatter key {key!r}: {filename}")
        normalized_value = value.strip()
        index += 1
        if not normalized_value:
            if key not in {"aliases", "tags"}:
                raise DocumentationError(f"empty top-level frontmatter at {filename}:{line_number}")
            block_items = 0
            while index < len(frontmatter_lines):
                continuation = frontmatter_lines[index]
                if not continuation.startswith((" ", "\t")):
                    break
                item = continuation.lstrip()
                if not item.startswith("- ") or not item[2:].strip():
                    raise DocumentationError(
                        f"invalid frontmatter list item at {filename}:{index + 2}"
                    )
                block_items += 1
                index += 1
            if block_items == 0:
                raise DocumentationError(
                    f"empty frontmatter list {key!r} at {filename}:{line_number}"
                )
            normalized_value = f"[{block_items} block items]"
        values[key] = normalized_value

    missing = REQUIRED_FRONTMATTER_KEYS - values.keys()
    if missing:
        raise DocumentationError(
            f"missing frontmatter keys in {filename}: {', '.join(sorted(missing))}"
        )
    for key in ("created", "updated"):
        try:
            date.fromisoformat(values[key])
        except ValueError:
            raise DocumentationError(f"invalid {key} date in {filename}: {values[key]}") from None
    if values["updated"] < values["created"]:
        raise DocumentationError(f"updated date precedes created date: {filename}")
    return values


def validate_fences(text: str, filename: str) -> None:
    """Reject unclosed or mismatched fenced code blocks."""

    opening_character: str | None = None
    opening_length = 0
    opening_line = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = FENCE.match(line)
        if match is None:
            continue
        delimiter, suffix = match.groups()
        character = delimiter[0]
        if opening_character is None:
            opening_character = character
            opening_length = len(delimiter)
            opening_line = line_number
            continue
        if (
            character == opening_character
            and len(delimiter) >= opening_length
            and not suffix.strip()
        ):
            opening_character = None
            opening_length = 0
            opening_line = 0
    if opening_character is not None:
        raise DocumentationError(f"unclosed code fence in {filename}:{opening_line}")


def markdown_content_lines(text: str) -> tuple[str, ...]:
    """Return Markdown lines with fenced and inline code removed."""

    content: list[str] = []
    opening_character: str | None = None
    opening_length = 0
    for line in text.splitlines():
        match = FENCE.match(line)
        if match is not None:
            delimiter, suffix = match.groups()
            character = delimiter[0]
            if opening_character is None:
                opening_character = character
                opening_length = len(delimiter)
            elif (
                character == opening_character
                and len(delimiter) >= opening_length
                and not suffix.strip()
            ):
                opening_character = None
                opening_length = 0
            content.append("")
            continue
        if opening_character is not None:
            content.append("")
            continue
        content.append(INLINE_CODE.sub("", line))
    return tuple(content)


def markdown_headings(text: str) -> frozenset[str]:
    """Return exact headings available for Obsidian heading links."""

    return frozenset(
        match.group(1).strip()
        for line in markdown_content_lines(text)
        if (match := HEADING.match(line)) is not None
    )


def wiki_links(text: str) -> frozenset[tuple[str, str | None]]:
    """Return validated flat-file Wiki targets and optional headings."""

    links: set[tuple[str, str | None]] = set()
    for line in markdown_content_lines(text):
        for match in WIKI_LINK.finditer(line):
            target_and_heading = match.group(1).split("|", maxsplit=1)[0].strip()
            target, separator, heading = target_and_heading.partition("#")
            target = target.strip()
            normalized_heading = heading.strip() if separator else None
            if not target or any(value in target for value in ("/", "\\", "..")):
                raise DocumentationError(f"invalid flat Wiki link target: {target!r}")
            if separator and not normalized_heading:
                raise DocumentationError(f"empty Wiki link heading: {target!r}")
            links.add((Path(target).stem, normalized_heading))
    return frozenset(links)


def validate_official_sources(text: str, filename: str) -> None:
    """Require a populated official-source table in every detailed design."""

    lines = text.splitlines()
    heading_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("## ") and "公式資料確認記録" in line
        ),
        None,
    )
    if heading_index is None:
        raise DocumentationError(f"missing official-source section: {filename}")
    table_lines = [line for line in lines[heading_index + 1 :] if line.startswith("|")]
    if len(table_lines) < 3:
        raise DocumentationError(f"official-source table has no data: {filename}")
    header = table_lines[0]
    if not all(label in header for label in ("確認日", "対象", "公式資料")):
        raise DocumentationError(f"invalid official-source table header: {filename}")

    data_rows = table_lines[2:]
    for row_number, row in enumerate(data_rows, start=1):
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if len(cells) < 4 or any(not cell for cell in cells[:4]):
            raise DocumentationError(f"invalid official-source row {row_number} in {filename}")
        try:
            date.fromisoformat(cells[0])
        except ValueError:
            raise DocumentationError(
                f"invalid official-source date in {filename}: {cells[0]}"
            ) from None
        if not any("https://" in cell for cell in cells):
            raise DocumentationError(
                f"official-source row lacks an HTTPS URL in {filename}: {row_number}"
            )


def validate_license_scope(repository_root: Path) -> None:
    """Validate the repository-level MIT and reserved-document boundary."""

    license_text = read_markdown(repository_root / "LICENSE")
    scope_text = read_markdown(repository_root / "LICENSE-SCOPE.md")
    docs_license = read_markdown(repository_root / "docs" / "LICENSE.md")
    if not license_text.startswith("MIT License\n"):
        raise DocumentationError("repository LICENSE is not the expected MIT License")
    for marker in ("`AGENTS.md`", "`docs/`", "all rights reserved"):
        if marker not in scope_text:
            raise DocumentationError(f"LICENSE-SCOPE.md lacks required marker: {marker}")
    for marker in ("All rights reserved", "not licensed for copying"):
        if marker not in docs_license:
            raise DocumentationError(f"docs/LICENSE.md lacks required marker: {marker}")


def validate_docs_directory(directory: Path) -> int:
    """Validate the complete public mirror and return the number of canonical notes."""

    if directory.is_symlink() or not directory.is_dir():
        raise DocumentationError(f"docs directory must be regular: {directory}")
    expected_names = set(EXPECTED_FILES) | ALLOWED_DESTINATION_EXTRAS
    actual_names = {entry.name for entry in directory.iterdir()}
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        details: list[str] = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        raise DocumentationError("invalid docs file set; " + "; ".join(details))

    findings: list[str] = []
    texts: dict[str, str] = {}
    headings: dict[str, frozenset[str]] = {}
    for filename in EXPECTED_FILES:
        try:
            text = read_markdown(directory / filename)
            texts[filename] = text
            headings[Path(filename).stem] = markdown_headings(text)
        except DocumentationError as error:
            findings.append(str(error))

    available_targets = set(headings)
    for filename in EXPECTED_FILES:
        if filename not in texts:
            continue
        try:
            text = texts[filename]
            parse_frontmatter(text, filename)
            validate_fences(text, filename)
            for target, heading in wiki_links(text):
                if target not in available_targets:
                    raise DocumentationError(f"missing Wiki link target in {filename}: {target}")
                if heading is not None and heading not in headings[target]:
                    raise DocumentationError(
                        f"missing Wiki link heading in {filename}: {target}#{heading}"
                    )
            if filename in OFFICIAL_SOURCE_FILES:
                validate_official_sources(text, filename)
        except DocumentationError as error:
            findings.append(str(error))

    for extra in ALLOWED_DESTINATION_EXTRAS:
        try:
            text = read_markdown(directory / extra)
            validate_fences(text, extra)
        except DocumentationError as error:
            findings.append(str(error))

    if findings:
        raise DocumentationError("documentation validation failed:\n- " + "\n- ".join(findings))
    return len(EXPECTED_FILES)


def main() -> int:
    try:
        count = validate_docs_directory(DEFAULT_DOCS_DIRECTORY)
        validate_license_scope(REPOSITORY_ROOT)
    except DocumentationError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"public documentation is valid: {count} canonical notes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
