"""Content-preserving versioned ballot codec; no legacy score reinterpretation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from decimal import Decimal

from shittim_chest.domain import ParticipantSlot
from shittim_chest.domain.composite_voting import (
    COMPOSITE_VOTING_VERSION,
    CandidateAssessment,
    CompositeBallot,
)

AXES = ("entertainment", "character", "originality", "responsiveness", "interaction")


def encode_composite_ballot(ballot: CompositeBallot) -> dict[str, object]:
    return {
        "rules_version": ballot.rules_version,
        "voter": ballot.voter.value,
        "assessments": [asdict(item) for item in ballot.assessments],
    }


def decode_composite_ballot(item: Mapping[str, object]) -> CompositeBallot:
    if (
        set(item) != {"rules_version", "voter", "assessments"}
        or item["rules_version"] != COMPOSITE_VOTING_VERSION
    ):
        raise ValueError("invalid composite ballot version or fields")
    raw = item["assessments"]
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError("invalid composite ballot assessment count")
    assessments = []
    for value in raw:
        if not isinstance(value, dict) or set(value) != {"candidate", "reason", *AXES}:
            raise ValueError("invalid composite assessment fields")
        scores = []
        for axis in AXES:
            number = value[axis]
            if (
                isinstance(number, Decimal)
                and number.is_finite()
                and number == number.to_integral_value()
            ):
                number = int(number)
            if type(number) is not int or not 0 <= number <= 5:
                raise ValueError("invalid composite assessment score")
            scores.append(number)
        if not isinstance(value["reason"], str):
            raise ValueError("invalid composite assessment reason")
        assessments.append(
            CandidateAssessment(
                _slot(value["candidate"]),
                scores[0],
                scores[1],
                scores[2],
                scores[3],
                scores[4],
                value["reason"],
            )
        )
    return CompositeBallot(_slot(item["voter"]), tuple(assessments))


def _slot(value: object) -> ParticipantSlot:
    if not isinstance(value, str) or value not in {slot.value for slot in ParticipantSlot}:
        raise ValueError("invalid composite ballot participant")
    return ParticipantSlot(value)
