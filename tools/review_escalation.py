#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Interactively record blind A/B/tie preferences without exposing policy names."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from tools.evaluate_escalation import REPOSITORY_ROOT, validate_output_directory

PreferenceReader = Callable[[str], str]
Emitter = Callable[[str], None]
Persister = Callable[[Mapping[str, object]], None]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blind-results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def load_document(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    if value.get("version") != "escalation-blind-v2":
        raise ValueError("unsupported blind results version")
    cases = value.get("cases")
    if (
        not isinstance(cases, list)
        or not cases
        or not all(isinstance(case, dict) for case in cases)
    ):
        raise ValueError("blind results must contain evaluation cases")
    return value


def validate_resume(source: Mapping[str, object], resumed: Mapping[str, object]) -> None:
    for field in ("version", "evaluation_id", "fixture_sha256"):
        if source.get(field) != resumed.get(field):
            raise ValueError(f"existing review output has a different {field}")


def normalize_preference(value: str) -> str | None:
    normalized = value.strip().lower()
    aliases = {"a": "A", "b": "B", "t": "tie", "tie": "tie"}
    return aliases.get(normalized)


def render_case(case: Mapping[str, object], *, index: int, total: int) -> str:
    case_id = _required_text(case.get("case_id"), "case ID")
    question = _required_text(case.get("question"), f"{case_id} question")
    sections = [f"\n=== {index}/{total}: {case_id} ===", f"Question: {question}"]
    for label in ("A", "B"):
        result = case.get(label)
        if not isinstance(result, dict):
            raise ValueError(f"{case_id} result {label} must be an object")
        sections.extend(_render_result(label, result, case_id=case_id))
    return "\n".join(sections)


def review_preferences(
    document: dict[str, object],
    *,
    read_preference: PreferenceReader,
    emit: Emitter,
    persist: Persister,
) -> bool:
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError("blind results must contain evaluation cases")
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError("every evaluation case must be an object")
        if case.get("preference") in {"A", "B", "tie"}:
            continue
        emit(render_case(case, index=index, total=len(cases)))
        while True:
            entered = read_preference("Preference [A/B/tie, q=save and quit]: ")
            if entered.strip().lower() in {"q", "quit"}:
                persist(document)
                return False
            preference = normalize_preference(entered)
            if preference is None:
                emit("Enter A, B, tie, or q.")
                continue
            case["preference"] = preference
            persist(document)
            break
    return True


def write_secure_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _render_result(label: str, result: Mapping[str, object], *, case_id: str) -> list[str]:
    status = result.get("status")
    if status == "failed":
        failure = result.get("failure")
        if not isinstance(failure, dict):
            raise ValueError(f"{case_id} failed result {label} requires failure details")
        return [f"\n[{label}] Failed: {failure.get('category', 'unknown')}"]
    if status != "succeeded":
        raise ValueError(f"{case_id} result {label} has an invalid status")
    decision = _required_text(result.get("decision"), f"{case_id} decision {label}")
    lines = [f"\n[{label}]", f"Decision: {decision}"]
    for heading, field in (("Actions", "actions"), ("Caveats", "caveats")):
        items = result.get(field)
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise ValueError(f"{case_id} result {label} has invalid {field}")
        lines.append(f"{heading}: " + (" / ".join(items) if items else "(none)"))
    return lines


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        source_path = args.blind_results.expanduser().resolve()
        if source_path == REPOSITORY_ROOT or REPOSITORY_ROOT in source_path.parents:
            raise ValueError("raw evaluation input must remain outside the repository")
        output_path = validate_output_directory(args.output)
        if source_path == output_path:
            raise ValueError("review output must not overwrite the original blind results")
        source = load_document(source_path)
        if output_path.exists():
            reviewed = load_document(output_path)
            validate_resume(source, reviewed)
        else:
            reviewed = deepcopy(source)
        completed = review_preferences(
            reviewed,
            read_preference=input,
            emit=print,
            persist=lambda value: write_secure_json(output_path, value),
        )
    except (EOFError, OSError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    if not completed:
        print(f"saved partial blind preferences to {output_path}")
        return 2
    write_secure_json(output_path, reviewed)
    print(f"completed blind preferences in {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
