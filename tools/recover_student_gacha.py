#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Inspect or explicitly requeue one parked student-gacha offer."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime

from shittim_chest.adapters.aws import create_control_records_dynamodb_client
from shittim_chest.adapters.dynamodb.student_gacha import DynamoDbStudentGachaRepository
from shittim_chest.application.ports import RepositoryConflict, RepositoryUnavailable


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--offer-id", required=True)
    parser.add_argument("--region", default="ap-northeast-1")
    parser.add_argument("--apply", action="store_true", help="Requeue the parked draw")
    args = parser.parse_args(argv)
    return asyncio.run(_recover(args))


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
