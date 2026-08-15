"""Projector and bounded backfill application services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from shittim_records.adapters import (
    ArchiveRepository,
    BackfillMode,
    ConfigurationRepository,
    SourceDebateRepository,
    StatisticsRepository,
)
from shittim_records.archive import ArchiveProjection, project_completed_debate


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    created: bool


class ProjectorService:
    """Re-read, validate, and immutably project one source partition."""

    def __init__(
        self,
        *,
        source: SourceDebateRepository,
        archive: ArchiveRepository,
        configuration: ConfigurationRepository,
    ) -> None:
        self._source = source
        self._archive = archive
        self._configuration = configuration

    def project_partition(self, partition_key: str, *, now: datetime) -> ProjectionResult:
        projection = self._prepare_partition(partition_key, now=now)
        return ProjectionResult(created=self._archive.put_projection(projection))

    def validate_partition(self, partition_key: str, *, now: datetime) -> None:
        """Run the complete source validation without writing the Archive."""

        self._prepare_partition(partition_key, now=now)

    def _prepare_partition(self, partition_key: str, *, now: datetime) -> ArchiveProjection:
        config = self._configuration.load()
        snapshot = self._source.load_partition(partition_key)
        return project_completed_debate(
            snapshot,
            identity_hmac_key=config.identity_hmac_key,
            presentation=config.presentation,
            projected_at=now,
        )


@dataclass(frozen=True, slots=True)
class BackfillResult:
    candidates: int
    validated: int
    projected: int
    skipped: int
    complete: bool


class BackfillService:
    """Process one bounded source Scan page through the same Projector."""

    def __init__(
        self,
        *,
        source: SourceDebateRepository,
        projector: ProjectorService,
        statistics: StatisticsRepository,
    ) -> None:
        self._source = source
        self._projector = projector
        self._statistics = statistics

    def run_page(
        self,
        *,
        apply: bool,
        now: datetime,
        page_limit: int = 100,
    ) -> BackfillResult:
        mode: BackfillMode = "apply" if apply else "dry-run"
        checkpoint = self._statistics.load_backfill_checkpoint(mode=mode)
        if checkpoint is not None and checkpoint.complete:
            return BackfillResult(
                candidates=checkpoint.candidate_count,
                validated=checkpoint.validated_count,
                projected=checkpoint.projected_count,
                skipped=checkpoint.skipped_count,
                complete=True,
            )
        partition_keys, last_key = self._source.scan_completed_meta(
            exclusive_start_key=(
                checkpoint.exclusive_start_key if checkpoint is not None else None
            ),
            limit=page_limit,
        )
        validated = 0
        projected = 0
        skipped = 0
        for partition_key in partition_keys:
            if not apply:
                self._projector.validate_partition(partition_key, now=now)
                validated += 1
                continue
            result = self._projector.project_partition(partition_key, now=now)
            validated += 1
            if result.created:
                projected += 1
            else:
                skipped += 1
        previous_candidates = checkpoint.candidate_count if checkpoint is not None else 0
        previous_validated = checkpoint.validated_count if checkpoint is not None else 0
        previous_projected = checkpoint.projected_count if checkpoint is not None else 0
        previous_skipped = checkpoint.skipped_count if checkpoint is not None else 0
        cumulative_candidates = previous_candidates + len(partition_keys)
        cumulative_validated = previous_validated + validated
        cumulative_projected = previous_projected + projected
        cumulative_skipped = previous_skipped + skipped
        self._statistics.save_backfill_checkpoint(
            mode=mode,
            exclusive_start_key=last_key,
            candidate_count=cumulative_candidates,
            validated_count=cumulative_validated,
            projected_count=cumulative_projected,
            skipped_count=cumulative_skipped,
            updated_at=now.astimezone(UTC).isoformat(),
        )
        return BackfillResult(
            candidates=cumulative_candidates,
            validated=cumulative_validated,
            projected=cumulative_projected,
            skipped=cumulative_skipped,
            complete=last_key is None,
        )
