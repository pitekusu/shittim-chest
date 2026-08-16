"""AWS Lambda entry points for projection and bounded backfill."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import boto3

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_ssm.client import SSMClient

from shittim_records.adapters import (
    ArchiveRepository,
    ConfigurationRepository,
    SourceDebateRepository,
    StatisticsRepository,
)
from shittim_records.projector import BackfillService, ProjectorService

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

_PROJECTOR: ProjectorService | None = None
_BACKFILL: BackfillService | None = None


def projector_handler(event: Mapping[str, Any], _context: object) -> dict[str, object]:
    """Handle one DynamoDB Streams batch using partial batch failures."""

    service = _projector_service()
    failures: list[dict[str, str]] = []
    created = 0
    skipped = 0
    for record in event.get("Records", []):
        event_id = _required_text(record, "eventID")
        try:
            partition_key = _completed_meta_partition(record)
            result = service.project_partition(partition_key, now=datetime.now(UTC))
            if result.created:
                created += 1
            else:
                skipped += 1
        except Exception as error:
            failures.append({"itemIdentifier": event_id})
            _log(
                event="records_projection_failed",
                error_type=type(error).__name__,
            )
    _log(
        event="records_projection_batch",
        received=len(event.get("Records", [])),
        created=created,
        skipped=skipped,
        failed=len(failures),
    )
    return {"batchItemFailures": failures}


def backfill_handler(event: Mapping[str, Any], _context: object) -> dict[str, object]:
    """Run one content-free dry-run or apply backfill page."""

    mode = event.get("mode", "dry-run")
    if mode not in {"dry-run", "apply"}:
        raise ValueError("backfill mode must be dry-run or apply")
    page_limit = event.get("page_limit", 100)
    if isinstance(page_limit, bool) or not isinstance(page_limit, int):
        raise ValueError("backfill page limit must be an integer")
    result = _backfill_service().run_page(
        apply=mode == "apply",
        now=datetime.now(UTC),
        page_limit=page_limit,
    )
    response = {
        "mode": mode,
        "candidates": result.candidates,
        "validated": result.validated,
        "projected": result.projected,
        "skipped": result.skipped,
        "complete": result.complete,
    }
    _log(event="records_backfill_page", **response)
    return response


def _projector_service() -> ProjectorService:
    global _PROJECTOR
    if _PROJECTOR is None:
        dynamodb = boto3.client("dynamodb")
        _PROJECTOR = _build_projector(dynamodb, boto3.client("ssm"))
    return _PROJECTOR


def _backfill_service() -> BackfillService:
    global _BACKFILL
    if _BACKFILL is None:
        dynamodb = boto3.client("dynamodb")
        source = SourceDebateRepository(dynamodb, _environment("SOURCE_TABLE_NAME"))
        _BACKFILL = BackfillService(
            source=source,
            projector=_build_projector(dynamodb, boto3.client("ssm"), source=source),
            statistics=StatisticsRepository(
                dynamodb,
                _environment("STATISTICS_TABLE_NAME"),
            ),
        )
    return _BACKFILL


def _build_projector(
    dynamodb: DynamoDBClient,
    ssm: SSMClient,
    *,
    source: SourceDebateRepository | None = None,
) -> ProjectorService:
    return ProjectorService(
        source=source or SourceDebateRepository(dynamodb, _environment("SOURCE_TABLE_NAME")),
        archive=ArchiveRepository(dynamodb, _environment("ARCHIVE_TABLE_NAME")),
        configuration=ConfigurationRepository(
            ssm,
            identity_hmac_parameter_name=_environment("IDENTITY_HMAC_PARAMETER_NAME"),
            presentation_parameter_name=_environment("PRESENTATION_PARAMETER_NAME"),
        ),
    )


def _completed_meta_partition(record: Mapping[str, Any]) -> str:
    if record.get("eventName") != "MODIFY":
        raise ValueError("projector accepts only MODIFY stream records")
    dynamodb = record.get("dynamodb")
    if not isinstance(dynamodb, Mapping):
        raise ValueError("stream record has no DynamoDB payload")
    image = dynamodb.get("NewImage")
    if not isinstance(image, Mapping):
        raise ValueError("stream record has no new image")
    expected = {
        "record_type": "debate_meta",
        "current_phase": "completed",
    }
    for field, expected_value in expected.items():
        value = image.get(field)
        if not isinstance(value, Mapping) or value.get("S") != expected_value:
            raise ValueError("stream record is not a completed debate metadata update")
    key = image.get("PK")
    if not isinstance(key, Mapping) or not isinstance(key.get("S"), str):
        raise ValueError("stream record has no source partition key")
    partition_key = key["S"]
    if not partition_key.startswith("DEBATE#"):
        raise ValueError("stream record partition is not a debate")
    return partition_key


def _required_text(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"stream record has no {field}")
    return value


def _environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _log(**fields: object) -> None:
    LOGGER.info(json.dumps(fields, sort_keys=True, separators=(",", ":")))
