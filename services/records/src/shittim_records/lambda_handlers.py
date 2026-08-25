"""AWS Lambda entry points for Records projection, rankings, auth, and reads."""

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
from shittim_records.admin import AdminAuthorizer
from shittim_records.admin_adapters import AdminSecurityConfigurationRepository
from shittim_records.admin_http import AdminStatusHttpController
from shittim_records.admin_status import AdminStatusService
from shittim_records.admin_status_adapters import (
    AwsAdminStatusConfiguration,
    AwsAdminStatusSource,
)
from shittim_records.auth import AuthService
from shittim_records.auth_adapters import (
    AuthConfigurationRepository,
    DiscordOAuthClient,
    DynamoAuthStore,
    S3AvatarStore,
)
from shittim_records.cost_adapters import (
    AwsCostExplorerSource,
    CostConfigurationRepository,
    DynamoCostLedgerStore,
    FrankfurterRateSource,
    OpenAICostSource,
)
from shittim_records.costs import CostCollectionFailed, CostCollectionService
from shittim_records.http_api import AuthHttpController, ReadHttpController, error_response
from shittim_records.projector import BackfillService, ProjectorService
from shittim_records.ranking_adapters import DynamoRankingSnapshotStore, DynamoRankingSource
from shittim_records.rankings import RankingService
from shittim_records.read_adapters import DynamoRecordsReader, ReadConfigurationRepository
from shittim_records.read_api import CursorCodec, RecordsReadService

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

_PROJECTOR: ProjectorService | None = None
_BACKFILL: BackfillService | None = None
_RANKING: RankingService | None = None
_COSTS: CostCollectionService | None = None
_AUTH_CONTROLLER: AuthHttpController | None = None
_READ_CONTROLLER: ReadHttpController | None = None
_ADMIN_STATUS_CONTROLLER: AdminStatusHttpController | None = None

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


def ranking_handler(_event: Mapping[str, Any], _context: object) -> dict[str, object]:
    """Recompute both ranking snapshots without logging identity data."""

    result = _ranking_service().refresh(now=datetime.now(UTC))
    response = {
        "archive_count": result.archive_count,
        "win_entries": len(result.wins),
        "request_entries": len(result.requests),
    }
    _log(event="records_rankings_refreshed", **response)
    return response


def cost_handler(event: Mapping[str, Any], _context: object) -> dict[str, object]:
    """Collect one bounded provider mode without logging amounts or identifiers."""

    mode = event.get("mode")
    if mode not in {"aws_fx", "openai"}:
        raise ValueError("cost collection mode must be aws_fx or openai")
    try:
        summaries = _cost_service().refresh(mode=mode, now=datetime.now(UTC))
    except CostCollectionFailed as error:
        _log(
            event="records_cost_collection_failed",
            mode=mode,
            succeeded_sources=len(error.summaries),
            failed_sources=len(error.failures),
            failure_codes=sorted(failure.code for failure in error.failures),
        )
        raise
    response = {
        "mode": mode,
        "sources": len(summaries),
        "windows": sum(summary.windows for summary in summaries),
        "days": sum(summary.days for summary in summaries),
        "complete": all(summary.initial_complete for summary in summaries),
    }
    _log(event="records_cost_collection_completed", **response)
    return response


def auth_handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    """Handle public OAuth and session routes."""

    try:
        return _auth_controller().handle(event, now=datetime.now(UTC))
    except Exception as error:
        return _content_free_http_failure(
            event,
            code="RECORDS_UNAVAILABLE",
            log_event="records_auth_request_failed",
            error=error,
        )


def read_handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    """Handle authenticated Archive list and detail routes."""

    return _read_controller().handle(event, now=datetime.now(UTC))


def admin_status_handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    """Handle authenticated, sanitized read-only AWS status requests."""

    try:
        return _admin_status_controller().handle(event, now=datetime.now(UTC))
    except Exception as error:
        return _content_free_http_failure(
            event,
            code="ADMIN_STATUS_UNAVAILABLE",
            log_event="records_admin_status_request_failed",
            error=error,
        )


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


def _ranking_service() -> RankingService:
    global _RANKING
    if _RANKING is None:
        dynamodb = boto3.client("dynamodb", config=SDK_CONFIG)
        _RANKING = RankingService(
            source=DynamoRankingSource(
                dynamodb,
                _environment("ARCHIVE_TABLE_NAME"),
            ),
            store=DynamoRankingSnapshotStore(
                dynamodb,
                _environment("STATISTICS_TABLE_NAME"),
            ),
        )
    return _RANKING


def _cost_service() -> CostCollectionService:
    global _COSTS
    if _COSTS is None:
        dynamodb = boto3.client("dynamodb", config=SDK_CONFIG)
        http = httpx.Client(timeout=httpx.Timeout(5.0, connect=2.0), follow_redirects=False)
        _COSTS = CostCollectionService(
            aws=AwsCostExplorerSource(
                boto3.client("ce", region_name="us-east-1", config=SDK_CONFIG)
            ),
            openai=OpenAICostSource(
                http,
                CostConfigurationRepository(
                    boto3.client("ssm", config=SDK_CONFIG),
                    admin_key_parameter_name=_environment("OPENAI_ADMIN_KEY_PARAMETER_NAME"),
                    project_id_parameter_name=_environment("OPENAI_PROJECT_ID_PARAMETER_NAME"),
                ),
            ),
            exchange=FrankfurterRateSource(http),
            store=DynamoCostLedgerStore(
                dynamodb,
                _environment("STATISTICS_TABLE_NAME"),
            ),
        )
    return _COSTS


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
            admin_user_id_parameter_name=_environment("ADMIN_DISCORD_USER_ID_PARAMETER_NAME"),
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
            statistics_table_name=_environment("STATISTICS_TABLE_NAME"),
            session_table_name=_environment("SESSION_TABLE_NAME"),
            media_bucket_name=_environment("MEDIA_BUCKET_NAME"),
        )
        _READ_CONTROLLER = ReadHttpController(
            store=DynamoAuthStore(dynamodb, _environment("SESSION_TABLE_NAME")),
            session_key=session_key,
            records=RecordsReadService(reader=reader, cursor_codec=CursorCodec(session_key)),
        )
    return _READ_CONTROLLER


def _admin_status_controller() -> AdminStatusHttpController:
    global _ADMIN_STATUS_CONTROLLER
    if _ADMIN_STATUS_CONTROLLER is None:
        region = _environment("AWS_REGION")
        dynamodb = boto3.client("dynamodb", config=SDK_CONFIG)
        ssm = boto3.client("ssm", config=SDK_CONFIG)
        configuration = AwsAdminStatusConfiguration(
            aws_account_id=_environment("ADMIN_AWS_ACCOUNT_ID"),
            cluster_name=_environment("ECS_CLUSTER_NAME"),
            service_name=_environment("ECS_SERVICE_NAME"),
            ecr_repository_name=_environment("ECR_REPOSITORY_NAME"),
            runtime_image_digest=_environment("RUNTIME_IMAGE_DIGEST"),
            break_glass_image_digest=_environment("BREAK_GLASS_IMAGE_DIGEST"),
            buckets={
                "web": _environment("WEB_BUCKET_NAME"),
                "media": _environment("MEDIA_BUCKET_NAME"),
                "release": _environment("RELEASE_BUNDLE_BUCKET_NAME"),
            },
            tables={
                "debate": _environment("SOURCE_TABLE_NAME"),
                "archive": _environment("ARCHIVE_TABLE_NAME"),
                "statistics": _environment("STATISTICS_TABLE_NAME"),
                "session": _environment("SESSION_TABLE_NAME"),
            },
            functions=_environment_mapping("ADMIN_STATUS_FUNCTIONS_JSON"),
            distribution_id=_environment("RECORDS_DISTRIBUTION_ID"),
            projector_dlq_url=_environment("PROJECTOR_DLQ_URL"),
            alarm_prefix=_environment("ADMIN_ALARM_PREFIX"),
        )
        source = AwsAdminStatusSource(
            configuration=configuration,
            ecs=boto3.client("ecs", region_name=region, config=SDK_CONFIG),
            ecr=boto3.client("ecr", region_name=region, config=SDK_CONFIG),
            inspector=boto3.client("inspector2", region_name=region, config=SDK_CONFIG),
            s3=_regional_s3_client(),
            dynamodb=dynamodb,
            lambda_client=boto3.client("lambda", region_name=region, config=SDK_CONFIG),
            cloudfront=boto3.client("cloudfront", region_name="us-east-1", config=SDK_CONFIG),
            acm=boto3.client("acm", region_name="us-east-1", config=SDK_CONFIG),
            sqs=boto3.client("sqs", region_name=region, config=SDK_CONFIG),
            cloudwatch=boto3.client("cloudwatch", region_name=region, config=SDK_CONFIG),
            cloudwatch_global=boto3.client(
                "cloudwatch",
                region_name="us-east-1",
                config=SDK_CONFIG,
            ),
        )
        _ADMIN_STATUS_CONTROLLER = AdminStatusHttpController(
            authorizer=AdminAuthorizer(
                store=DynamoAuthStore(dynamodb, _environment("SESSION_TABLE_NAME")),
                configuration=_admin_security_configuration(ssm),
            ),
            status=AdminStatusService(source),
        )
    return _ADMIN_STATUS_CONTROLLER


def _admin_security_configuration(ssm: Any) -> Any:
    return AdminSecurityConfigurationRepository(
        ssm,
        identity_parameter_name=_environment("IDENTITY_HMAC_PARAMETER_NAME"),
        session_key_parameter_name=_environment("SESSION_KEY_PARAMETER_NAME"),
        oauth_parameter_name=_environment("OAUTH_CONFIG_PARAMETER_NAME"),
        admin_user_id_parameter_name=_environment("ADMIN_DISCORD_USER_ID_PARAMETER_NAME"),
    ).load()


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


def _environment_mapping(name: str) -> dict[str, str]:
    try:
        value = json.loads(_environment(name))
    except TypeError, ValueError, json.JSONDecodeError:
        raise RuntimeError(f"required environment variable is invalid: {name}") from None
    if (
        not isinstance(value, dict)
        or not value
        or any(
            not isinstance(key, str) or not key or not isinstance(item, str) or not item
            for key, item in value.items()
        )
    ):
        raise RuntimeError(f"required environment variable is invalid: {name}")
    return value


def _content_free_http_failure(
    event: Mapping[str, Any],
    *,
    code: str,
    log_event: str,
    error: Exception,
) -> dict[str, Any]:
    """Return a stable boundary error without serializing exception inputs or causes."""

    _log(event=log_event, error_type=type(error).__name__)
    context = event.get("requestContext")
    request_id = context.get("requestId") if isinstance(context, Mapping) else None
    if not isinstance(request_id, str) or not request_id:
        request_id = "unavailable"
    return error_response(503, code, request_id)


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
