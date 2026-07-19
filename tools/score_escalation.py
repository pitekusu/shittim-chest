#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate blind human scores and generate a content-free policy summary."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import fmean
from typing import cast

from tools.evaluate_escalation import REPOSITORY_ROOT, RUBRIC_AXES, validate_output_directory

POLICIES = {"terra_standard", "luna_pro"}


@dataclass(frozen=True, slots=True)
class ScoredResult:
    status: str
    scores: tuple[int, ...]
    failure_category: str | None
    elapsed_ms: float
    input_tokens: float
    output_tokens: float
    reasoning_tokens: float
    estimated_usd: float | None


@dataclass(frozen=True, slots=True)
class PolicySummary:
    runs: int
    successful_runs: int
    major_failures: int
    operational_failures: int
    quality_mean: float | None
    preference_wins: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    p95_latency_ms: int
    estimated_total_usd: float | None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blind-results", type=Path, required=True)
    parser.add_argument("--policy-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--preference-only",
        action="store_true",
        help="rank policies from blind A/B/tie choices without rubric scores",
    )
    parser.add_argument("--quality-tie-margin", type=float, default=0.05)
    parser.add_argument("--max-p95-ms", type=int)
    parser.add_argument("--max-total-usd", type=float)
    return parser.parse_args(argv)


def load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return cast(dict[str, object], value)


def aggregate(
    blind: Mapping[str, object],
    key: Mapping[str, object],
    *,
    quality_tie_margin: float,
    max_p95_ms: int | None,
    max_total_usd: float | None,
    preference_only: bool = False,
) -> dict[str, object]:
    if quality_tie_margin < 0:
        raise ValueError("quality tie margin must not be negative")
    if max_p95_ms is not None and max_p95_ms < 1:
        raise ValueError("maximum p95 latency must be positive")
    if max_total_usd is not None and max_total_usd < 0:
        raise ValueError("maximum total cost must not be negative")
    _validate_pair(blind, key)
    key_by_case = _key_by_case(key)
    records: dict[str, list[ScoredResult]] = defaultdict(list)
    preferences: dict[str, int] = defaultdict(int)
    ties = 0

    for case in _cases(blind):
        case_id = _text(case.get("case_id"), "case ID")
        key_case = key_by_case.get(case_id)
        if key_case is None:
            raise ValueError(f"policy key is missing case {case_id}")
        mapping = key_case["mapping"]
        metrics_by_label = key_case["metrics"]
        for label in ("A", "B"):
            policy = _text(mapping.get(label), f"{case_id} policy {label}")
            if policy not in POLICIES:
                raise ValueError(f"unsupported policy {policy}")
            result = case.get(label)
            if not isinstance(result, dict):
                raise ValueError(f"{case_id} result {label} must be an object")
            typed_result = cast(dict[str, object], result)
            metrics = metrics_by_label.get(label)
            if not isinstance(metrics, dict):
                raise ValueError(f"{case_id} policy key is missing metrics for {label}")
            typed_metrics = cast(dict[str, object], metrics)
            records[policy].append(
                _validated_result(
                    typed_result,
                    metrics=typed_metrics,
                    case_id=case_id,
                    label=label,
                    preference_only=preference_only,
                )
            )
        preference = case.get("preference")
        if preference == "tie":
            ties += 1
        elif preference in {"A", "B"}:
            preferences[_text(mapping.get(str(preference)), "preferred policy")] += 1
        else:
            raise ValueError(f"{case_id} preference must be A, B, or tie")

    typed_summaries = {
        policy: _summarize(items, preferences[policy]) for policy, items in records.items()
    }
    recommendation = _recommend(
        typed_summaries,
        quality_tie_margin=quality_tie_margin,
        max_p95_ms=max_p95_ms,
        max_total_usd=max_total_usd,
        preference_only=preference_only,
    )
    return {
        "version": "escalation-summary-v1",
        "scoring_mode": "preference_only" if preference_only else "rubric",
        "evaluation_id": blind["evaluation_id"],
        "fixture_sha256": blind["fixture_sha256"],
        "case_count": len(_cases(blind)),
        "preference_ties": ties,
        "policies": {policy: asdict(summary) for policy, summary in typed_summaries.items()},
        "recommendation": recommendation,
    }


def _validate_pair(blind: Mapping[str, object], key: Mapping[str, object]) -> None:
    if blind.get("version") != "escalation-blind-v2":
        raise ValueError("unsupported blind results version")
    if key.get("version") != "escalation-key-v2":
        raise ValueError("unsupported policy key version")
    for field in ("evaluation_id", "fixture_sha256"):
        if blind.get(field) != key.get(field):
            raise ValueError(f"blind results and policy key {field} do not match")


def _cases(value: Mapping[str, object]) -> list[dict[str, object]]:
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("evaluation must contain cases")
    if not all(isinstance(case, dict) for case in cases):
        raise ValueError("every evaluation case must be an object")
    return cast(list[dict[str, object]], cases)


def _key_by_case(key: Mapping[str, object]) -> dict[str, dict[str, dict[str, object]]]:
    result: dict[str, dict[str, dict[str, object]]] = {}
    for case in _cases(key):
        case_id = _text(case.get("case_id"), "key case ID")
        mapping = case.get("mapping")
        if not isinstance(mapping, dict) or set(mapping) != {"A", "B"}:
            raise ValueError(f"{case_id} policy mapping must contain A and B")
        typed_mapping = cast(dict[str, object], mapping)
        metrics = case.get("metrics")
        if not isinstance(metrics, dict) or set(metrics) != {"A", "B"}:
            raise ValueError(f"{case_id} policy metrics must contain A and B")
        typed_metrics = cast(dict[str, object], metrics)
        result[case_id] = {"mapping": typed_mapping, "metrics": typed_metrics}
    return result


def _validated_result(
    result: Mapping[str, object],
    *,
    metrics: Mapping[str, object],
    case_id: str,
    label: str,
    preference_only: bool,
) -> ScoredResult:
    status = result.get("status")
    rubric = result.get("rubric")
    if not isinstance(rubric, dict):
        raise ValueError(f"{case_id} result {label} requires a rubric")
    typed_rubric = cast(dict[str, object], rubric)
    values = list(typed_rubric.values())
    if set(typed_rubric) != set(RUBRIC_AXES):
        raise ValueError(f"{case_id} result {label} has an invalid rubric")
    if status == "succeeded":
        if preference_only:
            if any(value is not None for value in values):
                raise ValueError(
                    f"{case_id} result {label} preference-only rubric must remain unscored"
                )
        elif not all(
            isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5
            for value in values
        ):
            raise ValueError(f"{case_id} result {label} rubric scores must be integers 1-5")
    elif status == "failed":
        if any(value is not None for value in values):
            raise ValueError(f"{case_id} failed result {label} must not be scored")
    else:
        raise ValueError(f"{case_id} result {label} has an invalid status")
    failure = result.get("failure")
    category = None
    if status == "failed":
        if not isinstance(failure, dict) or failure.get("category") not in {"major", "operational"}:
            raise ValueError(f"{case_id} failed result {label} requires a failure category")
        category = cast(dict[str, object], failure)["category"]
    return ScoredResult(
        status=str(status),
        scores=tuple(value for value in values if isinstance(value, int)),
        failure_category=category if isinstance(category, str) else None,
        elapsed_ms=_nonnegative_number(metrics.get("elapsed_ms"), "elapsed_ms"),
        input_tokens=_nonnegative_number(metrics.get("input_tokens"), "input_tokens"),
        output_tokens=_nonnegative_number(metrics.get("output_tokens"), "output_tokens"),
        reasoning_tokens=_nonnegative_number(metrics.get("reasoning_tokens"), "reasoning_tokens"),
        estimated_usd=_optional_nonnegative_number(metrics.get("estimated_usd"), "estimated_usd"),
    )


def _summarize(items: list[ScoredResult], preference_wins: int) -> PolicySummary:
    succeeded = [item for item in items if item.status == "succeeded"]
    scores = [float(score) for item in succeeded for score in item.scores]
    latencies = [item.elapsed_ms for item in items]
    costs = [item.estimated_usd for item in items]
    has_cost = all(cost is not None for cost in costs)
    return PolicySummary(
        runs=len(items),
        successful_runs=len(succeeded),
        major_failures=sum(item.failure_category == "major" for item in items),
        operational_failures=sum(item.failure_category == "operational" for item in items),
        quality_mean=None if not scores else round(fmean(scores), 4),
        preference_wins=preference_wins,
        input_tokens=int(sum(item.input_tokens for item in items)),
        output_tokens=int(sum(item.output_tokens for item in items)),
        reasoning_tokens=int(sum(item.reasoning_tokens for item in items)),
        p95_latency_ms=round(_percentile95(latencies)),
        estimated_total_usd=(
            round(sum(cost for cost in costs if cost is not None), 6) if has_cost else None
        ),
    )


def _recommend(
    summaries: Mapping[str, PolicySummary],
    *,
    quality_tie_margin: float,
    max_p95_ms: int | None,
    max_total_usd: float | None,
    preference_only: bool,
) -> dict[str, object]:
    if set(summaries) != POLICIES:
        return {"status": "needs_operator", "reason": "both policies require results"}
    if any(summary.operational_failures > 0 for summary in summaries.values()):
        return {"status": "rerun_required", "reason": "operational failures occurred"}
    minimum_major = min(summary.major_failures for summary in summaries.values())
    eligible = {
        policy: summary
        for policy, summary in summaries.items()
        if summary.major_failures == minimum_major
        and (preference_only or summary.quality_mean is not None)
        and (max_p95_ms is None or summary.p95_latency_ms <= max_p95_ms)
        and (
            max_total_usd is None
            or (
                summary.estimated_total_usd is not None
                and summary.estimated_total_usd <= max_total_usd
            )
        )
    }
    if not eligible:
        return {"status": "needs_operator", "reason": "no policy meets operational limits"}
    if preference_only:
        ranked_by_preference = sorted(
            eligible.items(), key=lambda item: item[1].preference_wins, reverse=True
        )
        if len(ranked_by_preference) == 1:
            return {
                "status": "candidate",
                "policy": ranked_by_preference[0][0],
                "reason": "only eligible policy",
            }
        first, second = ranked_by_preference
        if first[1].preference_wins > second[1].preference_wins:
            return {
                "status": "candidate",
                "policy": first[0],
                "reason": "more blind preference wins",
            }
        cheaper = _cheaper_or_faster(first, second)
        return {
            "status": "candidate",
            "policy": cheaper,
            "reason": "preference tie; lower cost or latency",
        }
    ranked = sorted(eligible.items(), key=lambda item: item[1].quality_mean or 0.0, reverse=True)
    if len(ranked) == 1:
        return {"status": "candidate", "policy": ranked[0][0], "reason": "only eligible policy"}
    first, second = ranked
    if (first[1].quality_mean or 0.0) - (second[1].quality_mean or 0.0) > quality_tie_margin:
        return {"status": "candidate", "policy": first[0], "reason": "higher quality mean"}
    cheaper = _cheaper_or_faster(first, second)
    return {
        "status": "candidate",
        "policy": cheaper,
        "reason": "quality tie; lower cost or latency",
    }


def _cheaper_or_faster(first: tuple[str, PolicySummary], second: tuple[str, PolicySummary]) -> str:
    first_cost = first[1].estimated_total_usd
    second_cost = second[1].estimated_total_usd
    if first_cost is not None and second_cost is not None and first_cost != second_cost:
        return first[0] if first_cost < second_cost else second[0]
    return first[0] if first[1].p95_latency_ms <= second[1].p95_latency_ms else second[0]


def _percentile95(values: list[float]) -> float:
    if not values:
        raise ValueError("latency values must not be empty")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _nonnegative_number(value: object, label: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative number")
    return float(value)


def _optional_nonnegative_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    return _nonnegative_number(value, label)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        for path in (args.blind_results, args.policy_key):
            resolved = path.expanduser().resolve()
            if resolved == REPOSITORY_ROOT or REPOSITORY_ROOT in resolved.parents:
                raise ValueError("raw evaluation inputs must remain outside the repository")
        output = validate_output_directory(args.output)
        summary = aggregate(
            load_json(args.blind_results),
            load_json(args.policy_key),
            quality_tie_margin=args.quality_tie_margin,
            max_p95_ms=args.max_p95_ms,
            max_total_usd=args.max_total_usd,
            preference_only=args.preference_only,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"wrote content-free evaluation summary to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
