"""Isolated public preview Lambda: no authentication/session or secret readers."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

from shittim_records.ogp_adapters import AwsPreviewStore
from shittim_records.ogp_http import PreviewService
from shittim_records.ogp_render import PreviewRenderer


@lru_cache(maxsize=1)
def service() -> PreviewService:
    config = Config(connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 1})
    dynamodb = boto3.resource("dynamodb", config=config)
    return PreviewService(
        AwsPreviewStore(
            dynamodb.Table(os.environ["ARCHIVE_TABLE_NAME"]),
            dynamodb.Table(os.environ["SESSION_TABLE_NAME"]),
            boto3.client("s3", config=config),
            media_bucket=os.environ["MEDIA_BUCKET_NAME"],
            web_bucket=os.environ["WEB_BUCKET_NAME"],
        ),
        PreviewRenderer(),
        "https://" + os.environ["RECORDS_PUBLIC_HOSTNAME"],
    )


def handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    return service().handle(event)
