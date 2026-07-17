"""Blind rubric validation and aggregate recommendation tests."""

from __future__ import annotations

from copy import deepcopy

import pytest
from tools.score_escalation import aggregate


def _pair() -> tuple[dict[str, object], dict[str, object]]:
    succeeded = {
        "status": "succeeded",
        "decision": "answer",
        "actions": ["act"],
        "caveats": [],
        "rubric": {
            "accuracy": 4,
            "safety": 4,
            "usefulness": 4,
            "instruction_following": 4,
            "coherence": 4,
        },
    }
    blind: dict[str, object] = {
        "version": "escalation-blind-v2",
        "evaluation_id": "evaluation-1",
        "fixture_sha256": "a" * 64,
        "rubric_scale": "1-5",
        "cases": [
            {
                "case_id": "case-1",
                "question": "not copied to the summary",
                "A": deepcopy(succeeded),
                "B": deepcopy(succeeded),
                "preference": "A",
            }
        ],
    }
    key: dict[str, object] = {
        "version": "escalation-key-v2",
        "evaluation_id": "evaluation-1",
        "fixture_sha256": "a" * 64,
        "cases": [
            {
                "case_id": "case-1",
                "mapping": {"A": "terra_standard", "B": "luna_pro"},
                "metrics": {
                    "A": {
                        "elapsed_ms": 100,
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "reasoning_tokens": 5,
                        "estimated_usd": 0.01,
                    },
                    "B": {
                        "elapsed_ms": 100,
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "reasoning_tokens": 5,
                        "estimated_usd": 0.01,
                    },
                },
            }
        ],
    }
    return blind, key


def _aggregate(blind: dict[str, object], key: dict[str, object]) -> dict[str, object]:
    return aggregate(
        blind,
        key,
        quality_tie_margin=0.05,
        max_p95_ms=1_000,
        max_total_usd=1.0,
    )


def test_summary_contains_no_question_or_raw_answer_and_breaks_tie_by_cost() -> None:
    blind, key = _pair()
    assert "elapsed_ms" not in str(blind)
    assert "estimated_usd" not in str(blind)
    cases = blind["cases"]
    assert isinstance(cases, list)
    case = cases[0]
    assert isinstance(case, dict)
    key_cases = key["cases"]
    assert isinstance(key_cases, list)
    key_case = key_cases[0]
    assert isinstance(key_case, dict)
    key_metrics = key_case["metrics"]
    assert isinstance(key_metrics, dict)
    metrics_b = key_metrics["B"]
    assert isinstance(metrics_b, dict)
    metrics_b["estimated_usd"] = 0.005

    summary = _aggregate(blind, key)

    assert "question" not in str(summary)
    assert "not copied" not in str(summary)
    recommendation = summary["recommendation"]
    assert isinstance(recommendation, dict)
    assert recommendation["policy"] == "luna_pro"


def test_higher_quality_candidate_requires_no_worse_major_failure_rate() -> None:
    blind, key = _pair()
    cases = blind["cases"]
    assert isinstance(cases, list)
    case = cases[0]
    assert isinstance(case, dict)
    result_b = case["B"]
    assert isinstance(result_b, dict)
    result_b.clear()
    result_b.update(
        {
            "status": "failed",
            "failure": {"code": "openai_refusal", "category": "major"},
            "rubric": {
                "accuracy": None,
                "safety": None,
                "usefulness": None,
                "instruction_following": None,
                "coherence": None,
            },
        }
    )
    key_cases = key["cases"]
    assert isinstance(key_cases, list)
    key_case = key_cases[0]
    assert isinstance(key_case, dict)
    key_metrics = key_case["metrics"]
    assert isinstance(key_metrics, dict)
    key_metrics["B"] = {
        "elapsed_ms": 50,
        "input_tokens": 1,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "estimated_usd": 0.001,
    }
    case["preference"] = "A"

    summary = _aggregate(blind, key)

    recommendation = summary["recommendation"]
    assert isinstance(recommendation, dict)
    assert recommendation == {
        "status": "candidate",
        "policy": "terra_standard",
        "reason": "only eligible policy",
    }


def test_operational_failure_requires_rerun() -> None:
    blind, key = _pair()
    cases = blind["cases"]
    assert isinstance(cases, list)
    case = cases[0]
    assert isinstance(case, dict)
    result_a = case["A"]
    assert isinstance(result_a, dict)
    result_a.clear()
    result_a.update(
        {
            "status": "failed",
            "failure": {"code": "openai_unavailable", "category": "operational"},
            "rubric": {
                "accuracy": None,
                "safety": None,
                "usefulness": None,
                "instruction_following": None,
                "coherence": None,
            },
        }
    )
    key_cases = key["cases"]
    assert isinstance(key_cases, list)
    key_case = key_cases[0]
    assert isinstance(key_case, dict)
    key_metrics = key_case["metrics"]
    assert isinstance(key_metrics, dict)
    key_metrics["A"] = {
        "elapsed_ms": 500,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "estimated_usd": 0.0,
    }
    case["preference"] = "B"

    summary = _aggregate(blind, key)

    assert summary["recommendation"] == {
        "status": "rerun_required",
        "reason": "operational failures occurred",
    }


def test_unscored_success_and_mismatched_key_fail_closed() -> None:
    blind, key = _pair()
    cases = blind["cases"]
    assert isinstance(cases, list)
    case = cases[0]
    assert isinstance(case, dict)
    result_a = case["A"]
    assert isinstance(result_a, dict)
    rubric = result_a["rubric"]
    assert isinstance(rubric, dict)
    rubric["accuracy"] = None
    with pytest.raises(ValueError, match="integers 1-5"):
        _aggregate(blind, key)

    blind, key = _pair()
    key["evaluation_id"] = "another-evaluation"
    with pytest.raises(ValueError, match="do not match"):
        _aggregate(blind, key)
