from decimal import Decimal
from typing import cast

import pytest

from shittim_chest.adapters.dynamodb.composite_ballot import (
    decode_composite_ballot,
    encode_composite_ballot,
)
from shittim_chest.domain import PARTICIPANTS
from shittim_chest.domain.composite_voting import CandidateAssessment, CompositeBallot


def test_roundtrip_preserves_both_scores_and_rejects_unknown_records():
    a, b, c = PARTICIPANTS
    ballot = CompositeBallot(
        a, tuple(CandidateAssessment(slot, 1, 2, 3, 4, 5, "fixture") for slot in (b, c))
    )
    encoded = encode_composite_ballot(ballot)
    assert decode_composite_ballot(encoded) == ballot
    assessments = cast(list[dict[str, object]], encoded["assessments"])
    assessments[0]["entertainment"] = Decimal(1)
    assert decode_composite_ballot(encoded) == ballot
    for version in (None, "unknown", "legacy-v1"):
        with pytest.raises(ValueError):
            decode_composite_ballot({**encoded, "rules_version": version})
    for invalid in (True, Decimal("1.2"), Decimal("NaN"), -1, 6):
        assessments[0]["entertainment"] = invalid
        with pytest.raises(ValueError):
            decode_composite_ballot(encoded)
