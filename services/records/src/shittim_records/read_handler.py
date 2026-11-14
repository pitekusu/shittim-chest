"""Lambda entry point for authenticated Records reads."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3

from shittim_records.auth_adapters import DynamoAuthStore, load_oauth_guild
from shittim_records.http_api import ReadHttpController
from shittim_records.lambda_support import (
    SDK_CONFIG,
    content_free_http_failure,
    environment,
    regional_s3_client,
)
from shittim_records.mobile_auth_adapters import DynamoMobileAuthStore
from shittim_records.read_adapters import DynamoRecordsReader, ReadConfigurationRepository
from shittim_records.read_api import CursorCodec, RecordsReadService

if TYPE_CHECKING:
    from shittim_records.gallery_read import GalleryReadService
    from shittim_records.momotalk_read import MomotalkReadService

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

_READ_CONTROLLER: ReadHttpController | None = None


def read_handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    """Handle authenticated Archive list and detail routes."""

    try:
        return _read_controller().handle(event, now=datetime.now(UTC))
    except Exception as error:
        return content_free_http_failure(
            event,
            code="RECORDS_UNAVAILABLE",
            log_event="records_read_request_failed",
            error=error,
            logger=LOGGER,
        )


def _read_controller() -> ReadHttpController:
    global _READ_CONTROLLER
    if _READ_CONTROLLER is not None:
        return _READ_CONTROLLER

    dynamodb = boto3.client("dynamodb", config=SDK_CONFIG)
    ssm = boto3.client("ssm", config=SDK_CONFIG)
    session_key = ReadConfigurationRepository(
        ssm,
        environment("SESSION_KEY_PARAMETER_NAME"),
    ).load_session_key()
    s3 = regional_s3_client()
    session_table = environment("SESSION_TABLE_NAME")
    statistics_table = environment("STATISTICS_TABLE_NAME")
    media_bucket = environment("MEDIA_BUCKET_NAME")
    public_origin = "https://" + environment("RECORDS_PUBLIC_HOSTNAME")
    reader = DynamoRecordsReader(
        dynamodb,
        s3,
        archive_table_name=environment("ARCHIVE_TABLE_NAME"),
        statistics_table_name=statistics_table,
        session_table_name=session_table,
        media_bucket_name=media_bucket,
    )

    @cache
    def gallery() -> GalleryReadService:
        from shittim_records.gallery_read import GalleryReadService
        from shittim_records.gallery_read_adapters import DynamoGalleryStore

        return GalleryReadService(
            DynamoGalleryStore(dynamodb, s3, table=statistics_table, bucket=media_bucket),
            reader,
            guild_id=load_oauth_guild(ssm, environment("OAUTH_CONFIG_PARAMETER_NAME")),
            session_key=session_key,
        )

    @cache
    def momotalk() -> MomotalkReadService:
        from shittim_records.momotalk_adapters import DynamoMomotalkStore, MomotalkAssets
        from shittim_records.momotalk_read import MomotalkReadService

        return MomotalkReadService(
            DynamoMomotalkStore(dynamodb, statistics_table),
            MomotalkAssets(s3, media_bucket),
            reader,
        )

    _READ_CONTROLLER = ReadHttpController(
        store=DynamoAuthStore(dynamodb, session_table),
        mobile_store=DynamoMobileAuthStore(dynamodb, session_table),
        session_key=session_key,
        allowed_origin=public_origin,
        records=RecordsReadService(
            reader=reader,
            cursor_codec=CursorCodec(session_key),
            public_origin=public_origin,
        ),
        gallery=gallery,
        momotalk=momotalk,
    )
    return _READ_CONTROLLER
