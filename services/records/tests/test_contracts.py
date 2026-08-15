"""Public Records API contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from shittim_records.contracts import (
    AvatarRef,
    CostsResponse,
    ErrorResponse,
    NonEmptyText,
    RankingsResponse,
    RecordDetailResponse,
    RecordListItem,
    RecordListResponse,
    RecordResultSummary,
    SessionResponse,
    VoteView,
)


def _record_detail_payload() -> dict[str, object]:
    participants = tuple(
        {
            "slot": slot,
            "displayName": name,
            "avatar": {
                "kind": "placeholder",
                "alt": f"{name}のアバター",
                "fallbackVariant": variant,
            },
        }
        for slot, name, variant in (
            ("participant-a", "参加者A", "cyan"),
            ("participant-b", "参加者B", "pink"),
            ("participant-c", "参加者C", "lavender"),
        )
    )
    return {
        "schemaVersion": 1,
        "recordId": "record-example",
        "completedAt": datetime(2026, 8, 15, tzinfo=UTC),
        "question": "休日の過ごし方を決める",
        "requester": {
            "displayName": "依頼者",
            "avatar": {
                "kind": "placeholder",
                "alt": "依頼者のアバター",
                "fallbackVariant": "cyan",
            },
        },
        "participants": participants,
        "initialOpinions": tuple(
            {"participant": slot, "summary": "要約", "proposal": "初回意見"}
            for slot in ("participant-a", "participant-b", "participant-c")
        ),
        "finalProposals": tuple(
            {"participant": slot, "title": "最終案", "proposal": "提案"}
            for slot in ("participant-a", "participant-b", "participant-c")
        ),
        "votes": (
            {"voter": "participant-a", "candidate": "participant-b", "reason": "理由A"},
            {"voter": "participant-b", "candidate": "participant-a", "reason": "理由B"},
            {"voter": "participant-c", "candidate": "participant-a", "reason": "理由C"},
        ),
        "result": {
            "winner": "participant-a",
            "voteCounts": (
                {"participant": "participant-a", "count": 2},
                {"participant": "participant-b", "count": 1},
                {"participant": "participant-c", "count": 0},
            ),
            "tieBreakApplied": False,
        },
        "finalDecision": {
            "winner": "participant-a",
            "victoryMessage": "勝利しました",
            "decision": "最終決定",
            "actions": ("実行する",),
            "caveats": ("注意する",),
        },
    }


def test_public_contracts_use_camel_case_and_reject_unknown_fields() -> None:
    response = SessionResponse.model_validate(
        {
            "schemaVersion": 1,
            "authenticated": True,
            "user": {
                "displayName": "利用者",
                "avatar": {
                    "kind": "placeholder",
                    "alt": "利用者のアバター",
                    "fallbackVariant": "cyan",
                },
            },
            "csrfToken": "csrf-example",
        }
    )

    payload = response.model_dump(by_alias=True, mode="json")

    assert payload["schemaVersion"] == 1
    assert payload["user"]["displayName"] == "利用者"
    with pytest.raises(ValidationError):
        SessionResponse.model_validate({"authenticated": False, "privateId": "forbidden"})


def test_record_list_does_not_expose_internal_or_evidence_fields() -> None:
    payload = RecordListResponse(
        schema_version=1,
        items=(
            {
                "schemaVersion": 1,
                "recordId": "record-example",
                "completedAt": datetime(2026, 8, 15, tzinfo=UTC),
                "questionPreview": "休日の過ごし方を決める",
                "requester": {
                    "displayName": "依頼者",
                    "avatar": {
                        "kind": "placeholder",
                        "alt": "依頼者のアバター",
                        "fallbackVariant": "pink",
                    },
                },
                "participants": tuple(
                    {
                        "slot": slot,
                        "displayName": name,
                        "avatar": {
                            "kind": "placeholder",
                            "alt": f"{name}のアバター",
                            "fallbackVariant": variant,
                        },
                    }
                    for slot, name, variant in (
                        ("participant-a", "参加者A", "cyan"),
                        ("participant-b", "参加者B", "pink"),
                        ("participant-c", "参加者C", "lavender"),
                    )
                ),
                "result": {
                    "winner": "participant-a",
                    "voteCounts": (
                        {"participant": "participant-a", "count": 2},
                        {"participant": "participant-b", "count": 1},
                        {"participant": "participant-c", "count": 0},
                    ),
                    "tieBreakApplied": False,
                },
            },
        ),
    ).model_dump(by_alias=True, mode="json")

    serialized = repr(payload)
    for forbidden in (
        "requesterId",
        "guildId",
        "channelId",
        "sourceFingerprint",
        "evidence",
        "duration",
    ):
        assert forbidden not in serialized


def test_public_text_rejects_whitespace_only() -> None:
    adapter = TypeAdapter(NonEmptyText)

    assert adapter.validate_python("内容あり") == "内容あり"
    with pytest.raises(ValidationError):
        adapter.validate_python(" \t\n")


def test_avatar_contract_requires_url_only_for_images() -> None:
    adapter = TypeAdapter(AvatarRef)

    placeholder = adapter.validate_python(
        {"kind": "placeholder", "alt": "代替アバター", "fallbackVariant": "cyan"}
    )
    assert placeholder.url is None
    image = adapter.validate_python(
        {
            "kind": "image",
            "url": "https://example.invalid/avatar.webp",
            "alt": "画像アバター",
            "fallbackVariant": "pink",
        }
    )
    assert image.url == "https://example.invalid/avatar.webp"
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "image", "alt": "URLなし", "fallbackVariant": "lavender"})
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "kind": "placeholder",
                "url": "https://example.invalid/unexpected.webp",
                "alt": "不正placeholder",
                "fallbackVariant": "cyan",
            }
        )


def test_error_envelope_is_strict() -> None:
    response = ErrorResponse(
        error={"code": "RECORD_NOT_FOUND", "message": "見つかりません", "requestId": "r1"}
    )
    assert response.error.code == "RECORD_NOT_FOUND"


def test_versioned_responses_require_the_exact_schema_version() -> None:
    with pytest.raises(ValidationError):
        SessionResponse.model_validate({"authenticated": False})
    with pytest.raises(ValidationError):
        SessionResponse.model_validate({"schemaVersion": 2, "authenticated": False})


@pytest.mark.parametrize(
    "payload",
    [
        {"schemaVersion": 1, "authenticated": True, "user": None, "csrfToken": None},
        {
            "schemaVersion": 1,
            "authenticated": False,
            "user": {
                "displayName": "利用者",
                "avatar": {
                    "kind": "placeholder",
                    "alt": "利用者のアバター",
                    "fallbackVariant": "cyan",
                },
            },
            "csrfToken": "unexpected",
        },
    ],
)
def test_session_contract_rejects_ambiguous_authentication_states(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SessionResponse.model_validate(payload)


@pytest.mark.parametrize(
    ("model", "field_name"),
    [
        (RecordListItem, "completed_at"),
        (RecordDetailResponse, "completed_at"),
        (RankingsResponse, "generated_at"),
        (CostsResponse, "updated_at"),
    ],
)
def test_public_timestamps_require_timezone_offsets(
    model: type[BaseModel],
    field_name: str,
) -> None:
    adapter = TypeAdapter(model.model_fields[field_name].annotation)
    aware = datetime(2026, 8, 15, tzinfo=UTC)

    assert adapter.validate_python(aware) == aware
    with pytest.raises(ValidationError):
        adapter.validate_python(aware.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("collection_name", "identity_field", "duplicate_slot"),
    [
        ("participants", "slot", "participant-a"),
        ("initialOpinions", "participant", "participant-a"),
        ("finalProposals", "participant", "participant-a"),
        ("votes", "voter", "participant-b"),
    ],
)
def test_record_detail_requires_every_participant_slot_once(
    collection_name: str,
    identity_field: str,
    duplicate_slot: str,
) -> None:
    payload = _record_detail_payload()
    collection = payload[collection_name]
    assert isinstance(collection, tuple)
    duplicate = collection[2]
    assert isinstance(duplicate, dict)
    duplicate[identity_field] = duplicate_slot

    with pytest.raises(ValidationError, match="every participant slot exactly once"):
        RecordDetailResponse.model_validate(payload)


def test_record_detail_requires_one_canonical_winner() -> None:
    payload = _record_detail_payload()
    assert RecordDetailResponse.model_validate(payload).result.winner == "participant-a"
    final_decision = payload["finalDecision"]
    assert isinstance(final_decision, dict)
    final_decision["winner"] = "participant-b"

    with pytest.raises(ValidationError, match="must identify the same winner"):
        RecordDetailResponse.model_validate(payload)


def test_record_result_requires_complete_consistent_vote_counts() -> None:
    complete_counts = (
        {"participant": "participant-a", "count": 2},
        {"participant": "participant-b", "count": 1},
        {"participant": "participant-c", "count": 0},
    )
    assert (
        RecordResultSummary.model_validate(
            {"winner": "participant-a", "voteCounts": complete_counts, "tieBreakApplied": False}
        ).winner
        == "participant-a"
    )

    with pytest.raises(ValidationError, match="every participant slot exactly once"):
        RecordResultSummary.model_validate(
            {
                "winner": "participant-a",
                "voteCounts": (
                    complete_counts[0],
                    {"participant": "participant-a", "count": 1},
                    complete_counts[2],
                ),
                "tieBreakApplied": False,
            }
        )
    with pytest.raises(ValidationError, match="highest vote count"):
        RecordResultSummary.model_validate(
            {"winner": "participant-c", "voteCounts": complete_counts, "tieBreakApplied": False}
        )
    with pytest.raises(ValidationError, match="must match the vote count tie"):
        RecordResultSummary.model_validate(
            {
                "winner": "participant-a",
                "voteCounts": tuple(
                    {"participant": slot, "count": 1}
                    for slot in ("participant-a", "participant-b", "participant-c")
                ),
                "tieBreakApplied": False,
            }
        )


def test_record_detail_requires_vote_counts_to_match_ballot() -> None:
    payload = _record_detail_payload()
    result = payload["result"]
    final_decision = payload["finalDecision"]
    assert isinstance(result, dict)
    assert isinstance(final_decision, dict)
    result["voteCounts"] = (
        {"participant": "participant-a", "count": 0},
        {"participant": "participant-b", "count": 0},
        {"participant": "participant-c", "count": 3},
    )
    result["winner"] = "participant-c"
    final_decision["winner"] = "participant-c"

    with pytest.raises(ValidationError, match="must match the complete ballot"):
        RecordDetailResponse.model_validate(payload)


def test_public_vote_rejects_self_vote() -> None:
    with pytest.raises(ValidationError, match="cannot vote for itself"):
        VoteView.model_validate(
            {"voter": "participant-a", "candidate": "participant-a", "reason": "自己投票"}
        )


def test_public_vote_rejects_whitespace_only_reason() -> None:
    with pytest.raises(ValidationError):
        VoteView.model_validate(
            {"voter": "participant-a", "candidate": "participant-b", "reason": " \t "}
        )
