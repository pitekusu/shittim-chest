"""Projector and bounded Backfill service tests."""

from __future__ import annotations

from typing import Any, cast

from tests.factories import NOW

from shittim_records.adapters import BackfillCheckpoint
from shittim_records.projector import BackfillService, ProjectionResult


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
