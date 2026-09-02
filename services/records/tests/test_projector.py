"""Projector and bounded Backfill service tests."""

from __future__ import annotations

from typing import Any, cast

import pytest
from shittim_chest.adapters.dynamodb.serializer import DynamoItem
from tests.factories import NOW

from shittim_records.adapters import BackfillCheckpoint
from shittim_records.archive import derive_requester_key
from shittim_records.projector import (
    AffectionProjectorService,
    BackfillService,
    ProjectionResult,
    project_affection_profile,
)

HMAC_KEY = b"records-test-key-that-is-longer-than-32-bytes"


def legacy_affection_profile(
    *,
    scores: tuple[int, int, int] = (625, 55, 987),
    version: int = 3,
) -> DynamoItem:
    return cast(
        DynamoItem,
        {
            "PK": "AFFECTION#REQUESTER#private-user",
            "SK": "PROFILE",
            "record_type": "affection_profile",
            "schema_version": 8,
            "requester_id": "private-user",
            "requester_username": "private-name",
            "requester_display_name": "Requester",
            "scores": list(scores),
            "version": version,
            "updated_at": NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        },
    )


class FakeSource:
    def __init__(self) -> None:
        self.scans = 0
        self.start_keys: list[object] = []

    def scan_completed_meta(self, **kwargs: object) -> tuple[list[str], dict[str, Any] | None]:
        self.scans += 1
        self.start_keys.append(kwargs["exclusive_start_key"])
        return ["DEBATE#one", "DEBATE#two"], {"PK": "DEBATE#next"}


class FakeProjector:
    def __init__(self) -> None:
        self.validated: list[str] = []
        self.projected: list[str] = []

    def validate_partition(self, partition_key: str, *, now: object) -> None:
        del now
        self.validated.append(partition_key)

    def project_partition(self, partition_key: str, *, now: object) -> ProjectionResult:
        del now
        self.projected.append(partition_key)
        return ProjectionResult(created=partition_key.endswith("one"))


class FakeStatistics:
    def __init__(
        self,
        checkpoints: dict[str, BackfillCheckpoint] | None = None,
    ) -> None:
        self.checkpoints = checkpoints or {}
        self.loaded: list[str] = []
        self.saved: list[dict[str, object]] = []

    def load_backfill_checkpoint(self, *, mode: str) -> BackfillCheckpoint | None:
        self.loaded.append(mode)
        return self.checkpoints.get(mode)

    def save_backfill_checkpoint(self, **kwargs: object) -> None:
        self.saved.append(dict(kwargs))


def service(
    projector: FakeProjector,
    statistics: FakeStatistics,
    source: FakeSource | None = None,
) -> BackfillService:
    return BackfillService(
        source=cast(Any, source or FakeSource()),
        projector=cast(Any, projector),
        statistics=cast(Any, statistics),
    )


def test_backfill_dry_run_validates_every_candidate_and_saves_only_its_checkpoint() -> None:
    projector = FakeProjector()
    statistics = FakeStatistics()

    result = service(projector, statistics).run_page(apply=False, now=NOW, page_limit=100)

    assert result.candidates == 2
    assert result.validated == 2
    assert result.projected == 0
    assert result.skipped == 0
    assert result.complete is False
    assert projector.validated == ["DEBATE#one", "DEBATE#two"]
    assert projector.projected == []
    assert statistics.loaded == ["dry-run"]
    assert statistics.saved == [
        {
            "mode": "dry-run",
            "exclusive_start_key": {"PK": "DEBATE#next"},
            "candidate_count": 2,
            "validated_count": 2,
            "projected_count": 0,
            "skipped_count": 0,
            "updated_at": NOW.isoformat(),
        }
    ]


def test_backfill_apply_projects_every_candidate_and_saves_checkpoint() -> None:
    projector = FakeProjector()
    statistics = FakeStatistics()

    result = service(projector, statistics).run_page(apply=True, now=NOW, page_limit=100)

    assert result.candidates == 2
    assert result.validated == 2
    assert result.projected == 1
    assert result.skipped == 1
    assert projector.validated == []
    assert projector.projected == ["DEBATE#one", "DEBATE#two"]
    assert len(statistics.saved) == 1
    assert statistics.saved[0]["mode"] == "apply"
    assert statistics.saved[0]["candidate_count"] == 2
    assert statistics.saved[0]["validated_count"] == 2
    assert statistics.saved[0]["projected_count"] == 1
    assert statistics.saved[0]["skipped_count"] == 1


def test_backfill_apply_accumulates_checkpoint_counts() -> None:
    projector = FakeProjector()
    statistics = FakeStatistics(
        {
            "apply": BackfillCheckpoint(
                exclusive_start_key={"PK": "DEBATE#previous"},
                complete=False,
                candidate_count=7,
                validated_count=7,
                projected_count=3,
                skipped_count=4,
            )
        }
    )

    service(projector, statistics).run_page(apply=True, now=NOW, page_limit=100)

    assert statistics.saved[0]["projected_count"] == 4
    assert statistics.saved[0]["skipped_count"] == 5
    assert statistics.saved[0]["candidate_count"] == 9
    assert statistics.saved[0]["validated_count"] == 9


def test_second_dry_run_page_resumes_without_reprocessing_the_first_page() -> None:
    projector = FakeProjector()
    statistics = FakeStatistics(
        {
            "dry-run": BackfillCheckpoint(
                exclusive_start_key={"PK": "DEBATE#previous"},
                complete=False,
                candidate_count=3,
                validated_count=3,
            )
        }
    )
    source = FakeSource()

    result = service(projector, statistics, source).run_page(
        apply=False,
        now=NOW,
        page_limit=100,
    )

    assert result.candidates == 5
    assert result.validated == 5
    assert result.projected == result.skipped == 0
    assert statistics.saved[0]["candidate_count"] == 5
    assert source.start_keys == [{"PK": "DEBATE#previous"}]
    assert statistics.saved[0]["exclusive_start_key"] == {"PK": "DEBATE#next"}


def test_apply_starts_from_the_beginning_after_dry_run_checkpoint() -> None:
    projector = FakeProjector()
    statistics = FakeStatistics(
        {
            "dry-run": BackfillCheckpoint(
                exclusive_start_key=None,
                complete=True,
                candidate_count=8,
                validated_count=8,
            )
        }
    )

    result = service(projector, statistics).run_page(apply=True, now=NOW, page_limit=100)

    assert statistics.loaded == ["apply"]
    assert result.candidates == 2
    assert projector.projected == ["DEBATE#one", "DEBATE#two"]


def test_completed_backfill_checkpoint_is_a_terminal_noop() -> None:
    projector = FakeProjector()
    statistics = FakeStatistics(
        {
            "apply": BackfillCheckpoint(
                exclusive_start_key=None,
                complete=True,
                candidate_count=9,
                validated_count=9,
                projected_count=5,
                skipped_count=4,
            )
        }
    )
    source = FakeSource()

    result = service(projector, statistics, source).run_page(
        apply=True,
        now=NOW,
        page_limit=100,
    )

    assert result.complete is True
    assert result.candidates == result.validated == 9
    assert result.projected == 5
    assert result.skipped == 4
    assert source.scans == 0
    assert statistics.saved == []


def test_completed_dry_run_checkpoint_is_a_terminal_noop() -> None:
    projector = FakeProjector()
    statistics = FakeStatistics(
        {
            "dry-run": BackfillCheckpoint(
                exclusive_start_key=None,
                complete=True,
                candidate_count=6,
                validated_count=6,
            )
        }
    )
    source = FakeSource()

    result = service(projector, statistics, source).run_page(
        apply=False,
        now=NOW,
        page_limit=100,
    )

    assert result.candidates == result.validated == 6
    assert result.projected == result.skipped == 0
    assert result.complete is True
    assert source.scans == 0
    assert projector.validated == projector.projected == []
    assert statistics.saved == []


def test_affection_profile_projection_hashes_private_identity_and_normalizes_scores() -> None:
    source = legacy_affection_profile()

    assert source["updated_at"] == NOW.isoformat(timespec="microseconds").replace("+00:00", "Z")

    projected = project_affection_profile(source, identity_hmac_key=HMAC_KEY)

    assert projected == {
        "PK": "AFFECTION#PROFILE",
        "SK": derive_requester_key(HMAC_KEY, "private-user"),
        "schema_version": 2,
        "record_type": "affection_profile",
        "source_version": 3,
        "display_name": "Requester",
        "scores": {
            "participant-a": 625,
            "participant-b": 55,
            "participant-c": 987,
        },
        "updated_at": NOW.isoformat(),
        "reset_count": 0,
        "memorial_cycle": 1,
    }
    assert "private-user" not in repr(projected)
    assert "private-name" not in repr(projected)


def test_affection_projector_reloads_source_and_converges_statistics() -> None:
    source_item = legacy_affection_profile(scores=(500, 501, 502), version=1)

    class Source:
        def find_affection_profile(self, partition_key: str) -> DynamoItem:
            assert partition_key == source_item["PK"]
            return source_item

    class Store:
        def __init__(self) -> None:
            self.item: object = None

        def put_profile(self, item: object) -> bool:
            self.item = item
            return True

    class Configuration:
        def load(self) -> object:
            return type("Config", (), {"identity_hmac_key": HMAC_KEY})()

    store = Store()
    service = AffectionProjectorService(
        source=cast(Any, Source()),
        statistics=cast(Any, store),
        configuration=cast(Any, Configuration()),
    )

    assert service.project_partition(str(source_item["PK"])).created is True
    assert isinstance(store.item, dict)
    assert store.item["source_version"] == 1


def test_affection_projector_recovers_late_v8_event_from_migrated_v9_profile() -> None:
    requester_id = "123456789" + "012345678"
    requester_key = derive_requester_key(HMAC_KEY, requester_id)
    legacy_pk = f"AFFECTION#REQUESTER#{requester_id}"
    current_pk = f"AFFECTION#REQUESTER#{requester_key}"
    current = {
        "PK": current_pk,
        "SK": "PROFILE",
        "record_type": "affection_profile",
        "schema_version": 9,
        "requester_key": requester_key,
        "requester_username": "private-name",
        "requester_display_name": "Requester",
        "scores": [500, 501, 502],
        "version": 2,
        "updated_at": NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "reset_count": 0,
        "memorial_cycle": 1,
    }

    class Source:
        def find_affection_profile(self, partition_key: str) -> DynamoItem | None:
            return cast(DynamoItem, current) if partition_key == current_pk else None

    class Store:
        def __init__(self) -> None:
            self.calls = 0

        def put_profile(self, _item: object) -> bool:
            self.calls += 1
            return self.calls == 1

    class Configuration:
        def load(self) -> object:
            return type("Config", (), {"identity_hmac_key": HMAC_KEY})()

    service = AffectionProjectorService(
        source=cast(Any, Source()),
        statistics=cast(Any, Store()),
        configuration=cast(Any, Configuration()),
    )

    assert service.project_partition(legacy_pk).created is True
    assert service.project_partition(legacy_pk).created is False


@pytest.mark.parametrize(
    "partition_key",
    (
        "AFFECTION#REQUESTER#" + "123456789" + "012345678",
        f"AFFECTION#REQUESTER#{'a' * 43}",
        "AFFECTION#REQUESTER#not-a-snowflake",
    ),
)
def test_affection_projector_rejects_missing_unrecoverable_profile(
    partition_key: str,
) -> None:
    class Source:
        def find_affection_profile(self, _partition_key: str) -> None:
            return None

    class Configuration:
        def load(self) -> object:
            return type("Config", (), {"identity_hmac_key": HMAC_KEY})()

    service = AffectionProjectorService(
        source=cast(Any, Source()),
        statistics=cast(Any, object()),
        configuration=cast(Any, Configuration()),
    )

    with pytest.raises(ValueError, match="profile does not exist") as error:
        service.project_partition(partition_key)
    assert partition_key not in str(error.value)


def test_v9_affection_profile_uses_opaque_identity_and_projects_memorial_metadata() -> None:
    requester_key = "a" * 43
    source = {
        "PK": f"AFFECTION#REQUESTER#{requester_key}",
        "SK": "PROFILE",
        "record_type": "affection_profile",
        "schema_version": 9,
        "requester_key": requester_key,
        "requester_username": "private-name",
        "requester_display_name": "Requester",
        "scores": [1000, 600, 500],
        "version": 4,
        "updated_at": NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "reset_count": 2,
        "memorial_cycle": 3,
        "unlocked_participant": "participant-a",
        "unlocked_at": NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "unlock_debate_id": "019d2c1f-0000-7000-8000-a00000000021",
        "unlock_display_name": "Requester at unlock",
        "unlock_retroactive": False,
    }

    projected = project_affection_profile(cast(Any, source), identity_hmac_key=HMAC_KEY)

    assert projected["SK"] == requester_key
    assert projected["reset_count"] == 2
    assert projected["memorial_cycle"] == 3
    assert projected["unlocked_participant"] == "participant-a"
    assert projected["unlock_memorial_cycle"] == 3
    assert projected["unlock_display_name"] == "Requester at unlock"
    assert projected["unlock_retroactive"] is False
    assert len(cast(str, projected["unlock_record_id"])) == 43
    assert "019d2c1f-0000-7000-8000-a00000000021" not in repr(projected)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("requester_key", "é" * 43),
        ("unlock_debate_id", "00000000-0000-4000-8000-000000000000"),
    ),
)
def test_v9_affection_profile_rejects_non_ascii_key_or_non_uuidv7_unlock(
    field: str, value: str
) -> None:
    requester_key = "a" * 43
    source = {
        "PK": f"AFFECTION#REQUESTER#{requester_key}",
        "SK": "PROFILE",
        "record_type": "affection_profile",
        "schema_version": 9,
        "requester_key": requester_key,
        "requester_username": "private-name",
        "requester_display_name": "Requester",
        "scores": [1000, 600, 500],
        "version": 4,
        "updated_at": NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "reset_count": 2,
        "memorial_cycle": 3,
        "unlocked_participant": "participant-a",
        "unlocked_at": NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "unlock_debate_id": "019d2c1f-0000-7000-8000-a00000000021",
        "unlock_display_name": "Requester at unlock",
        "unlock_retroactive": True,
    }
    source[field] = value
    if field == "requester_key":
        source["PK"] = f"AFFECTION#REQUESTER#{value}"

    with pytest.raises(ValueError, match=r"identity|record key"):
        project_affection_profile(cast(Any, source), identity_hmac_key=HMAC_KEY)


@pytest.mark.parametrize("retroactive", (None, "false"))
def test_v9_affection_profile_rejects_missing_or_non_boolean_unlock_provenance(
    retroactive: object,
) -> None:
    requester_key = "a" * 43
    source = {
        "PK": f"AFFECTION#REQUESTER#{requester_key}",
        "SK": "PROFILE",
        "record_type": "affection_profile",
        "schema_version": 9,
        "requester_key": requester_key,
        "requester_username": "private-name",
        "requester_display_name": "Requester",
        "scores": [1000, 600, 500],
        "version": 4,
        "updated_at": NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "reset_count": 2,
        "memorial_cycle": 3,
        "unlocked_participant": "participant-a",
        "unlocked_at": NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "unlock_debate_id": "019d2c1f-0000-7000-8000-a00000000021",
        "unlock_display_name": "Requester at unlock",
        "unlock_retroactive": retroactive,
    }
    if retroactive is None:
        del source["unlock_retroactive"]

    with pytest.raises(ValueError, match=r"fields|incomplete|invalid"):
        project_affection_profile(cast(Any, source), identity_hmac_key=HMAC_KEY)


def test_v9_affection_profile_rejects_raw_identity_or_partial_unlock() -> None:
    requester_key = "a" * 43
    source = {
        "PK": f"AFFECTION#REQUESTER#{requester_key}",
        "SK": "PROFILE",
        "record_type": "affection_profile",
        "schema_version": 9,
        "requester_key": requester_key,
        "requester_username": "private-name",
        "requester_display_name": "Requester",
        "scores": [500, 500, 500],
        "version": 1,
        "updated_at": NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "reset_count": 0,
        "memorial_cycle": 1,
    }
    source["requester_id"] = "private-user"
    with pytest.raises(ValueError, match="fields"):
        project_affection_profile(cast(Any, source), identity_hmac_key=HMAC_KEY)
    del source["requester_id"]
    source["unlocked_participant"] = "participant-a"
    with pytest.raises(ValueError, match=r"fields|incomplete"):
        project_affection_profile(cast(Any, source), identity_hmac_key=HMAC_KEY)


def test_v9_affection_profile_rejects_inconsistent_reset_cycle() -> None:
    requester_key = "a" * 43
    source = {
        "PK": f"AFFECTION#REQUESTER#{requester_key}",
        "SK": "PROFILE",
        "record_type": "affection_profile",
        "schema_version": 9,
        "requester_key": requester_key,
        "requester_username": "private-name",
        "requester_display_name": "Requester",
        "scores": [500, 500, 500],
        "version": 2,
        "updated_at": NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "reset_count": 1,
        "memorial_cycle": 1,
    }

    with pytest.raises(ValueError, match="identity"):
        project_affection_profile(cast(Any, source), identity_hmac_key=HMAC_KEY)
