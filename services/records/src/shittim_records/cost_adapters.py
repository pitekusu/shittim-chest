"""Provider and DynamoDB adapters for Records cost collection."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from datetime import time as datetime_time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, TypeGuard, cast

import httpx
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_ce.client import CostExplorerClient
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef
    from mypy_boto3_ssm.client import SSMClient

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import DynamoItem

from shittim_records.costs import (
    COST_CATEGORIES,
    CostCategory,
    CostCheckpoint,
    CostDataInvalid,
    CostProviderUnavailable,
    CostSourceName,
    ProviderDailyCost,
    ProviderDailyRate,
    StoredDailyCost,
    StoredDailyRate,
)

OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
FRANKFURTER_RATES_URL = "https://api.frankfurter.dev/v2/rates"
MAX_PROVIDER_BODY_BYTES = 1_048_576
MAX_RETRY_AFTER_SECONDS = 5.0
MAX_TRANSACTION_ACTIONS = 100
MAX_TRANSACTION_BYTES = 4 * 1024 * 1024
MAX_FAILURE_CODE_LENGTH = 64
PROJECT_TAG_VALUE = "shittim-chest"
AWS_CATEGORIES: tuple[CostCategory, ...] = ("FARGATE", "LAMBDA", "OTHER_AWS")
KNOWN_OTHER_COMPONENTS: tuple[str, ...] = (
    "cloudwatch",
    "public_ipv4",
    "dynamodb",
    "s3",
    "cloudfront",
    "api_gateway",
    "ecr",
    "inspector",
    "residual",
)

Sleeper = Callable[[float], None]


class CostConfigurationRepository:
    """Load exact OpenAI cost credentials from two SecureStrings."""

    def __init__(
        self,
        client: SSMClient,
        *,
        admin_key_parameter_name: str,
        project_id_parameter_name: str,
    ) -> None:
        self._client = client
        self._names = (admin_key_parameter_name, project_id_parameter_name)
        self._cached: tuple[str, str] | None = None

    def load(self) -> tuple[str, str]:
        if self._cached is not None:
            return self._cached
        try:
            response = self._client.get_parameters(Names=list(self._names), WithDecryption=True)
        except (BotoCoreError, ClientError) as error:
            raise CostProviderUnavailable("OPENAI", "configuration_unavailable") from error
        if response.get("InvalidParameters"):
            raise CostProviderUnavailable("OPENAI", "configuration_unavailable")
        values = {
            parameter.get("Name"): parameter.get("Value")
            for parameter in response.get("Parameters", [])
        }
        if set(values) != set(self._names):
            raise CostProviderUnavailable("OPENAI", "configuration_unavailable")
        admin_key, project_id = (values[name] for name in self._names)
        if not _is_exact_secret(admin_key) or not _is_exact_secret(project_id):
            raise CostProviderUnavailable("OPENAI", "configuration_invalid")
        self._cached = admin_key, project_id
        return self._cached


class AwsCostExplorerSource:
    """Collect Project-tagged AWS costs and classify every residual service."""

    def __init__(self, client: CostExplorerClient) -> None:
        self._client = client

    def fetch(self, *, start: date, end: date) -> tuple[ProviderDailyCost, ...]:
        values: dict[date, dict[CostCategory, Decimal]] = {
            day: {category: Decimal(0) for category in AWS_CATEGORIES}
            for day in _date_range(start, end)
        }
        components: dict[date, dict[str, Decimal]] = {
            day: {component: Decimal(0) for component in KNOWN_OTHER_COMPONENTS}
            for day in _date_range(start, end)
        }
        estimated: dict[date, bool] = {day: False for day in _date_range(start, end)}
        seen_dates: set[date] = set()
        try:
            request: dict[str, Any] = {
                "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
                "Granularity": "DAILY",
                "Metrics": ["UnblendedCost"],
                "Filter": {
                    "And": [
                        {
                            "Tags": {
                                "Key": "Project",
                                "Values": [PROJECT_TAG_VALUE],
                                "MatchOptions": ["EQUALS"],
                            }
                        },
                        {
                            "Not": {
                                "Dimensions": {
                                    "Key": "RECORD_TYPE",
                                    "Values": ["Credit", "Refund"],
                                }
                            }
                        },
                    ]
                },
                "GroupBy": [
                    {"Type": "DIMENSION", "Key": "SERVICE"},
                    {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
                ],
            }
            seen_tokens: set[str] = set()
            while True:
                page = self._client.get_cost_and_usage(**request)
                results = page.get("ResultsByTime")
                if not isinstance(results, list):
                    raise CostDataInvalid("AWS result page is invalid")
                for result in results:
                    if not isinstance(result, dict):
                        raise CostDataInvalid("AWS result row is invalid")
                    day = _cost_explorer_day(result, start=start, end=end)
                    seen_dates.add(day)
                    if result.get("Estimated") is True:
                        estimated[day] = True
                    elif result.get("Estimated") is not False:
                        raise CostDataInvalid("AWS estimated marker is invalid")
                    groups = result.get("Groups")
                    if not isinstance(groups, list):
                        raise CostDataInvalid("AWS cost groups are invalid")
                    for group in groups:
                        if not isinstance(group, dict):
                            raise CostDataInvalid("AWS cost group is invalid")
                        keys = group.get("Keys")
                        metrics = group.get("Metrics")
                        if not isinstance(metrics, dict):
                            raise CostDataInvalid("AWS cost metrics are invalid")
                        metric = metrics.get("UnblendedCost")
                        if not isinstance(metric, dict):
                            raise CostDataInvalid("AWS unblended cost is invalid")
                        if not isinstance(keys, list) or len(keys) != 2:
                            raise CostDataInvalid("AWS cost group identity is invalid")
                        service, usage_type = keys
                        if not isinstance(service, str) or not isinstance(usage_type, str):
                            raise CostDataInvalid("AWS cost group identity is invalid")
                        if metric.get("Unit") != "USD":
                            raise CostDataInvalid("AWS cost currency is invalid")
                        amount = _decimal(metric.get("Amount"), field="AWS amount", allow_zero=True)
                        category, component = _classify_aws(service, usage_type)
                        if category is None:
                            continue
                        values[day][category] += amount
                        if component is not None:
                            components[day][component] += amount
                next_token = page.get("NextPageToken")
                if next_token is None:
                    break
                if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                    raise CostDataInvalid("AWS pagination is invalid")
                seen_tokens.add(next_token)
                request["NextPageToken"] = next_token
        except CostDataInvalid as error:
            raise CostProviderUnavailable("AWS", "provider_output_invalid") from error
        except (BotoCoreError, ClientError) as error:
            raise CostProviderUnavailable("AWS", "provider_unavailable") from error
        if seen_dates != set(_date_range(start, end)):
            raise CostProviderUnavailable("AWS", "provider_output_incomplete")

        records: list[ProviderDailyCost] = []
        for day in _date_range(start, end):
            for category in AWS_CATEGORIES:
                records.append(
                    ProviderDailyCost(
                        cost_date=day,
                        category=category,
                        amount_usd=values[day][category],
                        estimated=estimated[day],
                        components=(
                            tuple((name, amount) for name, amount in components[day].items())
                            if category == "OTHER_AWS"
                            else ()
                        ),
                    )
                )
        return tuple(records)


class OpenAICostSource:
    """Collect complete daily Organization Costs for one exact project."""

    def __init__(
        self,
        client: httpx.Client,
        configuration: CostConfigurationRepository,
        *,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self._client = client
        self._configuration = configuration
        self._sleeper = sleeper

    def fetch(self, *, start: date, end: date) -> tuple[ProviderDailyCost, ...]:
        admin_key, project_id = self._configuration.load()
        amounts = {day: Decimal(0) for day in _date_range(start, end)}
        seen_buckets: set[date] = set()
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            params: list[tuple[str, str]] = [
                ("start_time", str(_unix_day(start))),
                ("end_time", str(_unix_day(end))),
                ("bucket_width", "1d"),
                ("limit", str((end - start).days)),
                ("project_ids", project_id),
                ("group_by", "project_id"),
            ]
            if page_token is not None:
                params.append(("page", page_token))
            payload = _request_json(
                self._client,
                OPENAI_COSTS_URL,
                params=params,
                headers={"Authorization": f"Bearer {admin_key}"},
                source="OPENAI",
                sleeper=self._sleeper,
            )
            try:
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise CostDataInvalid("OpenAI page is invalid")
                for bucket in payload["data"]:
                    day = _openai_bucket_day(bucket, start=start, end=end)
                    if day in seen_buckets:
                        raise CostDataInvalid("OpenAI bucket is duplicated")
                    seen_buckets.add(day)
                    results = bucket.get("results")
                    if not isinstance(results, list):
                        raise CostDataInvalid("OpenAI results are invalid")
                    for result in results:
                        if not isinstance(result, dict) or result.get("project_id") != project_id:
                            raise CostDataInvalid("OpenAI project is invalid")
                        amount = result.get("amount")
                        if not isinstance(amount, dict) or amount.get("currency") != "usd":
                            raise CostDataInvalid("OpenAI amount is invalid")
                        amounts[day] += _decimal(
                            amount.get("value"), field="OpenAI amount", allow_zero=True
                        )
                has_more = payload.get("has_more")
                next_page = payload.get("next_page")
                if not isinstance(has_more, bool):
                    raise CostDataInvalid("OpenAI pagination is invalid")
                if not has_more:
                    if next_page is not None:
                        raise CostDataInvalid("OpenAI pagination is invalid")
                    break
                if not isinstance(next_page, str) or not next_page or next_page in seen_tokens:
                    raise CostDataInvalid("OpenAI pagination is invalid")
                seen_tokens.add(next_page)
                page_token = next_page
            except CostDataInvalid as error:
                raise CostProviderUnavailable("OPENAI", "provider_output_invalid") from error
        if seen_buckets != set(_date_range(start, end)):
            raise CostProviderUnavailable("OPENAI", "provider_output_incomplete")
        return tuple(
            ProviderDailyCost(
                cost_date=day,
                category="OPENAI",
                amount_usd=amounts[day],
                estimated=False,
            )
            for day in _date_range(start, end)
        )


class FrankfurterRateSource:
    """Collect exact same-day USD/JPY reference rates from Frankfurter v2."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        if client.follow_redirects:
            raise ValueError("Frankfurter client must not follow redirects")
        self._client = client
        self._sleeper = sleeper

    def fetch(self, *, start: date, end: date) -> tuple[ProviderDailyRate, ...]:
        payload = _request_json(
            self._client,
            FRANKFURTER_RATES_URL,
            params=[
                ("from", start.isoformat()),
                ("to", (end - date.resolution).isoformat()),
                ("base", "USD"),
                ("quotes", "JPY"),
            ],
            headers={},
            source="FRANKFURTER",
            sleeper=self._sleeper,
        )
        try:
            if not isinstance(payload, list):
                raise CostDataInvalid("Frankfurter response is invalid")
            rates: list[ProviderDailyRate] = []
            for row in payload:
                if (
                    not isinstance(row, dict)
                    or set(row) != {"date", "base", "quote", "rate"}
                    or row.get("base") != "USD"
                    or row.get("quote") != "JPY"
                ):
                    raise CostDataInvalid("Frankfurter row is invalid")
                rate_date = date.fromisoformat(cast(str, row.get("date")))
                rates.append(
                    ProviderDailyRate(
                        rate_date=rate_date,
                        usd_jpy=_decimal(
                            row.get("rate"), field="Frankfurter rate", allow_zero=False
                        ),
                    )
                )
            return tuple(rates)
        except (CostDataInvalid, TypeError, ValueError) as error:
            raise CostProviderUnavailable("FRANKFURTER", "provider_output_invalid") from error


class DynamoCostLedgerStore:
    """Store bounded daily cost/rate windows and one checkpoint atomically."""

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def load_checkpoint(self, source: CostSourceName) -> CostCheckpoint | None:
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key=marshal_item({"PK": "COLLECTOR#COST", "SK": source}),
                ConsistentRead=True,
            )
        except (BotoCoreError, ClientError) as error:
            raise CostProviderUnavailable(source, "ledger_unavailable") from error
        raw = response.get("Item")
        if raw is None:
            return None
        item = unmarshal_item(raw)
        required = {
            "PK",
            "SK",
            "schema_version",
            "record_type",
            "source",
            "next_date",
            "initial_complete",
        }
        optional = {"last_success_at", "last_failure_code", "last_failure_at"}
        if (
            not required <= set(item) <= required | optional
            or item.get("PK") != "COLLECTOR#COST"
            or item.get("SK") != source
            or item.get("source") != source
            or item.get("schema_version") != 1
            or item.get("record_type") != "cost_checkpoint"
            or not isinstance(item.get("initial_complete"), bool)
        ):
            raise CostDataInvalid("cost checkpoint is invalid")
        try:
            next_date = date.fromisoformat(cast(str, item["next_date"]))
            last_success_at = (
                _canonical_utc(cast(str, item["last_success_at"]))
                if "last_success_at" in item
                else None
            )
            has_failure_code = "last_failure_code" in item
            has_failure_at = "last_failure_at" in item
            if has_failure_code != has_failure_at:
                raise ValueError("incomplete failure metadata")
            last_failure_code = (
                _failure_code(cast(str, item["last_failure_code"])) if has_failure_code else None
            )
            last_failure_at = (
                _canonical_utc(cast(str, item["last_failure_at"])) if has_failure_at else None
            )
        except (TypeError, ValueError) as error:
            raise CostDataInvalid("cost checkpoint is invalid") from error
        return CostCheckpoint(
            source=source,
            next_date=next_date,
            initial_complete=cast(bool, item["initial_complete"]),
            last_success_at=last_success_at,
            last_failure_code=last_failure_code,
            last_failure_at=last_failure_at,
        )

    def save_cost_window(
        self,
        *,
        source: str,
        costs: tuple[ProviderDailyCost, ...],
        next_date: date,
        initial_complete: bool,
        collected_at: datetime,
    ) -> None:
        if source not in {"AWS", "OPENAI"}:
            raise ValueError("cost source is invalid")
        items: list[DynamoItem] = []
        for cost in costs:
            item: DynamoItem = {
                "PK": "COST#DAILY",
                "SK": f"{cost.cost_date.isoformat()}#{cost.category}",
                "schema_version": 1,
                "record_type": "daily_cost",
                "source": source,
                "cost_date": cost.cost_date.isoformat(),
                "category": cost.category,
                "amount_usd": _decimal_string(cost.amount_usd),
                "currency": "USD",
                "estimated": cost.estimated,
                "collected_at": _utc_iso(collected_at),
            }
            if cost.category == "OTHER_AWS":
                item["components"] = {
                    name: _decimal_string(value) for name, value in cost.components
                }
            items.append(item)
        self._write_window(
            items=tuple(items),
            source=cast(CostSourceName, source),
            next_date=next_date,
            initial_complete=initial_complete,
            collected_at=collected_at,
        )

    def save_rate_window(
        self,
        *,
        rates: tuple[ProviderDailyRate, ...],
        next_date: date,
        initial_complete: bool,
        collected_at: datetime,
    ) -> None:
        items: tuple[DynamoItem, ...] = tuple(
            {
                "PK": "FX#DAILY",
                "SK": f"{rate.rate_date.isoformat()}#USDJPY",
                "schema_version": 1,
                "record_type": "daily_exchange_rate",
                "source": "FRANKFURTER",
                "rate_date": rate.rate_date.isoformat(),
                "base_currency": "USD",
                "quote_currency": "JPY",
                "rate": _decimal_string(rate.usd_jpy),
                "collected_at": _utc_iso(collected_at),
            }
            for rate in rates
        )
        self._write_window(
            items=items,
            source="FRANKFURTER",
            next_date=next_date,
            initial_complete=initial_complete,
            collected_at=collected_at,
        )

    def save_failure(
        self,
        *,
        checkpoint: CostCheckpoint,
        code: str,
        failed_at: datetime,
    ) -> None:
        code = _failure_code(code)
        item = _checkpoint_item(
            source=checkpoint.source,
            next_date=checkpoint.next_date,
            initial_complete=checkpoint.initial_complete,
            last_success_at=checkpoint.last_success_at,
            last_failure_code=code,
            last_failure_at=failed_at,
        )
        self._write_items(items=(item,), source=checkpoint.source)

    def _write_window(
        self,
        *,
        items: tuple[DynamoItem, ...],
        source: CostSourceName,
        next_date: date,
        initial_complete: bool,
        collected_at: datetime,
    ) -> None:
        checkpoint = _checkpoint_item(
            source=source,
            next_date=next_date,
            initial_complete=initial_complete,
            last_success_at=collected_at,
        )
        self._write_items(items=(*items, checkpoint), source=source)

    def _write_items(
        self,
        *,
        items: tuple[DynamoItem, ...],
        source: CostSourceName,
    ) -> None:
        actions: list[TransactWriteItemTypeDef] = [
            {"Put": {"TableName": self._table_name, "Item": marshal_item(item)}} for item in items
        ]
        if not 1 <= len(actions) <= MAX_TRANSACTION_ACTIONS:
            raise ValueError("cost transaction action count is invalid")
        encoded = json.dumps(actions, ensure_ascii=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_TRANSACTION_BYTES:
            raise ValueError("cost transaction exceeds the DynamoDB transaction size limit")
        try:
            self._client.transact_write_items(TransactItems=actions)
        except (BotoCoreError, ClientError) as error:
            raise CostProviderUnavailable(source, "ledger_unavailable") from error


def parse_stored_costs(items: tuple[DynamoItem, ...]) -> tuple[StoredDailyCost, ...]:
    records: list[StoredDailyCost] = []
    for item in items:
        if item.get("PK") != "COST#DAILY":
            continue
        expected = {
            "PK",
            "SK",
            "schema_version",
            "record_type",
            "source",
            "cost_date",
            "category",
            "amount_usd",
            "currency",
            "estimated",
            "collected_at",
        }
        if item.get("category") == "OTHER_AWS":
            expected.add("components")
        if set(item) != expected:
            raise CostDataInvalid("stored cost fields are invalid")
        category = item.get("category")
        source = item.get("source")
        if (
            category not in COST_CATEGORIES
            or source not in {"AWS", "OPENAI"}
            or (source == "OPENAI") != (category == "OPENAI")
            or item.get("schema_version") != 1
            or item.get("record_type") != "daily_cost"
            or item.get("currency") != "USD"
            or not isinstance(item.get("estimated"), bool)
        ):
            raise CostDataInvalid("stored cost is invalid")
        try:
            cost_date = date.fromisoformat(cast(str, item["cost_date"]))
            collected_at = _canonical_utc(cast(str, item["collected_at"]))
            amount = _decimal(item["amount_usd"], field="stored amount", allow_zero=True)
        except (TypeError, ValueError) as error:
            raise CostDataInvalid("stored cost is invalid") from error
        if item.get("SK") != f"{cost_date.isoformat()}#{category}":
            raise CostDataInvalid("stored cost identity is invalid")
        if category == "OTHER_AWS" and _validate_components(item.get("components")) != amount:
            raise CostDataInvalid("stored other AWS components are invalid")
        records.append(
            StoredDailyCost(
                cost_date=cost_date,
                category=cast(CostCategory, category),
                amount_usd=amount,
                estimated=cast(bool, item["estimated"]),
                collected_at=collected_at,
            )
        )
    return tuple(records)


def parse_stored_rates(items: tuple[DynamoItem, ...]) -> tuple[StoredDailyRate, ...]:
    records: list[StoredDailyRate] = []
    for item in items:
        if item.get("PK") != "FX#DAILY":
            continue
        if (
            set(item)
            != {
                "PK",
                "SK",
                "schema_version",
                "record_type",
                "source",
                "rate_date",
                "base_currency",
                "quote_currency",
                "rate",
                "collected_at",
            }
            or item.get("schema_version") != 1
            or item.get("record_type") != "daily_exchange_rate"
            or item.get("source") != "FRANKFURTER"
            or item.get("base_currency") != "USD"
            or item.get("quote_currency") != "JPY"
        ):
            raise CostDataInvalid("stored exchange rate is invalid")
        try:
            rate_date = date.fromisoformat(cast(str, item["rate_date"]))
            collected_at = _canonical_utc(cast(str, item["collected_at"]))
            rate = _decimal(item["rate"], field="stored rate", allow_zero=False)
        except (TypeError, ValueError) as error:
            raise CostDataInvalid("stored exchange rate is invalid") from error
        if item.get("SK") != f"{rate_date.isoformat()}#USDJPY":
            raise CostDataInvalid("stored exchange rate identity is invalid")
        records.append(
            StoredDailyRate(rate_date=rate_date, usd_jpy=rate, collected_at=collected_at)
        )
    return tuple(records)


def _checkpoint_item(
    *,
    source: CostSourceName,
    next_date: date,
    initial_complete: bool,
    last_success_at: datetime | None = None,
    last_failure_code: str | None = None,
    last_failure_at: datetime | None = None,
) -> DynamoItem:
    item: DynamoItem = {
        "PK": "COLLECTOR#COST",
        "SK": source,
        "schema_version": 1,
        "record_type": "cost_checkpoint",
        "source": source,
        "next_date": next_date.isoformat(),
        "initial_complete": initial_complete,
    }
    if last_success_at is not None:
        item["last_success_at"] = _utc_iso(last_success_at)
    if (last_failure_code is None) != (last_failure_at is None):
        raise ValueError("failure metadata must be complete")
    if last_failure_code is not None and last_failure_at is not None:
        item["last_failure_code"] = _failure_code(last_failure_code)
        item["last_failure_at"] = _utc_iso(last_failure_at)
    return item


def _request_json(
    client: httpx.Client,
    url: str,
    *,
    params: list[tuple[str, str]],
    headers: Mapping[str, str],
    source: CostSourceName,
    sleeper: Sleeper,
) -> object:
    for attempt in range(2):
        try:
            with client.stream(
                "GET",
                url,
                params=httpx.QueryParams(tuple(params)),
                headers=headers,
            ) as response:
                if response.status_code == 200:
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
                    if content_type != "application/json":
                        raise CostProviderUnavailable(source, "provider_output_invalid")
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_PROVIDER_BODY_BYTES:
                            raise CostProviderUnavailable(source, "provider_output_invalid")
                    try:
                        return json.loads(content, parse_float=Decimal)
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise CostProviderUnavailable(source, "provider_output_invalid") from error
                status_code = response.status_code
                retry_after = response.headers.get("retry-after")
        except httpx.TransportError as error:
            if attempt == 0:
                sleeper(0.25)
                continue
            raise CostProviderUnavailable(source, "provider_unavailable") from error
        if attempt == 0 and (status_code == 429 or 500 <= status_code <= 599):
            sleeper(_retry_after(retry_after))
            continue
        raise CostProviderUnavailable(source, f"provider_http_{status_code}")
    raise AssertionError("provider retry loop did not terminate")


def _retry_after(value: str | None) -> float:
    if value is None:
        return 0.25
    try:
        seconds = float(value)
    except ValueError:
        return 0.25
    if seconds < 0:
        return 0.25
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _failure_code(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_FAILURE_CODE_LENGTH
        or not value.isascii()
        or value != value.casefold()
        or not value.replace("_", "").isalnum()
    ):
        raise ValueError("failure code is invalid")
    return value


def _classify_aws(service: str, usage_type: str) -> tuple[CostCategory | None, str | None]:
    if service == "Amazon Route 53":
        return None, None
    if service == "Amazon Elastic Container Service" and "fargate" in usage_type.casefold():
        return "FARGATE", None
    if service == "AWS Lambda":
        return "LAMBDA", None
    lowered = service.casefold()
    if "cloudwatch" in lowered:
        component = "cloudwatch"
    elif service == "Amazon Virtual Private Cloud" and "publicipv4" in usage_type.casefold():
        component = "public_ipv4"
    elif "dynamodb" in lowered:
        component = "dynamodb"
    elif service == "Amazon Simple Storage Service":
        component = "s3"
    elif "cloudfront" in lowered:
        component = "cloudfront"
    elif "api gateway" in lowered:
        component = "api_gateway"
    elif "container registry" in lowered or service == "Amazon Elastic Container Registry Public":
        component = "ecr"
    elif "inspector" in lowered:
        component = "inspector"
    else:
        component = "residual"
    return "OTHER_AWS", component


def _cost_explorer_day(row: Mapping[str, Any], *, start: date, end: date) -> date:
    period = row.get("TimePeriod")
    if not isinstance(period, dict):
        raise CostDataInvalid("AWS time period is invalid")
    try:
        day = date.fromisoformat(cast(str, period.get("Start")))
        period_end = date.fromisoformat(cast(str, period.get("End")))
    except (TypeError, ValueError) as error:
        raise CostDataInvalid("AWS time period is invalid") from error
    if day < start or day >= end or period_end != day + date.resolution:
        raise CostDataInvalid("AWS time period is invalid")
    return day


def _openai_bucket_day(bucket: object, *, start: date, end: date) -> date:
    if not isinstance(bucket, dict):
        raise CostDataInvalid("OpenAI bucket is invalid")
    start_time = bucket.get("start_time")
    end_time = bucket.get("end_time")
    if isinstance(start_time, bool) or not isinstance(start_time, int):
        raise CostDataInvalid("OpenAI bucket is invalid")
    if isinstance(end_time, bool) or not isinstance(end_time, int):
        raise CostDataInvalid("OpenAI bucket is invalid")
    try:
        day = datetime.fromtimestamp(start_time, UTC).date()
    except (OSError, OverflowError, ValueError) as error:
        raise CostDataInvalid("OpenAI bucket is invalid") from error
    if start_time != _unix_day(day) or end_time != _unix_day(day + date.resolution):
        raise CostDataInvalid("OpenAI bucket is invalid")
    if not start <= day < end:
        raise CostDataInvalid("OpenAI bucket is outside the requested range")
    return day


def _decimal(value: object, *, field: str, allow_zero: bool) -> Decimal:
    if isinstance(value, bool):
        raise CostDataInvalid(f"{field} is invalid")
    try:
        result = value if isinstance(value, Decimal) else Decimal(cast(str, value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise CostDataInvalid(f"{field} is invalid") from error
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise CostDataInvalid(f"{field} is invalid")
    return result


def _decimal_string(value: Decimal) -> str:
    value = _decimal(value, field="decimal", allow_zero=True)
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _is_exact_secret(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _validate_components(value: object) -> Decimal:
    if not isinstance(value, dict) or set(value) != set(KNOWN_OTHER_COMPONENTS):
        raise CostDataInvalid("stored other AWS components are invalid")
    total = Decimal(0)
    for amount in value.values():
        total += _decimal(amount, field="stored component", allow_zero=True)
    return total


def _date_range(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + date.resolution * index for index in range((end - start).days))


def _unix_day(day: date) -> int:
    return int(datetime.combine(day, datetime_time.min, tzinfo=UTC).timestamp())


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _canonical_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is not timezone-aware")
    normalized = parsed.astimezone(UTC)
    if normalized.isoformat() != value:
        raise ValueError("timestamp is not canonical UTC")
    return normalized
