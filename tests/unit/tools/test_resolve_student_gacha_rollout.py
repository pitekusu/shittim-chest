from __future__ import annotations

import pytest
from tools.resolve_student_gacha_rollout import resolve_student_gacha_rollout


def test_first_release_defaults_off_and_later_releases_preserve_the_switch() -> None:
    assert resolve_student_gacha_rollout("keep", {}) == "false"
    enabled = {"Parameters": [{"ParameterKey": "StudentGachaEnabled", "ParameterValue": "true"}]}
    assert resolve_student_gacha_rollout("keep", enabled) == "true"
    assert resolve_student_gacha_rollout("false", enabled) == "false"


def test_keep_fails_on_malformed_deployed_parameter() -> None:
    stack = {"Parameters": [{"ParameterKey": "StudentGachaEnabled", "ParameterValue": ""}]}
    with pytest.raises(ValueError, match="deployed student gacha"):
        resolve_student_gacha_rollout("keep", stack)
