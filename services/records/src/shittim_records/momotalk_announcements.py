"""Announce a readable weekly MomoTalk once, using the manual announcement gateway."""

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal

import boto3
import httpx
from botocore.config import Config
from shittim_chest.adapters.discord.announcements import (
    AnnouncementError,
    announcement_fingerprint,
    announcement_nonce,
    create_discord_client,
    find_existing_announcement,
    post_announcement,
    resolve_target_channel,
    verify_posted_announcement,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.application.discord import DiscordRuntimeConfig
from shittim_chest.config.models import parse_discord_runtime_config
from shittim_chest.config.status_publisher import MODERATOR_TOKEN_PARAMETER

from shittim_records.contracts import MomotalkWeek
from shittim_records.momotalk import TOKYO, validate_week_id, week_for_schedule
from shittim_records.momotalk_adapters import WEEKS_PK

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_ssm.client import SSMClient

LOGGER = logging.getLogger(__name__)
RECEIPTS_PK = "MOMOTALK#ANNOUNCEMENTS"
SDK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=10,
    retries={"total_max_attempts": 1, "mode": "standard"},
)


@dataclass(frozen=True, slots=True)
class Receipt:
    fingerprint: str
    state: Literal["attempted", "sent"]


class DynamoMomotalkAnnouncements:
    """Read publication metadata and persist content-free exclusive send attempts."""

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self.client = client
        self.table_name = table_name

    def readable(self, week_id: date, now: datetime) -> bool:
        expected = week_for_schedule(datetime.combine(week_id, time(18), TOKYO))
        if now < expected.publish_at:
            return False
        result = self.client.get_item(
            TableName=self.table_name,
            Key=marshal_item({"PK": WEEKS_PK, "SK": str(week_id)}),
            ProjectionExpression="#week",
            ExpressionAttributeNames={"#week": "week"},
            ConsistentRead=True,
        )
        if not result.get("Item"):
            return False
        week = MomotalkWeek.model_validate(unmarshal_item(result["Item"])["week"])
        if week != expected:
            raise AnnouncementError("momotalk_publication_invalid")
        # Only the saved ready flag is needed; never load names or chat text here.
        for page in self.client.get_paginator("query").paginate(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk",
            FilterExpression="record_type = :type AND payload.#state = :ready",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues=marshal_item(
                {":pk": f"MOMOTALK#WEEK#{week_id}", ":type": "momotalk_room", ":ready": "ready"}
            ),
            ProjectionExpression="payload.#state",
            ConsistentRead=True,
        ):
            if page.get("Items"):
                return True
        return False

    def load(self, week_id: date) -> Receipt | None:
        result = self.client.get_item(
            TableName=self.table_name,
            Key=marshal_item({"PK": RECEIPTS_PK, "SK": str(week_id)}),
            ConsistentRead=True,
        )
        if not result.get("Item"):
            return None
        item = unmarshal_item(result["Item"])
        fingerprint = item.get("fingerprint")
        state = item.get("state")
        if (
            item.get("schema_version") != 1
            or not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            or state not in ("attempted", "sent")
        ):
            raise AnnouncementError("momotalk_announcement_receipt_invalid")
        return Receipt(fingerprint, "sent" if state == "sent" else "attempted")

    def reserve(self, week_id: date, fingerprint: str, now: datetime) -> bool:
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item=marshal_item(
                    {
                        "PK": RECEIPTS_PK,
                        "SK": str(week_id),
                        "schema_version": 1,
                        "fingerprint": fingerprint,
                        "state": "attempted",
                        "attempted_at": now.astimezone(UTC).isoformat(),
                    }
                ),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            return False
        return True

    def mark_sent(self, week_id: date, fingerprint: str, now: datetime) -> None:
        self.client.update_item(
            TableName=self.table_name,
            Key=marshal_item({"PK": RECEIPTS_PK, "SK": str(week_id)}),
            UpdateExpression="SET #state = :sent, verified_at = :now",
            ConditionExpression="fingerprint = :fingerprint AND #state = :attempted",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues=marshal_item(
                {
                    ":fingerprint": fingerprint,
                    ":attempted": "attempted",
                    ":sent": "sent",
                    ":now": now.astimezone(UTC).isoformat(),
                }
            ),
        )


def announcement_content(week_id: date, public_hostname: str) -> str:
    if public_hostname != "shittim.pitekusu.dev":
        raise AnnouncementError("unexpected_records_hostname")
    # Include the week so content-based duplicate discovery never matches another week.
    # Angle brackets keep the URL clickable without an asynchronous link-preview embed.
    return (
        "**新しいモモトークが公開されました!**\n\n"
        f"{week_id.year}年{week_id.month}月{week_id.day}日公開分\n"
        "アロナ・プラナ・安倍晋三AIの新しい会話をお楽しみください。\n\n"
        f"[モモトークを開く](<https://{public_hostname}/momotalk>)"
    )


def publish_announcement(
    store: DynamoMomotalkAnnouncements,
    discord: httpx.Client,
    runtime: DiscordRuntimeConfig,
    *,
    week_id: date,
    now: datetime,
    public_hostname: str,
) -> str:
    receipt = store.load(week_id)
    if receipt is not None and receipt.state == "sent":
        return "already_posted"
    if not store.readable(week_id, now):
        return "not_ready"
    target = resolve_target_channel(discord, runtime, channel_id=runtime.farewell_channel_id)
    content = announcement_content(week_id, public_hostname)
    nonce = announcement_nonce(f"momotalk-{week_id}")
    fingerprint = announcement_fingerprint(target, content)
    if receipt is not None and receipt.fingerprint != fingerprint:
        raise AnnouncementError("announcement_id_reused_for_different_content_or_target")
    existing = find_existing_announcement(discord, target, content=content, nonce=nonce)
    if existing is not None:
        verify_posted_announcement(discord, target, existing, content)
        if receipt is None and not store.reserve(week_id, fingerprint, now):
            return "in_progress"
        store.mark_sent(week_id, fingerprint, now)
        return "already_posted"
    if receipt is not None:
        # The POST may have succeeded before a timeout. Keep the receipt indefinitely;
        # recent Discord history and enforce_nonce both have bounded deduplication windows.
        raise AnnouncementError("previous_send_attempt_exists_check_discord_before_retry")
    if not store.reserve(week_id, fingerprint, now):
        return "in_progress"
    posted = post_announcement(discord, target, content=content, nonce=nonce)
    verify_posted_announcement(discord, target, posted, content)
    store.mark_sent(week_id, fingerprint, now)
    return "posted_and_verified"


def load_discord_config(client: SSMClient, runtime_name: str) -> tuple[DiscordRuntimeConfig, str]:
    if re.fullmatch(r"/shittim-chest/production/runtime/v[0-9]{4}", runtime_name) is None:
        raise AnnouncementError("invalid_runtime_parameter")
    parameter = client.get_parameter(Name=runtime_name, WithDecryption=True).get("Parameter", {})
    value = parameter.get("Value")
    if parameter.get("Name") != runtime_name or not isinstance(value, str) or not value:
        raise AnnouncementError("invalid_ssm_parameter")
    runtime, version = parse_discord_runtime_config(value)
    if runtime_name.rsplit("/", 1)[-1] != version:
        raise AnnouncementError("unexpected_runtime_version")
    parameter = client.get_parameter(Name=MODERATOR_TOKEN_PARAMETER, WithDecryption=True).get(
        "Parameter", {}
    )
    token = parameter.get("Value")
    if (
        parameter.get("Name") != MODERATOR_TOKEN_PARAMETER
        or not isinstance(token, str)
        or not token
        or token != token.strip()
        or "\r" in token
        or "\n" in token
    ):
        raise AnnouncementError("invalid_moderator_token")
    return runtime, token


def event_week(event: Mapping[str, Any], now: datetime) -> date:
    if event.get("source") == "aws.events" and event.get("detail-type") == "Scheduled Event":
        scheduled = datetime.fromisoformat(event["time"])
        if scheduled.tzinfo is None or not timedelta(0) <= now - scheduled <= timedelta(days=1):
            raise AnnouncementError("invalid_announcement_event")
        local = scheduled.astimezone(TOKYO)
        if local.weekday() != 6 or local.hour != 20 or local.minute != 0:
            raise AnnouncementError("invalid_announcement_event")
        return local.date()
    if event.get("source") == "shittim.momotalk" and set(event) == {"source", "weekId"}:
        return validate_week_id(event["weekId"])
    raise AnnouncementError("invalid_announcement_event")


def handler(event: Mapping[str, Any], _context: object) -> dict[str, str]:
    for name in ("boto3", "botocore", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        now = datetime.now(UTC)
        week_id = event_week(event, now)
        store = DynamoMomotalkAnnouncements(
            boto3.client("dynamodb", config=SDK_CONFIG), os.environ["STATISTICS_TABLE_NAME"]
        )
        receipt = store.load(week_id)
        if receipt is not None and receipt.state == "sent":
            return {"state": "already_posted"}
        if not store.readable(week_id, now):
            return {"state": "not_ready"}
        runtime, token = load_discord_config(
            boto3.client("ssm", config=SDK_CONFIG), os.environ["SHITTIM_RUNTIME_CONFIG_PARAMETER"]
        )
        with create_discord_client(token) as discord:
            state = publish_announcement(
                store,
                discord,
                runtime,
                week_id=week_id,
                now=now,
                public_hostname=os.environ["RECORDS_PUBLIC_HOSTNAME"],
            )
        return {"state": state}
    except Exception as error:
        category = error.category if isinstance(error, AnnouncementError) else type(error).__name__
        LOGGER.error("momotalk_announcement_failed category=%s", category)
        raise RuntimeError("MOMOTALK_ANNOUNCEMENT_FAILED") from None
