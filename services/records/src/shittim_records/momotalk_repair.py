"""Operator-only, one-image recovery; never print private records or exception messages."""

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime

from shittim_records.momotalk import MomotalkFailure, validate_week_id
from shittim_records.momotalk_adapters import MomotalkInputSource
from shittim_records.momotalk_generation import MomotalkGenerationService
from shittim_records.momotalk_handlers import _components
from shittim_records.momotalk_openai import OpenAIMomotalkGenerator


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", required=True)
    parser.add_argument(
        "--participant", required=True, choices=("participant-a", "participant-b", "participant-c")
    )
    parser.add_argument("--mood", required=True, choices=("happy", "unhappy"))
    parser.add_argument(
        "--execute", action="store_true", help="allow one paid generation and image-state update"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    try:
        week_id = validate_week_id(args.week)
        store, assets, queue, reader, configuration, references = _components()
        candidates = [
            room
            for room in store.all_rooms(week_id)
            if room.complete
            and room.state == "ready"
            and any(
                image.participant == args.participant
                and image.mood == args.mood
                and image.state == "failed"
                for image in room.images
            )
        ]
        if len(candidates) != 1:
            print(json.dumps({"state": "not_executed", "candidate_count": len(candidates)}))
            return 2
        if not args.execute:
            print(json.dumps({"state": "dry_run", "candidate_count": 1, "paid_attempts": 1}))
            return 0
        source = MomotalkInputSource(
            store.client, os.environ["ARCHIVE_TABLE_NAME"], store.table_name, reader, configuration
        )
        generator = OpenAIMomotalkGenerator(configuration, references)
        try:
            succeeded = MomotalkGenerationService(
                store, assets, queue, generator
            ).retry_failed_image(
                week_id, candidates[0].room_id, args.mood, source, now=datetime.now(UTC)
            )
        finally:
            generator.close()
        print(json.dumps({"state": "ready" if succeeded else "failed"}))
        return 0 if succeeded else 1
    except Exception as error:
        # SDK/Pydantic messages can contain private input. Only local fixed codes
        # are exposed; provider details are emitted by the allowlisted diagnostics.
        code = (
            error.code
            if isinstance(error, MomotalkFailure)
            and error.code
            in {
                "MOMOTALK_REPAIR_INVALID",
                "MOMOTALK_REPAIR_BUSY",
                "REQUEST_INVALID",
            }
            else "MOMOTALK_REPAIR_UNAVAILABLE"
        )
        print(json.dumps({"state": "failed", "code": code}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
