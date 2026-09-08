"""The live-only helper keeps inputs private and reports failed paid responses."""

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tools import evaluate_deliberation
from tools.evaluate_deliberation import HistoryCase, _parallel, usage_summary
from tools.evaluate_escalation import UsageCollector

from shittim_chest.adapters.openai import OpenAIFailureRecord, OpenAIUsageRecord


def test_catalog_and_usage_exclude_inputs_but_count_failed_response_tokens() -> None:
    case = HistoryCase("private fixture question", "2026-09-07T00:00:00+00:00")
    assert "private fixture" not in repr(case) + repr(case.catalog(0))
    usage = UsageCollector(
        usages=[
            OpenAIUsageRecord(
                "preferences", "resp_ok", "model", "policy", "standard", 1, 10, 20, 0, 5
            )
        ],
        failures=[
            OpenAIFailureRecord(
                operation="preferences",
                code="openai_incomplete",
                policy_id="policy",
                latency_ms=1,
                diagnostic_context="response_status",
                diagnostic_kind="max_output_tokens",
                max_output_tokens=2_000,
                input_tokens=30,
                output_tokens=40,
                reasoning_tokens=40,
            )
        ],
    )
    summary = usage_summary(usage)
    assert summary["logical_requests"] == 2
    assert summary["successful_responses"] == summary["failed_responses"] == 1
    assert (summary["input_tokens"], summary["output_tokens"], summary["reasoning_tokens"]) == (
        40,
        60,
        45,
    )
    assert summary["failures"] == [asdict(usage.failures[0])]


@pytest.mark.asyncio
async def test_parallel_failure_waits_for_already_started_calls() -> None:
    settled = []

    async def fail() -> str:
        raise ValueError("fixture failure")

    async def succeed() -> str:
        await asyncio.sleep(0)
        settled.append(True)
        return "done"

    with pytest.raises(ValueError, match="fixture failure"):
        await _parallel(fail(), succeed())
    assert settled == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("combined", [False, True])
async def test_revision_comparison_selects_requested_workflow(
    monkeypatch, capsys, combined
) -> None:
    baseline = object()
    choices = tuple(
        evaluate_deliberation.Choices(
            participant=slot,
            initial_choice="fixture choice",
            final_choice="fixture choice",
            answers_request="yes",
        )
        for slot in evaluate_deliberation.PARTICIPANTS
    )
    comparison = evaluate_deliberation.Comparison(
        topic="fixture", left=choices, right=choices, observation="fixture comparison"
    )
    services = [
        SimpleNamespace(
            system_prompt="same fixture system",
            client=SimpleNamespace(close=AsyncMock()),
            _parse=AsyncMock(return_value=comparison),
        )
        for _ in range(2)
    ]
    load = AsyncMock(side_effect=services)
    generate = AsyncMock(return_value={"initial": [], "final": []})
    monkeypatch.setattr(evaluate_deliberation, "load_baseline", lambda _: baseline)
    monkeypatch.setattr(evaluate_deliberation, "load_service", load)
    monkeypatch.setattr(evaluate_deliberation, "generate_speech", generate)
    case = HistoryCase("private fixture question", "2026-09-07T00:00:00+00:00")

    await evaluate_deliberation.evaluate(
        "a" * 40,
        [("case-1", case), ("case-2", case)],
        revisions=("before", "after"),
        combined=combined,
    )

    assert [call.args[0] for call in load.await_args_list] == ["before", "after"]
    assert [call.args[0] for call in generate.await_args_list] == [
        services[0],
        services[1],
        services[1],
        services[0],
    ]
    assert [call.args[2] for call in generate.await_args_list] == [
        baseline,
        None if combined else baseline,
        None if combined else baseline,
        baseline,
    ]
    assert "private fixture question" not in capsys.readouterr().out
    for service in services:
        service.client.close.assert_awaited_once()


def test_persona_revision_options_must_be_paired(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv", ["evaluate", "--history-table", "fixture", "--before-revision", "before"]
    )
    with pytest.raises(SystemExit) as error:
        evaluate_deliberation.main()
    assert error.value.code == 2
