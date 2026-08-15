"""AWS adapter boundary tests without network access."""

from __future__ import annotations

from typing import Any, cast

import pytest
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import serialize_snapshot
from tests.factories import NOW, completed_snapshot, presentation

from shittim_records.adapters import (
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
    items = tuple({**item, "schema_version": 6} for item in serialize_snapshot(source))
    client = FakeSourceDynamoDb([{"Items": [marshal_item(item) for item in items]}])

    restored = SourceDebateRepository(cast(Any, client), "source").load_partition(
        str(items[0]["PK"])
    )

    assert restored.state.schema_version == 7
    assert restored.final_decision == source.final_decision


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
