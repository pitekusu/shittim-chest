"""Projector and bounded Backfill service tests."""

from __future__ import annotations

from typing import Any, cast

from tests.factories import NOW

from shittim_records.adapters import BackfillCheckpoint
from shittim_records.projector import BackfillService, ProjectionResult


class FakeSource:
    def __init__(self) -> None:
        self.scans = 0

    def scan_completed_meta(self, **_kwargs: object) -> tuple[list[str], dict[str, Any] | None]:
        self.scans += 1
        return ["DEBATE#one", "DEBATE#two"], {"PK": {"S": "DEBATE#next"}}


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
    def __init__(self, checkpoint: BackfillCheckpoint | None = None) -> None:
        self.checkpoint = checkpoint
        self.saved: list[dict[str, object]] = []

    def load_backfill_checkpoint(self) -> BackfillCheckpoint | None:
        return self.checkpoint

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


def test_backfill_dry_run_validates_every_candidate_without_writes_or_checkpoint() -> None:
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
    assert statistics.saved == []


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
    assert statistics.saved[0]["projected_count"] == 1
    assert statistics.saved[0]["skipped_count"] == 1


def test_backfill_apply_accumulates_checkpoint_counts() -> None:
    projector = FakeProjector()
    statistics = FakeStatistics(
        BackfillCheckpoint(
            exclusive_start_key={"PK": "DEBATE#previous"},
            complete=False,
            projected_count=3,
            skipped_count=4,
        )
    )

    service(projector, statistics).run_page(apply=True, now=NOW, page_limit=100)

    assert statistics.saved[0]["projected_count"] == 4
    assert statistics.saved[0]["skipped_count"] == 5


def test_completed_backfill_checkpoint_is_a_terminal_noop() -> None:
    projector = FakeProjector()
    statistics = FakeStatistics(BackfillCheckpoint(exclusive_start_key=None, complete=True))
    source = FakeSource()

    result = service(projector, statistics, source).run_page(
        apply=True,
        now=NOW,
        page_limit=100,
    )

    assert result.complete is True
    assert result.candidates == result.validated == result.projected == result.skipped == 0
    assert source.scans == 0
    assert statistics.saved == []
