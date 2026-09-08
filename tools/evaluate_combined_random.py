"""Bounded 20-record random experiment; private inputs and outputs stay in memory."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import secrets
from collections import Counter
from dataclasses import asdict
from time import monotonic
from typing import Any

from shittim_chest.adapters.openai import OpenAIResponsesService
from shittim_chest.application.generation_policy import PhaseBudget, ReasoningEffort
from shittim_chest.domain import PARTICIPANTS, EvidenceBundle
from tools.evaluate_candidate_coordination import coordinate
from tools.evaluate_composite_voting import query
from tools.evaluate_deliberation import (
    _parallel,
    generate_speech,
    load_baseline,
    load_service,
    usage_summary,
)
from tools.evaluate_escalation import UsageCollector
from tools.evaluate_overlap import PAIR_RULES, Overlap, reconsider, reconsider_targets
from tools.evaluate_speech_quality import RUBRIC, Assessment, Judgment


def select_records(records: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    ordered = sorted(records, key=lambda item: item["PK"]["S"])
    if len(ordered) < 20 or len({item["PK"]["S"] for item in ordered}) != len(ordered):
        raise ValueError("twenty distinct archive records required")
    return random.Random(seed).sample(ordered, 20)  # noqa: S311 -- sampling, not security


async def combined(
    service: OpenAIResponsesService, question: str, index: int
) -> tuple[dict[str, object], dict[str, object]]:
    evidence = EvidenceBundle()
    frames = await _parallel(
        *(service.form_preferences(participant=slot, question=question) for slot in PARTICIPANTS)
    )
    plans = await _parallel(
        *(
            service.select_candidates(
                participant=slot, question=question, evidence=evidence, preference_frame=frame
            )
            for slot, frame in zip(PARTICIPANTS, frames, strict=True)
        )
    )
    plans, coordination = await coordinate(service, question, frames, plans, index)
    opinions = await _parallel(
        *(
            service.generate_initial_opinion(
                participant=slot,
                question=question,
                evidence=evidence,
                preference_frame=frame,
                candidate_plan=plan,
            )
            for slot, frame, plan in zip(PARTICIPANTS, frames, plans, strict=True)
        )
    )
    overlap = await service._parse(
        operation="combined_overlap",
        schema=Overlap,
        instructions=PAIR_RULES + "Classify the request. Only open_choice/open_advice permits "
        "alternative legitimate positions. Fixed factual/math answers use fixed_answer; "
        "other requests use other, ambiguity uses uncertain. Do not diversify fixed answers.",
        input_text=json.dumps(
            {"question": question, "opinions": [asdict(x) for x in opinions]}, ensure_ascii=False
        ),
        settings=PhaseBudget(ReasoningEffort.MEDIUM, 3_000),
    )
    targets = reconsider_targets(overlap)  # Shared conclusions with distinct reasons are valid.
    revised = await _parallel(
        *(
            reconsider(service, question, frames[i], plans[i], opinions)
            for i, slot in enumerate(PARTICIPANTS)
            if slot in targets
        )
    )
    by_slot = {plan.participant: (plan, opinion) for plan, opinion in revised}
    changes = sum(plan != plans[PARTICIPANTS.index(plan.participant)] for plan, _ in revised)
    revised_plans = tuple(
        by_slot[slot][0] if slot in by_slot else plans[i] for i, slot in enumerate(PARTICIPANTS)
    )
    revised_opinions = tuple(
        by_slot[slot][1] if slot in by_slot else opinions[i] for i, slot in enumerate(PARTICIPANTS)
    )
    finals = await _parallel(
        *(
            service.generate_final_proposal(
                participant=slot,
                question=question,
                evidence=evidence,
                initial_opinions=revised_opinions,
                preference_frame=frame,
                candidate_plan=plan,
            )
            for slot, frame, plan in zip(PARTICIPANTS, frames, revised_plans, strict=True)
        )
    )
    return {
        "initial": [asdict(x) for x in revised_opinions],
        "final": [asdict(x) for x in finals],
    }, {"coordination": coordination, "reconsidered": len(targets), "changed": changes}


def summarize(observations: list[dict[str, Any]]) -> dict[str, Any]:
    preferences = [item["preferred"] for item in observations]
    disagreements = [
        f"{branch}.{axis}"
        for branch in ("released", "combined")
        for axis in Assessment.model_fields
        if observations[0]["assessments"][branch][axis]
        != observations[1]["assessments"][branch][axis]
    ]
    return {
        "outcome": preferences[0] if preferences[0] == preferences[1] else "disputed",
        "stable_assessment": not disagreements,
        "assessment_disagreements": disagreements,
        "observations": observations,
    }


async def evaluate(commit: str) -> None:
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
    )
    seed = secrets.randbits(64)
    records = select_records(catalog, seed)
    print(
        json.dumps(
            {"population": len(catalog), "sample": 20, "seed": seed, "baseline_commit": commit}
        ),
        flush=True,
    )
    baseline = load_baseline(commit)
    service = await load_service()
    usage = UsageCollector()
    service.recorder = usage
    results: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(records):
            question = item["question"]["S"]
            runs, seconds, intervention = {}, {}, {}
            branches = ("released", "combined") if index % 2 == 0 else ("combined", "released")
            for branch in branches:
                started = monotonic()
                if branch == "released":
                    runs[branch] = await generate_speech(service, question, baseline)
                else:
                    runs[branch], intervention = await combined(service, question, index)
                seconds[branch] = round(monotonic() - started, 1)
            observations = []
            for left, right in (branches, tuple(reversed(branches))):
                judged = await service._parse(
                    operation="combined_quality",
                    schema=Judgment,
                    instructions=RUBRIC,
                    input_text=json.dumps(
                        {
                            "question": question,
                            "personas": {
                                slot: service.profiles.for_participant(slot).system_prompt
                                for slot in PARTICIPANTS
                            },
                            "left": runs[left],
                            "right": runs[right],
                        },
                        ensure_ascii=False,
                    ),
                    settings=PhaseBudget(ReasoningEffort.HIGH, 6_000),
                )
                for positions in (judged.left_positions, judged.right_positions):
                    if tuple(x.participant for x in positions) != PARTICIPANTS:
                        raise ValueError("invalid observation coverage")
                observations.append(
                    {
                        "preferred": {"left": left, "right": right}.get(
                            judged.preferred, judged.preferred
                        ),
                        "basis": judged.basis,
                        "assessments": {
                            left: judged.left.model_dump(),
                            right: judged.right.model_dump(),
                        },
                    }
                )
            result = {
                "case": index + 1,
                "date": item["completed_at"]["S"][:10],
                "seconds": seconds,
                "intervention": intervention,
                **summarize(observations),
            }
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        print(
            json.dumps(
                {
                    "completed": len(results),
                    "outcomes": dict(Counter(x["outcome"] for x in results)),
                    "stable_assessments": sum(x["stable_assessment"] for x in results),
                    "mean_seconds": {
                        branch: round(sum(x["seconds"][branch] for x in results) / 20, 1)
                        for branch in ("released", "combined")
                    },
                    "usage": usage_summary(usage),
                }
            ),
            flush=True,
        )
    finally:
        await service.client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--baseline-commit", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(evaluate(args.baseline_commit))
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "status": "stopped"}), flush=True)
        raise SystemExit(1) from None
