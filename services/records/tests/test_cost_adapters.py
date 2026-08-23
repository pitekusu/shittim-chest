"""Cost provider and DynamoDB ledger adapter contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from shittim_chest.adapters.dynamodb.codec import unmarshal_item

from shittim_records.cost_adapters import (
    AwsCostExplorerSource,
    CostConfigurationRepository,
    DynamoCostLedgerStore,
    FrankfurterRateSource,
    OpenAICostSource,
    parse_stored_costs,
    parse_stored_rates,
)
from shittim_records.costs import (
    CostCheckpoint,
    CostDataInvalid,
    CostProviderUnavailable,
    ProviderDailyCost,
    ProviderDailyRate,
)

START = date(2026, 8, 22)
END = date(2026, 8, 24)
COLLECTED_AT = datetime(2026, 8, 24, tzinfo=UTC)


class FakeCostExplorer:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = iter(pages)
        self.calls: list[dict[str, Any]] = []

    def get_cost_and_usage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return next(self.pages)


class FakeSsm:
    def get_parameters(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"Names": ["admin", "project"], "WithDecryption": True}
        return {
            "Parameters": [
                {"Name": "admin", "Value": "test-admin-key"},
                {"Name": "project", "Value": "project-example"},
            ]
        }


class FakeDynamo:
    def __init__(self) -> None:
        self.item: dict[str, Any] | None = None
        self.transactions: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["ConsistentRead"] is True
        return {} if self.item is None else {"Item": self.item}

    def transact_write_items(self, **kwargs: Any) -> None:
        self.transactions.append(kwargs)


def aws_row(day: date, groups: list[tuple[str, str, str]]) -> dict[str, Any]:
    return {
        "TimePeriod": {
            "Start": day.isoformat(),
            "End": (day + date.resolution).isoformat(),
        },
        "Estimated": False,
        "Groups": [
            {
                "Keys": [service, usage],
                "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}},
            }
            for service, usage, amount in groups
        ],
    }


def test_aws_costs_classify_fargate_lambda_residual_and_exclude_route53() -> None:
    rows = [
        aws_row(
            day,
            [
                ("Amazon Elastic Container Service", "APS1-Fargate-vCPU-Hours:perCPU", "1"),
                ("AWS Lambda", "Lambda-GB-Second", "2"),
                ("AmazonCloudWatch", "TimedStorage-ByteHrs", "3"),
                ("Amazon Route 53", "HostedZone", "4"),
                ("New AWS Service", "Example", "5"),
            ],
        )
        for day in (START, START + date.resolution)
    ]
    client = FakeCostExplorer(
        [
            {"ResultsByTime": [rows[0]], "NextPageToken": "page-2"},
            {"ResultsByTime": [rows[1]]},
        ]
    )

    records = AwsCostExplorerSource(cast(Any, client)).fetch(start=START, end=END)

    assert [(record.category, record.amount_usd) for record in records[:3]] == [
        ("FARGATE", Decimal("1")),
        ("LAMBDA", Decimal("2")),
        ("OTHER_AWS", Decimal("8")),
    ]
    assert dict(records[2].components)["cloudwatch"] == Decimal("3")
    assert dict(records[2].components)["residual"] == Decimal("5")
    call = client.calls[0]
    assert call["TimePeriod"] == {"Start": "2026-08-22", "End": "2026-08-24"}
    assert call["Metrics"] == ["UnblendedCost"]
    assert "Credit" in repr(call["Filter"])
    assert client.calls[1]["NextPageToken"] == "page-2"


def test_openai_costs_require_complete_project_scoped_pages_and_retry_503_once() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.url.params["project_ids"] == "project-example"
        assert request.url.params["group_by"] == "project_id"
        if attempts == 1:
            return httpx.Response(503, headers={"Retry-After": "1"})
        return httpx.Response(
            200,
            json={
                "object": "page",
                "data": [
                    {
                        "object": "bucket",
                        "start_time": 1787356800,
                        "end_time": 1787443200,
                        "results": [
                            {
                                "object": "organization.costs.result",
                                "project_id": "project-example",
                                "amount": {"value": 0.125, "currency": "usd"},
                            }
                        ],
                    },
                    {
                        "object": "bucket",
                        "start_time": 1787443200,
                        "end_time": 1787529600,
                        "results": [],
                    },
                ],
                "has_more": False,
                "next_page": None,
            },
        )

    configuration = CostConfigurationRepository(
        cast(Any, FakeSsm()),
        admin_key_parameter_name="admin",
        project_id_parameter_name="project",
    )
    source = OpenAICostSource(
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
        configuration,
        sleeper=sleeps.append,
    )

    records = source.fetch(start=START, end=END)

    assert attempts == 2
    assert sleeps == [1.0]
    assert [record.amount_usd for record in records] == [Decimal("0.125"), Decimal("0")]


def test_openai_costs_reject_another_project_without_revealing_it() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "start_time": 1787356800,
                        "end_time": 1787443200,
                        "results": [
                            {
                                "project_id": "another-project",
                                "amount": {"value": 1, "currency": "usd"},
                            }
                        ],
                    }
                ],
                "has_more": False,
                "next_page": None,
            },
        )

    source = OpenAICostSource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        CostConfigurationRepository(
            cast(Any, FakeSsm()),
            admin_key_parameter_name="admin",
            project_id_parameter_name="project",
        ),
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(CostProviderUnavailable) as caught:
        source.fetch(start=START, end=START + date.resolution)

    assert caught.value.code == "provider_output_invalid"
    assert "another-project" not in str(caught.value)


def test_openai_costs_follow_each_unique_page_cursor() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        second_page = request.url.params.get("page") == "next-safe-page"
        day = START + (date.resolution if second_page else date.resolution * 0)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "start_time": int(
                            datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()
                        ),
                        "end_time": int(
                            datetime(
                                (day + date.resolution).year,
                                (day + date.resolution).month,
                                (day + date.resolution).day,
                                tzinfo=UTC,
                            ).timestamp()
                        ),
                        "results": [
                            {
                                "project_id": "project-example",
                                "amount": {"value": "0.01", "currency": "usd"},
                            }
                        ],
                    }
                ],
                "has_more": not second_page,
                "next_page": None if second_page else "next-safe-page",
            },
        )

    source = OpenAICostSource(
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
        CostConfigurationRepository(
            cast(Any, FakeSsm()),
            admin_key_parameter_name="admin",
            project_id_parameter_name="project",
        ),
        sleeper=lambda _seconds: None,
    )

    records = source.fetch(start=START, end=END)

    assert [record.amount_usd for record in records] == [Decimal("0.01"), Decimal("0.01")]
    assert requests[0].url.params.get("page") is None
    assert requests[1].url.params["page"] == "next-safe-page"


def test_frankfurter_uses_inclusive_to_and_rejects_redirects_by_client_policy() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {"date": "2026-08-22", "base": "USD", "quote": "JPY", "rate": 150.1},
                {"date": "2026-08-23", "base": "USD", "quote": "JPY", "rate": 150.2},
            ],
        )

    source = FrankfurterRateSource(
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
        sleeper=lambda _seconds: None,
    )

    records = source.fetch(start=START, end=END)

    assert [record.usd_jpy for record in records] == [Decimal("150.1"), Decimal("150.2")]
    assert requests[0].url.params["from"] == "2026-08-22"
    assert requests[0].url.params["to"] == "2026-08-23"
    with pytest.raises(ValueError, match="must not follow redirects"):
        FrankfurterRateSource(httpx.Client(follow_redirects=True))


def test_dynamo_ledger_preserves_integer_decimals_and_roundtrips_exact_records() -> None:
    client = FakeDynamo()
    store = DynamoCostLedgerStore(cast(Any, client), "statistics")
    store.save_cost_window(
        source="AWS",
        costs=(
            ProviderDailyCost(START, "FARGATE", Decimal("10"), False),
            ProviderDailyCost(START, "LAMBDA", Decimal("0.0100"), False),
            ProviderDailyCost(
                START,
                "OTHER_AWS",
                Decimal("0"),
                False,
                components=(
                    ("cloudwatch", Decimal("0")),
                    ("public_ipv4", Decimal("0")),
                    ("dynamodb", Decimal("0")),
                    ("s3", Decimal("0")),
                    ("cloudfront", Decimal("0")),
                    ("api_gateway", Decimal("0")),
                    ("ecr", Decimal("0")),
                    ("inspector", Decimal("0")),
                    ("residual", Decimal("0")),
                ),
            ),
        ),
        next_date=START + date.resolution,
        initial_complete=True,
        collected_at=COLLECTED_AT,
    )
    store.save_rate_window(
        rates=(ProviderDailyRate(START, Decimal("150.25")),),
        next_date=START + date.resolution,
        initial_complete=True,
        collected_at=COLLECTED_AT,
    )

    cost_items = tuple(
        unmarshal_item(action["Put"]["Item"])
        for action in client.transactions[0]["TransactItems"][:-1]
    )
    rate_items = tuple(
        unmarshal_item(action["Put"]["Item"])
        for action in client.transactions[1]["TransactItems"][:-1]
    )
    assert cost_items[0]["amount_usd"] == "10"
    assert cost_items[1]["amount_usd"] == "0.01"
    assert parse_stored_costs(cost_items)[0].amount_usd == Decimal("10")
    assert parse_stored_rates(rate_items)[0].usd_jpy == Decimal("150.25")

    store.save_failure(
        checkpoint=CostCheckpoint(
            "OPENAI",
            START,
            False,
            last_success_at=COLLECTED_AT,
        ),
        code="provider_http_503",
        failed_at=COLLECTED_AT,
    )
    failure_item = client.transactions[2]["TransactItems"][0]["Put"]["Item"]
    client.item = failure_item
    checkpoint = store.load_checkpoint("OPENAI")

    assert checkpoint is not None
    assert checkpoint.last_success_at == COLLECTED_AT
    assert checkpoint.last_failure_code == "provider_http_503"
    assert checkpoint.last_failure_at == COLLECTED_AT


def test_configuration_rejects_whitespace_wrapped_values() -> None:
    class InvalidSsm:
        def get_parameters(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Parameters": [
                    {"Name": "admin", "Value": " secret"},
                    {"Name": "project", "Value": "project-example"},
                ]
            }

    repository = CostConfigurationRepository(
        cast(Any, InvalidSsm()),
        admin_key_parameter_name="admin",
        project_id_parameter_name="project",
    )

    with pytest.raises(CostProviderUnavailable) as caught:
        repository.load()

    assert caught.value.code == "configuration_invalid"


def test_parse_rejects_unknown_daily_cost_fields() -> None:
    with pytest.raises(CostDataInvalid, match="stored cost fields are invalid"):
        parse_stored_costs(
            (
                cast(
                    Any,
                    {
                        "PK": "COST#DAILY",
                        "SK": "2026-08-22#OPENAI",
                        "unexpected": "private",
                    },
                ),
            )
        )
