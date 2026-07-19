"""Interactive blind preference review tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import cast

from tools.review_escalation import (
    normalize_preference,
    review_preferences,
    validate_resume,
    write_secure_json,
)


def _blind_document() -> dict[str, object]:
    result = {
        "status": "succeeded",
        "decision": "Choose a reversible option.",
        "actions": ["Run a small trial."],
        "caveats": ["Revisit after measurement."],
        "rubric": {
            "accuracy": None,
            "safety": None,
            "usefulness": None,
            "instruction_following": None,
            "coherence": None,
        },
    }
    return {
        "version": "escalation-blind-v2",
        "evaluation_id": "evaluation-1",
        "fixture_sha256": "a" * 64,
        "cases": [
            {
                "case_id": "case-1",
                "question": "Which option is better?",
                "A": deepcopy(result),
                "B": deepcopy(result),
                "preference": None,
            },
            {
                "case_id": "case-2",
                "question": "Which tradeoff is acceptable?",
                "A": deepcopy(result),
                "B": deepcopy(result),
                "preference": None,
            },
        ],
    }


def test_preference_aliases_are_intentionally_small() -> None:
    assert normalize_preference(" A ") == "A"
    assert normalize_preference("b") == "B"
    assert normalize_preference("T") == "tie"
    assert normalize_preference("tie") == "tie"
    assert normalize_preference("first") is None


def test_review_saves_after_each_choice_and_can_resume() -> None:
    document = _blind_document()
    entered = iter(("invalid", "a", "q"))
    saved: list[dict[str, object]] = []
    messages: list[str] = []

    completed = review_preferences(
        document,
        read_preference=lambda _prompt: next(entered),
        emit=messages.append,
        persist=lambda value: saved.append(deepcopy(dict(value))),
    )

    assert completed is False
    assert len(saved) == 2
    cases = document["cases"]
    assert isinstance(cases, list)
    cases = cast(list[object], cases)
    first = cases[0]
    assert isinstance(first, dict)
    first = cast(dict[str, object], first)
    assert first["preference"] == "A"
    assert any("Enter A, B, tie, or q." in message for message in messages)

    completed = review_preferences(
        document,
        read_preference=lambda _prompt: "tie",
        emit=messages.append,
        persist=lambda value: saved.append(deepcopy(dict(value))),
    )

    assert completed is True
    second = cases[1]
    assert isinstance(second, dict)
    second = cast(dict[str, object], second)
    assert second["preference"] == "tie"


def test_secure_writer_uses_owner_only_permissions(tmp_path: Path) -> None:
    output = tmp_path / "review" / "preferences.json"

    write_secure_json(output, _blind_document())

    assert output.stat().st_mode & 0o777 == 0o600
    loaded = json.loads(output.read_text(encoding="utf-8"))
    loaded = cast(dict[str, object], loaded)
    assert loaded["evaluation_id"] == "evaluation-1"


def test_resume_must_match_evaluation_identity() -> None:
    source = _blind_document()
    resumed = deepcopy(source)
    validate_resume(source, resumed)
    resumed["fixture_sha256"] = "b" * 64

    try:
        validate_resume(source, resumed)
    except ValueError as error:
        assert "different fixture_sha256" in str(error)
    else:
        raise AssertionError("mismatched review must fail closed")
