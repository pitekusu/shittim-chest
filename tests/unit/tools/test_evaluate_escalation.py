"""Safety tests for the opt-in paid escalation evaluator."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.evaluate_escalation import (
    DEFAULT_FIXTURE,
    _adapter_errors,
    _failure_category,
    _representative_failure_code,
    load_cases,
    require_live_access,
    validate_output_directory,
    validate_separate_output_directories,
)

from shittim_chest.adapters.openai.errors import OpenAIRefusal, OpenAIUnavailable


def test_paid_evaluation_requires_both_explicit_gates() -> None:
    with pytest.raises(ValueError, match="--live"):
        require_live_access(live=False, environment={"OPENAI_API_KEY": "not-a-real-key"})
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        require_live_access(live=True, environment={})
    assert (
        require_live_access(
            live=True,
            environment={"OPENAI_API_KEY": "not-a-real-key"},
        )
        == "not-a-real-key"
    )


def test_raw_output_is_rejected_inside_repository(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        validate_output_directory(Path(__file__).resolve().parents[3] / "eval-output")
    assert validate_output_directory(tmp_path) == tmp_path.resolve()


def test_blind_results_and_policy_key_require_separate_trees(tmp_path: Path) -> None:
    scorer = tmp_path / "scorer"
    key = tmp_path / "operator"
    assert validate_separate_output_directories(scorer, key) == (
        scorer.resolve(),
        key.resolve(),
    )
    with pytest.raises(ValueError, match="separate"):
        validate_separate_output_directories(scorer, scorer)
    with pytest.raises(ValueError, match="separate"):
        validate_separate_output_directories(scorer, scorer / "key")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("openai_refusal", "major"),
        ("openai_incomplete", "major"),
        ("openai_invalid_output", "major"),
        ("openai_rate_limited", "operational"),
        ("openai_unavailable", "operational"),
    ],
)
def test_failure_categories_separate_quality_from_operations(code: str, expected: str) -> None:
    assert _failure_category(code) == expected


def test_nested_task_group_failures_are_unwrapped_without_hiding_unknown_errors() -> None:
    refusal = OpenAIRefusal()
    unavailable = OpenAIUnavailable()
    grouped = ExceptionGroup(
        "parallel policy calls failed",
        [refusal, ExceptionGroup("nested", [unavailable, RuntimeError("bug")])],
    )

    assert _adapter_errors(grouped) == (refusal, unavailable)
    assert _adapter_errors(RuntimeError("bug")) == ()
    assert _representative_failure_code((refusal.code, unavailable.code)) == "openai_unavailable"


def test_versioned_fixture_has_ten_valid_generic_cases() -> None:
    cases = load_cases(DEFAULT_FIXTURE, limit=10)
    assert len(cases) == 10
    assert len({case.case_id for case in cases}) == 10
    assert all(len(case.initial_opinions) == 3 for case in cases)
