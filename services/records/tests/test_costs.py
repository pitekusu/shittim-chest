"""Deterministic cost collection and JPY view tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast

import pytest

from shittim_records.costs import (
    COST_CATEGORIES,
    CostCategory,
    CostCheckpoint,
    CostCollectionFailed,
    CostCollectionService,
    CostProviderUnavailable,
    CostSourceName,
    ProviderDailyCost,
    ProviderDailyRate,
    StoredDailyCost,
    StoredDailyRate,
    build_cost_view,
    period_bounds,
)

NOW = datetime(2026, 8, 23, 3, 17, tzinfo=UTC)
TODAY = date(2026, 8, 23)


class FakeCostSource:
    def __init__(self, source: Literal["AWS", "OPENAI"], *, fail: bool = False) -> None:
        self.source = source
        self.fail = fail
        self.calls: list[tuple[date, date]] = []

    def fetch(self, *, start: date, end: date) -> tuple[ProviderDailyCost, ...]:
        self.calls.append((start, end))
        if self.fail:
            raise CostProviderUnavailable(self.source, "provider_unavailable")
        categories = ("FARGATE", "LAMBDA", "OTHER_AWS") if self.source == "AWS" else ("OPENAI",)
        return tuple(
            ProviderDailyCost(
                cost_date=start + timedelta(days=index),
                category=category,
                amount_usd=Decimal("1"),
                estimated=False,
                components=(("residual", Decimal("1")),) if category == "OTHER_AWS" else (),
            )
            for index in range((end - start).days)
            for category in categories
        )


class FakeRateSource:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[date, date]] = []

    def fetch(self, *, start: date, end: date) -> tuple[ProviderDailyRate, ...]:
        self.calls.append((start, end))
        if self.fail:
            raise CostProviderUnavailable("FRANKFURTER", "provider_unavailable")
        return tuple(
            ProviderDailyRate(start + timedelta(days=index), Decimal("150"))
            for index in range((end - start).days)
        )


class FakeStore:
    def __init__(self, checkpoints: dict[str, CostCheckpoint] | None = None) -> None:
        self.checkpoints = checkpoints or {}
        self.cost_writes: list[dict[str, object]] = []
        self.rate_writes: list[dict[str, object]] = []
        self.failure_writes: list[dict[str, object]] = []

    def load_checkpoint(self, source: CostSourceName) -> CostCheckpoint | None:
        return self.checkpoints.get(source)

    def save_cost_window(
        self,
        *,
        source: Literal["AWS", "OPENAI"],
        costs: tuple[ProviderDailyCost, ...],
        next_date: date,
        initial_complete: bool,
        collected_at: datetime,
    ) -> None:
        self.cost_writes.append(
            {
                "source": source,
                "costs": costs,
                "next_date": next_date,
                "initial_complete": initial_complete,
                "collected_at": collected_at,
            }
        )
        self.checkpoints[source] = CostCheckpoint(
            source=source,
            next_date=next_date,
            initial_complete=initial_complete,
        )

    def save_rate_window(
        self,
        *,
        rates: tuple[ProviderDailyRate, ...],
        next_date: date,
        initial_complete: bool,
        collected_at: datetime,
    ) -> None:
        self.rate_writes.append(
            {
                "rates": rates,
                "next_date": next_date,
                "initial_complete": initial_complete,
                "collected_at": collected_at,
            }
        )
        self.checkpoints["FRANKFURTER"] = CostCheckpoint(
            source="FRANKFURTER",
            next_date=next_date,
            initial_complete=initial_complete,
        )

    def save_failure(
        self,
        *,
        checkpoint: CostCheckpoint,
        code: str,
        failed_at: datetime,
    ) -> None:
        self.failure_writes.append({"checkpoint": checkpoint, "code": code, "failed_at": failed_at})
        self.checkpoints[checkpoint.source] = CostCheckpoint(
            source=checkpoint.source,
            next_date=checkpoint.next_date,
            initial_complete=checkpoint.initial_complete,
            last_success_at=checkpoint.last_success_at,
            last_failure_code=code,
            last_failure_at=failed_at,
        )


def service(
    *,
    store: FakeStore | None = None,
    aws_fail: bool = False,
    fx_fail: bool = False,
) -> tuple[CostCollectionService, FakeCostSource, FakeCostSource, FakeRateSource, FakeStore]:
    ledger = store or FakeStore()
    aws = FakeCostSource("AWS", fail=aws_fail)
    openai = FakeCostSource("OPENAI")
    rates = FakeRateSource(fail=fx_fail)
    return (
        CostCollectionService(aws=aws, openai=openai, exchange=rates, store=ledger),
        aws,
        openai,
        rates,
        ledger,
    )


def test_initial_collection_commits_six_thirty_day_windows() -> None:
    costs, _aws, openai, _rates, store = service()

    result = costs.refresh(mode="openai", now=NOW)

    assert result[0].windows == 6
    assert result[0].days == 180
    assert result[0].initial_complete is True
    assert openai.calls[0] == (date(2026, 2, 25), date(2026, 3, 27))
    assert openai.calls[-1] == (date(2026, 7, 25), date(2026, 8, 24))
    assert len(store.cost_writes) == 6


def test_completed_collection_refreshes_only_the_latest_seven_days() -> None:
    checkpoint = CostCheckpoint("OPENAI", date(2026, 8, 24), True)
    costs, _aws, openai, _rates, store = service(store=FakeStore({"OPENAI": checkpoint}))

    result = costs.refresh(mode="openai", now=NOW)

    assert result[0].days == 7
    assert openai.calls == [(date(2026, 8, 17), date(2026, 8, 24))]
    assert len(store.cost_writes) == 1


def test_aws_and_exchange_succeed_independently() -> None:
    costs, _aws, _openai, rates, store = service(aws_fail=True)

    with pytest.raises(CostCollectionFailed) as captured:
        costs.refresh(mode="aws_fx", now=NOW)

    assert [summary.source for summary in captured.value.summaries] == ["FRANKFURTER"]
    assert [failure.source for failure in captured.value.failures] == ["AWS"]
    assert len(rates.calls) == 6
    assert len(store.rate_writes) == 6
    assert store.failure_writes == [
        {
            "checkpoint": CostCheckpoint("AWS", date(2026, 2, 25), False),
            "code": "provider_unavailable",
            "failed_at": NOW,
        }
    ]


def test_incomplete_exchange_window_preserves_a_stable_failure_checkpoint() -> None:
    class IncompleteRates(FakeRateSource):
        def fetch(self, *, start: date, end: date) -> tuple[ProviderDailyRate, ...]:
            return super().fetch(start=start, end=end)[:-1]

    store = FakeStore()
    rates = IncompleteRates()
    costs = CostCollectionService(
        aws=FakeCostSource("AWS"),
        openai=FakeCostSource("OPENAI"),
        exchange=rates,
        store=store,
    )

    with pytest.raises(CostCollectionFailed) as captured:
        costs.refresh(mode="aws_fx", now=NOW)

    assert [failure.source for failure in captured.value.failures] == ["FRANKFURTER"]
    assert store.failure_writes == [
        {
            "checkpoint": CostCheckpoint("FRANKFURTER", date(2026, 2, 25), False),
            "code": "cost_data_invalid",
            "failed_at": NOW,
        }
    ]


def stored_cost(
    day: date,
    category: CostCategory,
    amount: str,
    *,
    estimated: bool = False,
) -> StoredDailyCost:
    return StoredDailyCost(
        cost_date=day,
        category=category,
        amount_usd=Decimal(amount),
        estimated=estimated,
        collected_at=datetime(2026, 8, 23, 0, tzinfo=UTC),
    )


def test_cost_view_uses_same_day_rates_and_jst_timestamps() -> None:
    day = date(2026, 8, 22)
    costs = tuple(
        stored_cost(day, category, str(index)) for index, category in enumerate(COST_CATEGORIES, 1)
    )
    rates = (
        StoredDailyRate(
            rate_date=day,
            usd_jpy=Decimal("150.1234567"),
            collected_at=datetime(2026, 8, 23, 1, tzinfo=UTC),
        ),
    )

    result = build_cost_view(costs=costs, rates=rates, period="all", now=NOW)

    assert result.start_date == day
    assert result.end_date == TODAY
    assert result.amounts_jpy == {
        "FARGATE": "150.123457",
        "LAMBDA": "300.246913",
        "OPENAI": "450.370370",
        "OTHER_AWS": "600.493827",
    }
    assert result.total_jpy == "1501.234567"
    assert result.updated_at is not None and result.updated_at.isoformat().endswith("+09:00")
    assert result.status == "partial"


def test_cost_view_does_not_substitute_a_missing_rate() -> None:
    day = date(2026, 8, 22)
    costs = tuple(stored_cost(day, category, "1") for category in COST_CATEGORIES)

    result = build_cost_view(costs=costs, rates=(), period="all", now=NOW)

    assert result.total_jpy == "0.000000"
    assert result.status == "unavailable"
    assert result.conversion_updated_at is None


def test_cost_view_returns_unavailable_without_valid_cost_records() -> None:
    result = build_cost_view(costs=(), rates=(), period="week", now=NOW)

    assert result.status == "unavailable"
    assert result.start_date == date(2026, 8, 17)
    assert result.end_date == TODAY


def test_cost_view_rolls_to_the_next_japanese_year_at_jst_midnight() -> None:
    before_midnight = build_cost_view(
        costs=(),
        rates=(),
        period="today",
        now=datetime(2026, 12, 31, 14, 59, 59, tzinfo=UTC),
    )
    at_midnight = build_cost_view(
        costs=(),
        rates=(),
        period="week",
        now=datetime(2026, 12, 31, 15, 0, tzinfo=UTC),
    )

    assert before_midnight.end_date == date(2026, 12, 31)
    assert at_midnight.start_date == date(2026, 12, 26)
    assert at_midnight.end_date == date(2027, 1, 1)


def test_cost_view_freshness_uses_each_category_latest_collection() -> None:
    day = date(2026, 8, 22)
    old = datetime(2026, 8, 20, tzinfo=UTC)
    current = datetime(2026, 8, 23, 2, tzinfo=UTC)
    costs = (
        *(
            StoredDailyCost(day, category, Decimal("1"), False, current)
            for category in COST_CATEGORIES
        ),
        StoredDailyCost(date(2026, 8, 21), "FARGATE", Decimal("1"), False, old),
    )
    rates = (
        StoredDailyRate(day, Decimal("150"), current),
        StoredDailyRate(date(2026, 8, 21), Decimal("149"), old),
    )

    result = build_cost_view(costs=costs, rates=rates, period="all", now=NOW)

    assert result.updated_at is not None
    assert result.conversion_updated_at is not None
    assert result.updated_at == current.astimezone(result.updated_at.tzinfo)
    assert result.conversion_updated_at == current.astimezone(result.conversion_updated_at.tzinfo)


@pytest.mark.parametrize(
    ("period", "expected"),
    (
        ("today", (date(2026, 8, 23), date(2026, 8, 23))),
        ("week", (date(2026, 8, 17), date(2026, 8, 23))),
        ("month", (date(2026, 8, 1), date(2026, 8, 23))),
        ("all", (date(2026, 7, 1), date(2026, 8, 23))),
    ),
)
def test_period_bounds_use_japanese_calendar_dates(period: str, expected: object) -> None:
    assert (
        period_bounds(
            cast(Literal["today", "week", "month", "all"], period),
            today=TODAY,
            stored_dates=(date(2026, 7, 1),),
        )
        == expected
    )
