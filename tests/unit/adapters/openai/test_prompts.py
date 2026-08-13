"""Focused prompt-contract tests for participant and final-proposal instructions."""

from __future__ import annotations

from shittim_chest.adapters.openai.prompts import (
    final_proposal_instructions,
    participant_instructions,
)


def test_participant_instructions_apply_the_shared_evidence_and_persona_rules() -> None:
    instructions = participant_instructions("private persona marker")

    assert "Evidence as the ceiling for factual claims" in instructions
    assert "verified facts" in instructions
    assert "do not rush toward consensus" in instructions
    assert "average compromise" in instructions
    assert "neutral, generic assistant" in instructions
    assert instructions.count("private persona marker") == 1
    assert "review all three initial opinions" not in instructions


def test_final_proposal_instructions_require_persona_led_cross_opinion_review() -> None:
    instructions = final_proposal_instructions("private persona marker")

    assert "review all three initial opinions" in instructions
    assert "common ground and conflicts" in instructions
    assert "incorporate useful strengths" in instructions
    assert "material weaknesses" in instructions
    assert "or omissions" in instructions
    assert "driven by this persona's own judgment" in instructions
    assert "not a list or neutral summary" in instructions
    assert "return only the requested structured output" in instructions
