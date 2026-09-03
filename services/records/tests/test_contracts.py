"""Public Records API contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from shittim_records.contracts import (
    AffectionRankingsResponse,
    AvatarRef,
    CostsResponse,
    ErrorResponse,
    MemorialGenerateRequest,
    MemorialMemoryResponse,
    MemorialResetRequest,
    MemorialStateResponse,
    MemorialUploadRequest,
    MemorialUploadResponse,
    NonEmptyText,
    RankingsResponse,
    RecordDetailResponse,
    RecordListItem,
    RecordListResponse,
    RecordResultSummary,
    SessionResponse,
    VoteView,
)


def test_affection_rankings_require_nullable_next_cursor_property() -> None:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "generatedAt": datetime(2026, 8, 30, tzinfo=UTC),
        "defaultScore": 500,
        "maxScore": 1000,
        "rankings": tuple(
            {"participant": slot, "displayName": name, "entries": ()}
            for slot, name in (
                ("participant-a", "アロナ"),
                ("participant-b", "プラナ"),
                ("participant-c", "安倍晋三AI"),
            )
        ),
        "nextCursor": None,
    }

    assert AffectionRankingsResponse.model_validate(payload).next_cursor is None
    payload.pop("nextCursor")
    with pytest.raises(ValidationError):
        AffectionRankingsResponse.model_validate(payload)


def test_affection_ranking_reset_count_is_public_and_defaults_for_old_pages() -> None:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "generatedAt": datetime(2026, 8, 30, tzinfo=UTC),
        "defaultScore": 500,
        "maxScore": 1000,
        "rankings": tuple(
            {
                "participant": slot,
                "displayName": name,
                "entries": (
                    {
                        "rank": 1,
                        "displayName": "Requester",
                        "avatar": {
                            "kind": "placeholder",
                            "alt": "Requester avatar",
                            "fallbackVariant": "cyan",
                        },
                        "score": 1000,
                        **({"resetCount": 2} if slot == "participant-a" else {}),
                    },
                ),
            }
            for slot, name in (
                ("participant-a", "アロナ"),
                ("participant-b", "プラナ"),
                ("participant-c", "安倍晋三AI"),
            )
        ),
        "nextCursor": None,
    }

    result = AffectionRankingsResponse.model_validate(payload)

    assert result.rankings[0].entries[0].reset_count == 2
    assert result.rankings[1].entries[0].reset_count == 0


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
        "schemaVersion": 2,
        "recordId": "r" * 43,
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
        "affection": None,
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
            "isAdmin": True,
        }
    )

    payload = response.model_dump(by_alias=True, mode="json")

    assert payload["schemaVersion"] == 1
    assert payload["user"]["displayName"] == "利用者"

    legacy_response = SessionResponse.model_validate(
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
    assert legacy_response.root.is_admin is False
    with pytest.raises(ValidationError):
        SessionResponse.model_validate({"authenticated": False, "privateId": "forbidden"})


def test_record_list_does_not_expose_internal_or_evidence_fields() -> None:
    payload = RecordListResponse(
        schema_version=1,
        items=(
            {
                "schemaVersion": 1,
                "recordId": "r" * 43,
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


def test_record_list_requires_nonempty_next_cursor() -> None:
    assert (
        RecordListResponse.model_validate(
            {"schemaVersion": 1, "items": [], "nextCursor": "cursor-example"}
        ).next_cursor
        == "cursor-example"
    )
    assert (
        RecordListResponse.model_validate(
            {"schemaVersion": 1, "items": [], "nextCursor": None}
        ).next_cursor
        is None
    )

    with pytest.raises(ValidationError):
        RecordListResponse.model_validate({"schemaVersion": 1, "items": [], "nextCursor": ""})


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


def test_cost_contract_is_jpy_only_and_rejects_internal_ledger_fields() -> None:
    payload = {
        "schemaVersion": 1,
        "period": "week",
        "timeZone": "Asia/Tokyo",
        "startDate": "2026-08-17",
        "endDate": "2026-08-23",
        "currency": "JPY",
        "total": "12.345678",
        "breakdown": {
            "fargate": "1.000000",
            "lambda": "2.000000",
            "openai": "3.000000",
            "otherAws": "6.345678",
        },
        "conversion": {
            "source": "frankfurter-v2",
            "method": "daily-reference-rate",
            "baseCurrency": "USD",
            "updatedAt": "2026-08-23T12:17:00+09:00",
        },
        "updatedAt": "2026-08-23T12:17:00+09:00",
        "status": "partial",
    }

    response = CostsResponse.model_validate(payload)

    assert response.total == "12.345678"
    for private_field in ("amountUsd", "rate", "projectId", "checkpoint"):
        with pytest.raises(ValidationError):
            CostsResponse.model_validate({**payload, private_field: "forbidden"})
    with pytest.raises(ValidationError):
        CostsResponse.model_validate({**payload, "currency": "USD"})
    with pytest.raises(ValidationError):
        CostsResponse.model_validate({**payload, "total": "12.3456789"})
    with pytest.raises(ValidationError):
        CostsResponse.model_validate({**payload, "total": "12"})


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


def test_record_detail_affection_requires_all_three_consistent_changes() -> None:
    payload = _record_detail_payload()
    payload["affection"] = {
        "status": "applied",
        "rubricVersion": "affection-rubric-v1",
        "participants": (
            {
                "participant": "participant-a",
                "before": 500,
                "questionScore": 35,
                "appliedDelta": 35,
                "after": 535,
            },
            {
                "participant": "participant-b",
                "before": 10,
                "questionScore": -43,
                "appliedDelta": -10,
                "after": 0,
            },
            {
                "participant": "participant-c",
                "before": 987,
                "questionScore": 50,
                "appliedDelta": 13,
                "after": 1000,
            },
        ),
    }

    result = RecordDetailResponse.model_validate(payload)

    assert result.affection is not None
    assert result.affection.participants[2].applied_delta == 13
    invalid = dict(payload)
    invalid_affection = dict(cast(dict[str, object], payload["affection"]))
    invalid_entries = list(cast(tuple[dict[str, object], ...], invalid_affection["participants"]))
    invalid_entries[0] = {**invalid_entries[0], "after": 534}
    invalid_affection["participants"] = tuple(invalid_entries)
    invalid["affection"] = invalid_affection
    with pytest.raises(ValidationError, match="applied_delta"):
        RecordDetailResponse.model_validate(invalid)
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


def test_final_decision_rejects_whitespace_only_victory_message() -> None:
    payload = _record_detail_payload()
    final_decision = payload["finalDecision"]
    assert isinstance(final_decision, dict)
    final_decision["victoryMessage"] = " \t "

    with pytest.raises(ValidationError):
        RecordDetailResponse.model_validate(payload)


def test_memorial_state_enumerates_content_free_memory_summaries() -> None:
    payload = {
        "schemaVersion": 1,
        "state": "locked",
        "cycle": 3,
        "resetCount": 2,
        "unlockedParticipant": None,
        "unlockedAt": None,
        "uploadReady": False,
        "latestReadyCycle": 2,
        "memories": (
            {
                "cycle": 1,
                "participant": "participant-a",
                "unlockedAt": "2026-08-30T01:00:00Z",
                "generatedAt": "2026-08-30T01:10:00Z",
            },
            {
                "cycle": 2,
                "participant": "participant-c",
                "unlockedAt": "2026-08-31T01:00:00Z",
                "generatedAt": "2026-08-31T01:10:00Z",
            },
        ),
    }

    response = MemorialStateResponse.model_validate(payload)

    assert [item.cycle for item in response.memories] == [1, 2]
    serialized = response.model_dump_json(by_alias=True)
    for private_name in ("requesterKey", "ownerKey", "imageAssetKey", "narrative"):
        assert private_name not in serialized
    with pytest.raises(ValidationError, match="unique ascending"):
        MemorialStateResponse.model_validate(
            {**payload, "memories": tuple(reversed(cast(tuple[object, ...], payload["memories"])))}
        )
    with pytest.raises(ValidationError, match="must match memories"):
        MemorialStateResponse.model_validate({**payload, "latestReadyCycle": 1})


def test_memorial_upload_contract_is_strict_and_bounded() -> None:
    request = {
        "schemaVersion": 1,
        "expectedCycle": 1,
        "contentType": "image/webp",
        "sizeBytes": 10 * 1024 * 1024,
        "sha256": "a" * 64,
    }

    assert MemorialUploadRequest.model_validate(request).size_bytes == 10 * 1024 * 1024
    for invalid in (
        {**request, "schemaVersion": True},
        {**request, "schemaVersion": 1.0},
        {**request, "expectedCycle": 0},
        {**request, "sizeBytes": 10 * 1024 * 1024 + 1},
        {**request, "contentType": "image/gif"},
        {**request, "sha256": "A" * 64},
        {**request, "requesterKey": "private"},
    ):
        with pytest.raises(ValidationError):
            MemorialUploadRequest.model_validate(invalid)


def test_memorial_presigned_post_contract_allows_only_fixed_s3_fields() -> None:
    payload = {
        "schemaVersion": 1,
        "cycle": 1,
        "method": "POST",
        "uploadUrl": "https://upload.example.invalid/",
        "expiresAt": "2026-09-03T01:05:00Z",
        "fields": {
            "key": "opaque/upload",
            "Content-Type": "image/png",
            "x-amz-checksum-sha256": "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
            "x-amz-algorithm": "AWS4-HMAC-SHA256",
            "x-amz-credential": "credential/scope",
            "x-amz-date": "20260903T010000Z",
            "policy": "cG9saWN5",
            "x-amz-signature": "b" * 64,
        },
    }

    assert MemorialUploadResponse.model_validate(payload).method == "POST"
    with pytest.raises(ValidationError):
        MemorialUploadResponse.model_validate(
            {
                **payload,
                "fields": {**payload["fields"], "privateOwner": "forbidden"},
            }
        )
    with pytest.raises(ValidationError):
        MemorialUploadResponse.model_validate({**payload, "method": "PUT"})


def test_memorial_confirmation_and_memory_contracts_are_content_free() -> None:
    assert (
        MemorialGenerateRequest.model_validate(
            {"schemaVersion": 1, "expectedCycle": 1, "confirmation": "GENERATE MEMORIAL"}
        ).confirmation
        == "GENERATE MEMORIAL"
    )
    assert (
        MemorialResetRequest.model_validate(
            {"schemaVersion": 1, "expectedCycle": 1, "confirmation": "RESET AFFECTION"}
        ).confirmation
        == "RESET AFFECTION"
    )
    with pytest.raises(ValidationError):
        MemorialGenerateRequest.model_validate(
            {"schemaVersion": 1, "expectedCycle": 1, "confirmation": "generate memorial"}
        )
    memory = {
        "schemaVersion": 1,
        "cycle": 1,
        "participant": "participant-b",
        "unlockedAt": "2026-09-03T01:00:00Z",
        "generatedAt": "2026-09-03T01:10:00Z",
        "image": {
            "url": "https://media.example.invalid/memory.png",
            "width": 1920,
            "height": 1080,
            "alt": "メモリアルロビー",
        },
        "narrative": "大切な思い出です。",
    }
    assert MemorialMemoryResponse.model_validate(memory).cycle == 1
    with pytest.raises(ValidationError):
        MemorialMemoryResponse.model_validate({**memory, "requesterKey": "private"})
