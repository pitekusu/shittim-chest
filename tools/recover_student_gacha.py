#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""List, inspect, or explicitly requeue parked student-gacha offers."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

from shittim_chest.adapters.aws import create_control_records_dynamodb_client
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.student_gacha import DynamoDbStudentGachaRepository
from shittim_chest.application.ports import RepositoryConflict, RepositoryUnavailable

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

_OFFER_KEY = re.compile(r"GACHA#OFFER#([0-9a-f]{32})")
_PARKED_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
_MAX_LIST_LIMIT = 100
_SCAN_PAGE_SIZE = 250


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table-name", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--offer-id", help="Inspect a known offer")
    action.add_argument("--list-parked", action="store_true", help="Find parked offers")
    parser.add_argument("--region", default="ap-northeast-1")
    parser.add_argument("--apply", action="store_true", help="Requeue the parked draw")
    parser.add_argument("--limit", type=int, help="Stop after this many matching offers")
    args = parser.parse_args(argv)
    if args.list_parked:
        if args.apply:
            parser.error("--apply requires --offer-id")
        if args.limit is not None and not 1 <= args.limit <= _MAX_LIST_LIMIT:
            parser.error(f"--limit must be between 1 and {_MAX_LIST_LIMIT}")
        return _list_parked(args)
    if args.limit is not None:
        parser.error("--limit requires --list-parked")
    return asyncio.run(_recover(args))


def _list_parked(args: argparse.Namespace) -> int:
    try:
        client = create_control_records_dynamodb_client(region_name=args.region)
        offers = _parked_offers(client, table_name=args.table_name, limit=args.limit)
    except BotoCoreError, ClientError, ValueError:
        print(json.dumps({"state": "recovery_unavailable"}))
        return 2
    print(json.dumps({"parked_offers": offers}))
    return 0


def _parked_offers(client: DynamoDBClient, *, table_name: str, limit: int) -> list[dict[str, str]]:
    offers: list[dict[str, str]] = []
    pages = client.get_paginator("scan").paginate(
        TableName=table_name,
        ConsistentRead=True,
        FilterExpression="begins_with(PK, :prefix) AND SK=:meta AND #state=:parked",
        ProjectionExpression="PK, parked_at",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues=marshal_item(
            {":prefix": "GACHA#OFFER#", ":meta": "META", ":parked": "delivery_unconfirmed"}
        ),
        PaginationConfig={"PageSize": _SCAN_PAGE_SIZE},
    )
    for page in pages:
        for row in page.get("Items", ()):
            item = unmarshal_item(row)
            key = item.get("PK")
            parked_at = item.get("parked_at")
            match = _OFFER_KEY.fullmatch(key) if isinstance(key, str) else None
            if (
                match is None
                or not isinstance(parked_at, str)
                or not _PARKED_AT.fullmatch(parked_at)
            ):
                raise ValueError("invalid parked offer")
            offers.append({"offer_id": match[1], "parked_at": parked_at})
            if len(offers) == limit:
                return offers
    return offers


async def _recover(args: argparse.Namespace) -> int:
    try:
        repository = DynamoDbStudentGachaRepository(
            client=create_control_records_dynamodb_client(region_name=args.region),
            table_name=args.table_name,
        )
        if args.apply and await repository.resume_parked(args.offer_id, at=datetime.now(UTC)):
            print(json.dumps({"offer_id": args.offer_id, "state": "requeued"}))
            return 0
        state = await repository.recovery_state(args.offer_id)
    except RepositoryConflict, RepositoryUnavailable, ValueError:
        print(json.dumps({"state": "recovery_unavailable"}))
        return 2
    print(json.dumps({"offer_id": args.offer_id, "state": state}))
    return 3 if args.apply or state == "missing" else 0


if __name__ == "__main__":
    raise SystemExit(main())
