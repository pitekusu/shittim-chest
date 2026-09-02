"""AWS adapter boundary tests without network access."""

from __future__ import annotations

from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    PREVIOUS_SCHEMA_VERSION,
    DynamoItem,
    serialize_snapshot,
)
from tests.factories import NOW, completed_snapshot, presentation

from shittim_records.adapters import (
    AffectionProjectionRepository,
    ArchiveRepository,
    ProjectionConflict,
    SourceDebateRepository,
    StatisticsRepository,
)
from shittim_records.archive import ArchiveProjection, project_completed_debate

HMAC_KEY = b"records-test-key-that-is-longer-than-32-bytes"


class FakeDynamoDb:
    def __init__(self) -> None:
        self.marker: dict[str, Any] | None = None
        self.transactions: list[dict[str, Any]] = []

    def get_item(self, **_kwargs: object) -> dict[str, object]:
        return {} if self.marker is None else {"Item": self.marker}

    def transact_write_items(self, **kwargs: object) -> dict[str, object]:
        self.transactions.append(dict(kwargs))
        return {}

    def put_item(self, **kwargs: object) -> dict[str, object]:
        self.marker = cast(dict[str, Any], kwargs["Item"])
        return {}


class FakeSourceDynamoDb:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.queries: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        return self.pages[len(self.queries) - 1]


def projection() -> object:
    return project_completed_debate(
        completed_snapshot(),
        identity_hmac_key=HMAC_KEY,
        presentation=presentation(),
        projected_at=NOW,
    )


def test_source_repository_reads_all_pages_with_strong_consistency() -> None:
    source = completed_snapshot()
    items = tuple(serialize_snapshot(source))
    middle = len(items) // 2
    cursor = marshal_item({"PK": items[middle - 1]["PK"], "SK": items[middle - 1]["SK"]})
    client = FakeSourceDynamoDb(
        [
            {
                "Items": [marshal_item(item) for item in items[:middle]],
                "LastEvaluatedKey": cursor,
            },
            {"Items": [marshal_item(item) for item in items[middle:]]},
        ]
    )

    restored = SourceDebateRepository(cast(Any, client), "source").load_partition(
        str(items[0]["PK"])
    )

    assert restored == source
    assert len(client.queries) == 2
    assert all(query["ConsistentRead"] is True for query in client.queries)
    assert "ExclusiveStartKey" not in client.queries[0]
    assert client.queries[1]["ExclusiveStartKey"] == cursor


def test_source_repository_accepts_previous_source_schema() -> None:
    source = completed_snapshot()
    items = tuple(
        {**item, "schema_version": PREVIOUS_SCHEMA_VERSION} for item in serialize_snapshot(source)
    )
    client = FakeSourceDynamoDb([{"Items": [marshal_item(item) for item in items]}])

    restored = SourceDebateRepository(cast(Any, client), "source").load_partition(
        str(items[0]["PK"])
    )

    assert restored.state.schema_version == CURRENT_SCHEMA_VERSION
    assert restored.final_decision == source.final_decision

    projection_client = FakeSourceDynamoDb([{"Items": [marshal_item(item) for item in items]}])
    loaded = SourceDebateRepository(
        cast(Any, projection_client), "source"
    ).load_partition_for_projection(str(items[0]["PK"]))
    assert loaded.schema_version == PREVIOUS_SCHEMA_VERSION
    assert loaded.snapshot.state.schema_version == CURRENT_SCHEMA_VERSION


def test_source_repository_accepts_mixed_partition_with_current_completion_metadata() -> None:
    source = completed_snapshot()
    items = [
        {
            **item,
            "schema_version": (
                CURRENT_SCHEMA_VERSION
                if item.get("record_type") in {"debate_meta", "attempt_meta"}
                else PREVIOUS_SCHEMA_VERSION
            ),
        }
        for item in serialize_snapshot(source)
    ]
    client = FakeSourceDynamoDb([{"Items": [marshal_item(item) for item in items]}])

    loaded = SourceDebateRepository(cast(Any, client), "source").load_partition_for_projection(
        str(items[0]["PK"])
    )

    assert loaded.schema_version == CURRENT_SCHEMA_VERSION
    assert loaded.snapshot.state.schema_version == CURRENT_SCHEMA_VERSION
    assert loaded.snapshot.final_decision == source.final_decision


@pytest.mark.parametrize("invalid_schema", (True, 7, 10))
def test_source_repository_rejects_invalid_item_schema(invalid_schema: object) -> None:
    items = list(serialize_snapshot(completed_snapshot()))
    items[2] = cast(DynamoItem, {**items[2], "schema_version": invalid_schema})
    client = FakeSourceDynamoDb([{"Items": [marshal_item(cast(Any, item)) for item in items]}])

    with pytest.raises(ValueError, match="partition schema"):
        SourceDebateRepository(cast(Any, client), "source").load_partition_for_projection(
            str(items[0]["PK"])
        )


@pytest.mark.parametrize(
    ("record_type", "mutation", "message"),
    (
        ("debate_meta", {"current_phase": "running"}, "debate metadata"),
        ("attempt_meta", {"phase": "running"}, "attempt metadata"),
        (
            "attempt_meta",
            {"schema_version": PREVIOUS_SCHEMA_VERSION},
            "metadata schema",
        ),
    ),
)
def test_source_repository_rejects_invalid_completion_metadata(
    record_type: str,
    mutation: dict[str, object],
    message: str,
) -> None:
    items = list(serialize_snapshot(completed_snapshot()))
    index = next(
        index for index, item in enumerate(items) if item.get("record_type") == record_type
    )
    items[index] = cast(DynamoItem, {**items[index], **mutation})
    client = FakeSourceDynamoDb([{"Items": [marshal_item(item) for item in items]}])

    with pytest.raises(ValueError, match=message):
        SourceDebateRepository(cast(Any, client), "source").load_partition_for_projection(
            str(items[0]["PK"])
        )


@pytest.mark.parametrize("record_type", ("debate_meta", "attempt_meta"))
@pytest.mark.parametrize("mode", ("missing", "duplicate"))
def test_source_repository_rejects_missing_or_duplicate_completion_metadata(
    record_type: str,
    mode: str,
) -> None:
    items = list(serialize_snapshot(completed_snapshot()))
    index = next(
        index for index, item in enumerate(items) if item.get("record_type") == record_type
    )
    if mode == "missing":
        del items[index]
    else:
        items.append(dict(items[index]))
    client = FakeSourceDynamoDb([{"Items": [marshal_item(item) for item in items]}])

    with pytest.raises(ValueError, match=r"debate metadata|attempt metadata"):
        SourceDebateRepository(cast(Any, client), "source").load_partition_for_projection(
            str(items[0]["PK"])
        )


def test_archive_repository_creates_all_items_in_one_transaction() -> None:
    client = FakeDynamoDb()
    repository = ArchiveRepository(cast(Any, client), "archive")
    value = projection()

    assert repository.put_projection(cast(Any, value)) is True

    assert len(client.transactions) == 1
    assert len(client.transactions[0]["TransactItems"]) == 12
    assert str(client.transactions[0]["ClientRequestToken"]).startswith("records-")


def test_archive_repository_treats_same_marker_as_idempotent_noop() -> None:
    value = cast(Any, projection())
    client = FakeDynamoDb()
    client.marker = marshal_item(
        {
            "PK": f"RECORD#{value.record_id}",
            "SK": "PROJECTION#V1",
            "source_fingerprint": value.source_fingerprint,
        }
    )

    assert ArchiveRepository(cast(Any, client), "archive").put_projection(value) is False
    assert client.transactions == []


def test_archive_repository_rejects_conflicting_marker() -> None:
    value = cast(Any, projection())
    client = FakeDynamoDb()
    client.marker = marshal_item(
        {
            "PK": f"RECORD#{value.record_id}",
            "SK": "PROJECTION#V1",
            "source_fingerprint": "f" * 64,
        }
    )

    with pytest.raises(ProjectionConflict):
        ArchiveRepository(cast(Any, client), "archive").put_projection(value)


def test_statistics_repository_preserves_completed_checkpoint_state() -> None:
    client = FakeDynamoDb()
    repository = StatisticsRepository(cast(Any, client), "statistics")

    repository.save_backfill_checkpoint(
        mode="apply",
        exclusive_start_key=None,
        candidate_count=3,
        validated_count=3,
        projected_count=2,
        skipped_count=1,
        updated_at="2026-08-15T12:00:00+00:00",
    )

    checkpoint = repository.load_backfill_checkpoint(mode="apply")
    assert checkpoint is not None
    assert checkpoint.complete is True
    assert checkpoint.exclusive_start_key is None
    assert checkpoint.candidate_count == 3
    assert checkpoint.validated_count == 3
    assert checkpoint.projected_count == 2
    assert checkpoint.skipped_count == 1


def test_statistics_repository_uses_independent_dry_run_and_apply_keys() -> None:
    client = FakeDynamoDb()
    repository = StatisticsRepository(cast(Any, client), "statistics")

    repository.save_backfill_checkpoint(
        mode="dry-run",
        exclusive_start_key={"PK": "DEBATE#cursor"},
        candidate_count=2,
        validated_count=2,
        projected_count=0,
        skipped_count=0,
        updated_at="2026-08-15T12:00:00+00:00",
    )

    item = unmarshal_item(cast(Any, client.marker))
    assert item["SK"] == "ARCHIVE#V1#DRY-RUN"
    assert item["mode"] == "dry-run"
    assert item["exclusive_start_key"] == {"PK": "DEBATE#cursor"}

    repository.save_backfill_checkpoint(
        mode="apply",
        exclusive_start_key=None,
        candidate_count=1,
        validated_count=1,
        projected_count=1,
        skipped_count=0,
        updated_at="2026-08-15T12:05:00+00:00",
    )

    item = unmarshal_item(cast(Any, client.marker))
    assert item["SK"] == "ARCHIVE#V1#APPLY"
    assert item["mode"] == "apply"


def test_statistics_repository_rejects_archive_counts_for_dry_run() -> None:
    repository = StatisticsRepository(cast(Any, FakeDynamoDb()), "statistics")

    with pytest.raises(ValueError, match="cannot contain Archive writes"):
        repository.save_backfill_checkpoint(
            mode="dry-run",
            exclusive_start_key=None,
            candidate_count=1,
            validated_count=1,
            projected_count=1,
            skipped_count=0,
            updated_at="2026-08-15T12:00:00+00:00",
        )


class ConditionalProfileClient:
    def __init__(self, existing: dict[str, object]) -> None:
        self.existing = existing
        self.puts: list[dict[str, object]] = []

    def put_item(self, **kwargs: object) -> None:
        self.puts.append(dict(kwargs))
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "PutItem",
        )

    def get_item(self, **_kwargs: object) -> dict[str, object]:
        return {"Item": marshal_item(cast(Any, self.existing))}


class LegacyUpgradeProfileClient(ConditionalProfileClient):
    def put_item(self, **kwargs: object) -> None:
        self.puts.append(dict(kwargs))
        if self.existing.get("schema_version") == 1:
            condition = cast(str, kwargs["ConditionExpression"])
            assert "schema_version = :legacy_schema" in condition
            values = unmarshal_item(cast(Any, kwargs["ExpressionAttributeValues"]))
            assert values[":display_name"] == self.existing["display_name"]
            assert values[":scores"] == self.existing["scores"]
            self.existing = unmarshal_item(cast(Any, kwargs["Item"]))
            return
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "PutItem",
        )


def projected_profile(*, version: int, updated_at: str) -> dict[str, object]:
    return {
        "PK": "AFFECTION#PROFILE",
        "SK": "a" * 43,
        "schema_version": 2,
        "record_type": "affection_profile",
        "source_version": version,
        "display_name": "Requester",
        "scores": {
            "participant-a": 500,
            "participant-b": 500,
            "participant-c": 500,
        },
        "updated_at": updated_at,
        "reset_count": 0,
        "memorial_cycle": 1,
    }


def test_affection_projection_skips_strictly_older_source_event() -> None:
    existing = projected_profile(version=3, updated_at="2026-08-15T12:03:00+00:00")
    client = ConditionalProfileClient(existing)
    incoming = projected_profile(version=2, updated_at="2026-08-15T12:02:00+00:00")

    assert (
        AffectionProjectionRepository(cast(Any, client), "statistics").put_profile(
            cast(Any, incoming)
        )
        is False
    )


def test_affection_projection_rejects_same_version_with_different_content() -> None:
    existing = projected_profile(version=3, updated_at="2026-08-15T12:03:00+00:00")
    client = ConditionalProfileClient(existing)
    incoming = {**existing, "display_name": "Conflicting"}

    with pytest.raises(ProjectionConflict, match="one version"):
        AffectionProjectionRepository(cast(Any, client), "statistics").put_profile(
            cast(Any, incoming)
        )


def test_affection_projection_upgrades_same_version_legacy_defaults_once() -> None:
    incoming = projected_profile(version=3, updated_at="2026-08-15T12:03:00+00:00")
    existing = {
        key: value
        for key, value in incoming.items()
        if key not in {"reset_count", "memorial_cycle"}
    } | {"schema_version": 1}
    client = LegacyUpgradeProfileClient(existing)
    repository = AffectionProjectionRepository(cast(Any, client), "statistics")

    assert repository.put_profile(cast(Any, incoming)) is True
    assert client.existing == incoming
    assert repository.put_profile(cast(Any, incoming)) is False


@pytest.mark.parametrize("field", ("display_name", "scores"))
def test_affection_projection_rejects_legacy_upgrade_with_content_difference(
    field: str,
) -> None:
    incoming = projected_profile(version=3, updated_at="2026-08-15T12:03:00+00:00")
    existing = {
        key: value
        for key, value in incoming.items()
        if key not in {"reset_count", "memorial_cycle"}
    } | {"schema_version": 1}
    existing[field] = (
        "Different"
        if field == "display_name"
        else {
            "participant-a": 499,
            "participant-b": 500,
            "participant-c": 500,
        }
    )
    client = ConditionalProfileClient(existing)

    with pytest.raises(ProjectionConflict, match="one version"):
        AffectionProjectionRepository(cast(Any, client), "statistics").put_profile(
            cast(Any, incoming)
        )


def test_affection_projection_rejects_crossed_version_and_timestamp_order() -> None:
    existing = projected_profile(version=3, updated_at="2026-08-15T12:03:00+00:00")
    client = ConditionalProfileClient(existing)
    incoming = projected_profile(version=2, updated_at="2026-08-15T12:04:00+00:00")

    with pytest.raises(ProjectionConflict, match="ordering"):
        AffectionProjectionRepository(cast(Any, client), "statistics").put_profile(
            cast(Any, incoming)
        )


def test_affection_projection_condition_requires_monotonic_version_and_timestamp() -> None:
    existing = projected_profile(version=3, updated_at="2026-08-15T12:03:00+00:00")
    client = ConditionalProfileClient(existing)
    incoming = projected_profile(version=4, updated_at="2026-08-15T12:02:00+00:00")

    with pytest.raises(ProjectionConflict, match="ordering"):
        AffectionProjectionRepository(cast(Any, client), "statistics").put_profile(
            cast(Any, incoming)
        )
    assert cast(str, client.puts[0]["ConditionExpression"]).startswith(
        "attribute_not_exists(PK) OR attribute_not_exists(source_version) "
        "OR source_version = :seed_version "
        "OR (source_version < :version AND updated_at <= :updated_at)"
    )


@pytest.mark.parametrize(
    "items, message",
    (
        (
            tuple(
                {"PK": "RECORD#opaque", "SK": f"ITEM#{index}", "payload": "x" * 350_000}
                for index in range(12)
            ),
            "transaction size",
        ),
        (
            tuple({"PK": "RECORD#opaque", "SK": f"ITEM#{index}"} for index in range(100)),
            "action count",
        ),
    ),
)
def test_archive_repository_rejects_oversized_transactions(
    items: tuple[dict[str, Any], ...],
    message: str,
) -> None:
    value = ArchiveProjection(
        record_id="opaque",
        source_fingerprint="a" * 64,
        items=cast(Any, items),
    )

    with pytest.raises(ValueError, match=message):
        ArchiveRepository(cast(Any, FakeDynamoDb()), "archive").put_projection(value)
