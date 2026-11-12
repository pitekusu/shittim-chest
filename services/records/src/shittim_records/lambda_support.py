"""Shared AWS client settings and HTTP error handling for Records Lambdas."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.config import Config

SDK_CONFIG = Config(
    retries={"total_max_attempts": 2, "mode": "standard"},
    connect_timeout=2,
    read_timeout=5,
)
S3_SDK_CONFIG = SDK_CONFIG.merge(
    Config(signature_version="s3v4", s3={"addressing_style": "virtual"})
)


def environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def regional_s3_client() -> Any:
    region = environment("AWS_REGION")
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=S3_SDK_CONFIG,
    )


def content_free_http_failure(
    event: Mapping[str, Any],
    *,
    code: str,
    log_event: str,
    error: Exception,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Return a stable boundary error without serializing exception inputs or causes."""

    from shittim_records.http_api import error_response

    fields = {"event": log_event, "error_type": type(error).__name__}
    logger.info(json.dumps(fields, sort_keys=True, separators=(",", ":")))
    context = event.get("requestContext")
    request_id = context.get("requestId") if isinstance(context, Mapping) else None
    if not isinstance(request_id, str) or not request_id:
        request_id = "unavailable"
    return error_response(503, code, request_id)
