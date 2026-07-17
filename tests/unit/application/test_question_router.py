"""Tests for deterministic search requirement routing."""

import pytest

from shittim_chest.application.question_router import DeterministicQuestionRouter
from shittim_chest.domain import SearchRequirement


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("東京の今日の天気は?", SearchRequirement.REQUIRED),
        ("現在の日本の首相は?", SearchRequirement.REQUIRED),
        ("明朝の空模様を教えて", SearchRequirement.REQUIRED),
        ("いまのドル円レートは?", SearchRequirement.REQUIRED),
        ("総理は誰?", SearchRequirement.REQUIRED),
        ("今日の朝ごはんは何がいい?甘いものが食べたい", SearchRequirement.OPTIONAL),
        ("フレンチトーストとパンケーキを比較して", SearchRequirement.NONE),
        ("来週よさそうな甘味処を知りたい", SearchRequirement.OPTIONAL),
    ],
)
def test_route_is_explicit_and_deterministic(
    question: str,
    expected: SearchRequirement,
) -> None:
    router = DeterministicQuestionRouter()

    route = router.route(question)
    assert route.requirement is expected
    assert route.rules_version == "question-router-v2"
    assert route.reason


def test_unknown_expression_falls_back_to_optional_with_audit_reason() -> None:
    route = DeterministicQuestionRouter().route("朝イチの景況感をざっくり知りたい")

    assert route.requirement is SearchRequirement.OPTIONAL
    assert route.reason == "unknown_expression"


def test_empty_question_fails_closed() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        DeterministicQuestionRouter().route("  ")
