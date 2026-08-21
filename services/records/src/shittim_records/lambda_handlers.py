"""AWS Lambda entry points for Records projection, backfill, auth, and reads."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_ssm.client import SSMClient

from shittim_records.adapters import (
    ArchiveRepository,
    ConfigurationRepository,
    SourceDebateRepository,
    StatisticsRepository,
)
from shittim_records.auth import AuthService
from shittim_records.auth_adapters import (
    AuthConfigurationRepository,
    DiscordOAuthClient,
    DynamoAuthStore,
    S3AvatarStore,
)
from shittim_records.http_api import AuthHttpController, ReadHttpController
from shittim_records.projector import BackfillService, ProjectorService
from shittim_records.read_adapters import DynamoRecordsReader, ReadConfigurationRepository
from shittim_records.read_api import CursorCodec, RecordsReadService

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

_PROJECTOR: ProjectorService | None = None
_BACKFILL: BackfillService | None = None
_AUTH_CONTROLLER: AuthHttpController | None = None
_READ_CONTROLLER: ReadHttpController | None = None

SDK_CONFIG = Config(
    retries={"total_max_attempts": 2, "mode": "standard"},
    connect_timeout=2,
    read_timeout=5,
)
S3_SDK_CONFIG = SDK_CONFIG.merge(Config(s3={"addressing_style": "virtual"}))


def projector_handler(event: Mapping[str, Any], _context: object) -> dict[str, object]:
    """Handle one DynamoDB Streams batch using partial batch failures."""

    service = _projector_service()
    failures: list[dict[str, str]] = []
    created = 0
    skipped = 0
    for record in event.get("Records", []):
        sequence_number = _stream_sequence_number(record)
        try:
            partition_key = _completed_meta_partition(record)
            result = service.project_partition(partition_key, now=datetime.now(UTC))
            if result.created:
                created += 1
            else:
                skipped += 1
        except Exception as error:
            failures.append({"itemIdentifier": sequence_number})
            _log(
                event="records_projection_failed",
                **_projection_failure_fields(error),
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


def auth_handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    """Handle public OAuth and session routes."""

    return _auth_controller().handle(event, now=datetime.now(UTC))


def read_handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    """Handle authenticated Archive list and detail routes."""

    return _read_controller().handle(event, now=datetime.now(UTC))


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


def _auth_controller() -> AuthHttpController:
    global _AUTH_CONTROLLER
    if _AUTH_CONTROLLER is None:
        dynamodb = boto3.client("dynamodb", config=SDK_CONFIG)
        configuration = AuthConfigurationRepository(
            boto3.client("ssm", config=SDK_CONFIG),
            identity_parameter_name=_environment("IDENTITY_HMAC_PARAMETER_NAME"),
            oauth_parameter_name=_environment("OAUTH_CONFIG_PARAMETER_NAME"),
            client_secret_parameter_name=_environment("OAUTH_CLIENT_SECRET_PARAMETER_NAME"),
            session_key_parameter_name=_environment("SESSION_KEY_PARAMETER_NAME"),
        ).load()
        service = AuthService(
            store=DynamoAuthStore(dynamodb, _environment("SESSION_TABLE_NAME")),
            discord=DiscordOAuthClient(
                httpx.Client(timeout=httpx.Timeout(3.0, connect=2.0), follow_redirects=False)
            ),
            avatars=S3AvatarStore(
                _regional_s3_client(),
                _environment("MEDIA_BUCKET_NAME"),
            ),
            configuration=configuration,
        )
        _AUTH_CONTROLLER = AuthHttpController(service)
    return _AUTH_CONTROLLER


def _read_controller() -> ReadHttpController:
    global _READ_CONTROLLER
    if _READ_CONTROLLER is None:
        dynamodb = boto3.client("dynamodb", config=SDK_CONFIG)
        session_key = ReadConfigurationRepository(
            boto3.client("ssm", config=SDK_CONFIG),
            _environment("SESSION_KEY_PARAMETER_NAME"),
        ).load_session_key()
        reader = DynamoRecordsReader(
            dynamodb,
            _regional_s3_client(),
            archive_table_name=_environment("ARCHIVE_TABLE_NAME"),
            session_table_name=_environment("SESSION_TABLE_NAME"),
            media_bucket_name=_environment("MEDIA_BUCKET_NAME"),
        )
        _READ_CONTROLLER = ReadHttpController(
            store=DynamoAuthStore(dynamodb, _environment("SESSION_TABLE_NAME")),
            session_key=session_key,
            records=RecordsReadService(reader=reader, cursor_codec=CursorCodec(session_key)),
        )
    return _READ_CONTROLLER


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


def _stream_sequence_number(record: Mapping[str, Any]) -> str:
    dynamodb = record.get("dynamodb")
    if not isinstance(dynamodb, Mapping):
        raise ValueError("stream record has no DynamoDB payload")
    value = dynamodb.get("SequenceNumber")
    if not isinstance(value, str) or not value:
        raise ValueError("stream record has no sequence number")
    return value


def _projection_failure_fields(error: Exception) -> dict[str, object]:
    fields: dict[str, object] = {"error_type": type(error).__name__}
    if not isinstance(error, ClientError):
        return fields
    raw_code = error.response.get("Error", {}).get("Code")
    fields["client_error_code"] = (
        raw_code
        if isinstance(raw_code, str) and raw_code.isascii() and raw_code.isalnum()
        else "unknown"
    )
    raw_reasons = error.response.get("CancellationReasons")
    if isinstance(raw_reasons, list):
        reason_codes = [
            code
            for reason in raw_reasons
            if isinstance(reason, Mapping)
            and isinstance((code := reason.get("Code")), str)
            and code.isascii()
            and code.isalnum()
        ]
        if reason_codes:
            fields["cancellation_reason_codes"] = reason_codes
    return fields


def _environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _regional_s3_client() -> Any:
    region = _environment("AWS_REGION")
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=S3_SDK_CONFIG,
    )


def _log(**fields: object) -> None:
    LOGGER.info(json.dumps(fields, sort_keys=True, separators=(",", ":")))
