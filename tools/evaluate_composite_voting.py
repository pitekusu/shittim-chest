"""Read three archived answers and rescore in memory; print only numeric summaries."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from typing import Any

from shittim_chest.domain import (
    PARTICIPANTS,
    EvidenceBundle,
    FinalProposal,
    InitialOpinion,
    select_winner,
)
from shittim_chest.domain.composite_voting import COMPOSITE_VOTING_VERSION
from tools.evaluate_deliberation import load_service


def query(*args: str) -> list[dict[str, Any]]:
    executable = shutil.which("aws")
    if executable is None:
        raise RuntimeError("AWS CLI required")
    process = subprocess.run(  # noqa: S603 -- fixed read-only command, no shell
        [
            executable,
            "dynamodb",
            "query",
            "--region",
            "ap-northeast-1",
            "--table-name",
            "shittim-chest-production-records",
            "--output",
            "json",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode:
        raise RuntimeError("archive read failed")
    return json.loads(process.stdout)["Items"]


async def main() -> None:
    logging.disable(logging.CRITICAL)
    catalog = query(
        "--index-name",
        "gsi1",
        "--key-condition-expression",
        "gsi1pk = :key",
        "--expression-attribute-values",
        '{":key":{"S":"ARCHIVE#COMPLETED"}}',
        "--projection-expression",
        "PK,question,completed_at",
        "--no-scan-index-forward",
        "--max-items",
        "100",
    )
    chosen = []
    for _label, pattern in (
        ("food", "夕飯|夕食|料理"),
        ("leisure", "休日|休み|秋|夏"),
        ("creative", "俳句|短歌|川柳|詩"),
    ):
        match = next(
            (
                item
                for item in catalog
                if re.search(pattern, item["question"]["S"])
                and item["completed_at"]["S"] < "2026-09-06"
                and item not in chosen
            ),
            None,
        )
        if match is None:
            raise RuntimeError("three distinct archive categories required")
        chosen.append(match)
    service = await load_service()
    try:
        for index, meta in enumerate(chosen):
            rows = query(
                "--key-condition-expression",
                "PK = :key",
                "--expression-attribute-values",
                json.dumps({":key": meta["PK"]}),
            )
            by_key = {item["SK"]["S"]: item for item in rows}
            proposals = tuple(
                FinalProposal(
                    slot,
                    by_key[f"FINAL#{slot.value}"]["title"]["S"],
                    by_key[f"FINAL#{slot.value}"]["proposal"]["S"],
                )
                for slot in PARTICIPANTS
            )
            initials = tuple(
                InitialOpinion(
                    slot,
                    by_key[f"INITIAL#{slot.value}"]["summary"]["S"],
                    by_key[f"INITIAL#{slot.value}"]["proposal"]["S"],
                )
                for slot in PARTICIPANTS
            )
            output = {"case": index + 1, "date": meta["completed_at"]["S"][:10]}
            # Interleave both orders to reduce time-of-run confounding. Three repeats
            # per order: 3 cases x 2 orders x 3 repeats x 3 voters = 54 requests.
            for repeat, reverse in ((n, order) for n in range(3) for order in (False, True)):
                label = f"{'reverse' if reverse else 'forward'}_{repeat + 1}"
                votes = await asyncio.gather(
                    *(
                        service.cast_vote(
                            voter=voter,
                            question=meta["question"]["S"],
                            evidence=EvidenceBundle(),
                            candidates=tuple(
                                item
                                for item in (proposals[::-1] if reverse else proposals)
                                if item.participant != voter
                            ),
                            voting_rules_version=COMPOSITE_VOTING_VERSION,
                            debate_key=meta["PK"]["S"],
                            initial_opinions=initials,
                        )
                        for voter in PARTICIPANTS
                    )
                )
                result = select_winner(tuple(votes), debate_key=meta["PK"]["S"])
                run_summary = {
                    "winner": result.winner.value,
                    "decided_by": result.decided_by,
                    "totals": {item.candidate.value: item.score_sum / 2 for item in result.totals},
                    "votes": {vote.voter.value: vote.candidate.value for vote in votes},
                }
                output[label] = run_summary
                print(json.dumps({"case": index + 1, "run": label, **run_summary}), flush=True)
            print(json.dumps(output), flush=True)
    finally:
        await service.client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__}))
        raise SystemExit(1) from None
