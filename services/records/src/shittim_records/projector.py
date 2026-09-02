"""Projector and bounded backfill application services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from pydantic import AwareDatetime, TypeAdapter, ValidationError
from shittim_chest.adapters.dynamodb.serializer import DynamoItem, DynamoValue

from shittim_records.adapters import (
    AffectionProjectionRepository,
    ArchiveRepository,
    BackfillMode,
    ConfigurationRepository,
    SourceDebateRepository,
    StatisticsRepository,
)
from shittim_records.archive import (
    ArchiveProjection,
    derive_record_key,
    derive_requester_key,
    project_completed_debate,
)

PARTICIPANT_SLOTS = ("participant-a", "participant-b", "participant-c")
LEGACY_AFFECTION_SCHEMA_VERSION = 8
OPAQUE_AFFECTION_SCHEMA_VERSION = 9
OPAQUE_KEY_LENGTH = 43


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
        loaded = self._source.load_partition_for_projection(partition_key)
        return project_completed_debate(
            loaded.snapshot,
            identity_hmac_key=config.identity_hmac_key,
            presentation=config.presentation,
            projected_at=now,
            source_schema_version=loaded.schema_version,
        )


class AffectionProjectorService:
    """Project private source profiles to opaque, monotonic Statistics rows."""

    def __init__(
        self,
        *,
        source: SourceDebateRepository,
        statistics: AffectionProjectionRepository,
        configuration: ConfigurationRepository,
    ) -> None:
        self._source = source
        self._statistics = statistics
        self._configuration = configuration

    def project_partition(self, partition_key: str) -> ProjectionResult:
        config = self._configuration.load()
        source = self._source.find_affection_profile(partition_key)
        if source is None:
            requester_id = _legacy_requester_id(partition_key)
            if requester_id is None:
                raise ValueError("source affection profile does not exist")
            requester_key = derive_requester_key(config.identity_hmac_key, requester_id)
            source = self._source.find_affection_profile(f"AFFECTION#REQUESTER#{requester_key}")
            if source is None:
                raise ValueError("source affection profile does not exist")
        item = project_affection_profile(source, identity_hmac_key=config.identity_hmac_key)
        return ProjectionResult(created=self._statistics.put_profile(item))


def _legacy_requester_id(partition_key: str) -> str | None:
    prefix = "AFFECTION#REQUESTER#"
    suffix = partition_key.removeprefix(prefix)
    if (
        partition_key.startswith(prefix)
        and 17 <= len(suffix) <= 20
        and suffix.isascii()
        and suffix.isdigit()
    ):
        return suffix
    return None


def project_affection_profile(
    source: DynamoItem,
    *,
    identity_hmac_key: bytes,
) -> DynamoItem:
    """Validate a source profile and remove its private identity before projection."""

    if len(identity_hmac_key) < 32:
        raise ValueError("identity HMAC key must contain at least 32 bytes")
    prefix = "AFFECTION#REQUESTER#"
    source_schema_version = source.get("schema_version")
    pk = source.get("PK")
    scores = source.get("scores")
    version = source.get("version")
    display_name = source.get("requester_display_name")
    updated_at_text = source.get("updated_at")
    if (
        source_schema_version
        not in {LEGACY_AFFECTION_SCHEMA_VERSION, OPAQUE_AFFECTION_SCHEMA_VERSION}
        or source.get("SK") != "PROFILE"
        or source.get("record_type") != "affection_profile"
        or not isinstance(pk, str)
        or not pk.startswith(prefix)
        or not isinstance(display_name, str)
        or not display_name.strip()
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        or not isinstance(scores, list)
        or len(scores) != len(PARTICIPANT_SLOTS)
        or not isinstance(updated_at_text, str)
    ):
        raise ValueError("source affection profile is invalid")
    common_fields = {
        "PK",
        "SK",
        "record_type",
        "schema_version",
        "requester_username",
        "requester_display_name",
        "scores",
        "version",
        "updated_at",
    }
    expected_fields = (
        {*common_fields, "requester_id"}
        if source_schema_version == LEGACY_AFFECTION_SCHEMA_VERSION
        else {
            *common_fields,
            "requester_key",
            "reset_count",
            "memorial_cycle",
            *(
                {
                    "unlocked_participant",
                    "unlocked_at",
                    "unlock_debate_id",
                    "unlock_display_name",
                    "unlock_retroactive",
                }
                if "unlocked_participant" in source
                else set()
            ),
        }
    )
    if set(source) != expected_fields:
        raise ValueError("source affection profile fields are invalid")
    clean_scores: dict[str, DynamoValue] = {}
    for slot, score in zip(PARTICIPANT_SLOTS, scores, strict=True):
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 1000:
            raise ValueError("source affection score is invalid")
        clean_scores[slot] = score
    try:
        updated_at = TypeAdapter(AwareDatetime).validate_python(updated_at_text).astimezone(UTC)
    except OverflowError, ValidationError:
        raise ValueError("source affection timestamp is invalid") from None
    source_timestamp = updated_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if source_timestamp != updated_at_text:
        raise ValueError("source affection timestamp is not canonical UTC")
    reset_count = 0
    memorial_cycle = 1
    unlock_projection: DynamoItem = {}
    if source_schema_version == LEGACY_AFFECTION_SCHEMA_VERSION:
        requester_id = source.get("requester_id")
        if not isinstance(requester_id, str) or not requester_id or pk != f"{prefix}{requester_id}":
            raise ValueError("source affection profile identity is invalid")
        requester_key = derive_requester_key(identity_hmac_key, requester_id)
    else:
        requester_key = source.get("requester_key")
        reset_count = source.get("reset_count")
        memorial_cycle = source.get("memorial_cycle")
        if (
            not _is_opaque_key(requester_key)
            or pk != f"{prefix}{requester_key}"
            or isinstance(reset_count, bool)
            or not isinstance(reset_count, int)
            or reset_count < 0
            or isinstance(memorial_cycle, bool)
            or not isinstance(memorial_cycle, int)
            or memorial_cycle < 1
            or memorial_cycle != reset_count + 1
        ):
            raise ValueError("source affection profile identity is invalid")
        unlock_projection = _project_memorial_unlock(
            source,
            identity_hmac_key=identity_hmac_key,
            memorial_cycle=memorial_cycle,
        )
    return cast(
        DynamoItem,
        {
            "PK": "AFFECTION#PROFILE",
            "SK": requester_key,
            "schema_version": 2,
            "record_type": "affection_profile",
            "source_version": version,
            "display_name": display_name,
            "scores": clean_scores,
            "updated_at": updated_at.isoformat(),
            "reset_count": reset_count,
            "memorial_cycle": memorial_cycle,
            **unlock_projection,
        },
    )


def _project_memorial_unlock(
    source: DynamoItem,
    *,
    identity_hmac_key: bytes,
    memorial_cycle: int,
) -> DynamoItem:
    fields = {
        "unlocked_participant",
        "unlocked_at",
        "unlock_debate_id",
        "unlock_display_name",
        "unlock_retroactive",
    }
    present = fields.intersection(source)
    if not present:
        return {}
    if present != fields:
        raise ValueError("source memorial unlock is incomplete")
    participant = source.get("unlocked_participant")
    unlocked_at_text = source.get("unlocked_at")
    debate_id = source.get("unlock_debate_id")
    display_name = source.get("unlock_display_name")
    retroactive = source.get("unlock_retroactive")
    if (
        participant not in PARTICIPANT_SLOTS
        or not isinstance(unlocked_at_text, str)
        or not isinstance(debate_id, str)
        or not debate_id
        or not isinstance(display_name, str)
        or not display_name.strip()
        or not isinstance(retroactive, bool)
    ):
        raise ValueError("source memorial unlock is invalid")
    try:
        unlocked_at = TypeAdapter(AwareDatetime).validate_python(unlocked_at_text).astimezone(UTC)
    except OverflowError, ValidationError:
        raise ValueError("source memorial unlock timestamp is invalid") from None
    canonical = unlocked_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if canonical != unlocked_at_text:
        raise ValueError("source memorial unlock timestamp is not canonical UTC")
    return cast(
        DynamoItem,
        {
            "unlocked_participant": participant,
            "unlocked_at": unlocked_at.isoformat(),
            "unlock_record_id": derive_record_key(identity_hmac_key, debate_id),
            "unlock_display_name": display_name,
            "unlock_memorial_cycle": memorial_cycle,
            "unlock_retroactive": retroactive,
        },
    )


def _is_opaque_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == OPAQUE_KEY_LENGTH
        and value.isascii()
        and all(character.isalnum() or character in "_-" for character in value)
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
