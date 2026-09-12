"""Composition roots for the weekly scheduler and private SQS consumer."""

import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config
from pydantic import Field

from shittim_records.memorial_adapters import (
    MEMORIAL_PARTICIPANT_REFERENCE_ASSET_KEYS,
    MemorialConfigurationRepository,
    S3MemorialAssetStore,
)
from shittim_records.momotalk import OpaqueId, StoredModel, validate_week_id, week_for_schedule
from shittim_records.momotalk_adapters import (
    DynamoMomotalkStore,
    MomotalkAssets,
    MomotalkInputSource,
    MomotalkQueue,
)
from shittim_records.momotalk_generation import MomotalkGenerationService, collect_week
from shittim_records.momotalk_openai import OpenAIMomotalkGenerator
from shittim_records.read_adapters import DynamoRecordsReader

LOGGER = logging.getLogger(__name__)
SDK_CONFIG = Config(
    connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 2, "mode": "standard"}
)
S3_CONFIG = SDK_CONFIG.merge(Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))


class Job(StoredModel):
    week_id: str = Field(alias="weekId")
    room_id: OpaqueId = Field(alias="roomId")


@lru_cache(maxsize=1)
def _components() -> tuple[
    DynamoMomotalkStore,
    MomotalkAssets,
    MomotalkQueue,
    DynamoRecordsReader,
    MemorialConfigurationRepository,
    S3MemorialAssetStore,
]:
    for name in ("httpx", "httpx2", "httpcore", "openai", "boto3", "botocore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    dynamodb = boto3.client("dynamodb", config=SDK_CONFIG)
    s3 = boto3.client("s3", config=S3_CONFIG)
    media = os.environ["MEDIA_BUCKET_NAME"]
    statistics = os.environ["STATISTICS_TABLE_NAME"]
    configuration = MemorialConfigurationRepository(
        boto3.client("ssm", config=SDK_CONFIG),
        api_key_parameter_name=os.environ["MOMOTALK_OPENAI_API_KEY_PARAMETER_NAME"],
        runtime_prompt_parameter_root="/shittim-chest/production/runtime-prompts",
        legacy_persona_parameter_names={
            "participant-a": os.environ["LEGACY_PERSONA_PARTICIPANT_A_PARAMETER_NAME"],
            "participant-b": os.environ["LEGACY_PERSONA_PARTICIPANT_B_PARAMETER_NAME"],
            "participant-c": os.environ["LEGACY_PERSONA_PARTICIPANT_C_PARAMETER_NAME"],
        },
    )
    return (
        DynamoMomotalkStore(dynamodb, statistics),
        MomotalkAssets(s3, media),
        MomotalkQueue(boto3.client("sqs", config=SDK_CONFIG), os.environ["MOMOTALK_QUEUE_URL"]),
        DynamoRecordsReader(
            dynamodb,
            s3,
            archive_table_name=os.environ["ARCHIVE_TABLE_NAME"],
            statistics_table_name=statistics,
            session_table_name=os.environ["SESSION_TABLE_NAME"],
            media_bucket_name=media,
        ),
        configuration,
        S3MemorialAssetStore(
            s3,
            upload_bucket_name=media,
            media_bucket_name=media,
            participant_asset_keys=MEMORIAL_PARTICIPANT_REFERENCE_ASSET_KEYS,
        ),
    )


def collect_handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    try:
        if event.get("source") != "aws.events" or event.get("detail-type") != "Scheduled Event":
            raise ValueError("invalid scheduled event")
        scheduled_at = datetime.fromisoformat(event["time"])
        now = datetime.now(UTC)
        if scheduled_at.tzinfo is None or not timedelta(0) <= now - scheduled_at <= timedelta(
            days=1
        ):
            raise ValueError("invalid scheduled time")
        week = week_for_schedule(scheduled_at)
        store, assets, queue, reader, configuration, _references = _components()
        source = MomotalkInputSource(
            store.client, os.environ["ARCHIVE_TABLE_NAME"], store.table_name, reader, configuration
        )
        count = collect_week(week, source, store, assets, queue)
        return {"state": "queued", "rooms": count}
    except Exception as error:
        LOGGER.error("momotalk_collection_failed category=%s", type(error).__name__)
        # Lambda's automatic traceback must never print a Pydantic/private-input error.
        raise RuntimeError("MOMOTALK_COLLECTION_FAILED") from None


def worker_handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    failures = []
    for record in event.get("Records", []):
        try:
            job = Job.model_validate_json(record["body"])
            week_id = validate_week_id(job.week_id)
            store, assets, queue, _reader, configuration, references = _components()
            generator = OpenAIMomotalkGenerator(configuration, references)
            try:
                succeeded = MomotalkGenerationService(store, assets, queue, generator).run(
                    week_id,
                    job.room_id,
                    now=datetime.now(UTC),
                )
            finally:
                generator.close()
            if not succeeded:
                failures.append({"itemIdentifier": record["messageId"]})
        except Exception as error:
            LOGGER.error("momotalk_generation_failed category=%s", type(error).__name__)
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
