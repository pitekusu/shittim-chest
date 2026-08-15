"""Public Records API contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from shittim_records.contracts import (
    AvatarRef,
    ErrorResponse,
    RecordListResponse,
    SessionResponse,
)


def test_public_contracts_use_camel_case_and_reject_unknown_fields() -> None:
    response = SessionResponse(
        schema_version=1,
        authenticated=True,
        user={
            "displayName": "利用者",
            "avatar": {
                "kind": "placeholder",
                "alt": "利用者のアバター",
                "fallbackVariant": "cyan",
            },
        },
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


def test_avatar_image_url_is_optional_for_placeholder() -> None:
    avatar = AvatarRef(kind="placeholder", alt="代替アバター", fallback_variant="cyan")
    assert avatar.url is None


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
