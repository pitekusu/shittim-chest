"""Deterministic cost collection and JPY presentation for Records insights."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Annotated, Literal, Protocol, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shittim_records.contracts import CostPeriod

CostCategory = Literal["FARGATE", "LAMBDA", "OPENAI", "OTHER_AWS"]
CostSourceName = Literal["AWS", "OPENAI", "FRANKFURTER"]

JST = ZoneInfo("Asia/Tokyo")
INITIAL_DAYS = 180
REFRESH_DAYS = 7
WINDOW_DAYS = 30
MAX_INITIAL_WINDOWS = 6
JPY_QUANTUM = Decimal("0.000001")
COST_CATEGORIES: tuple[CostCategory, ...] = (
    "FARGATE",
    "LAMBDA",
    "OPENAI",
    "OTHER_AWS",
)


class CostDataInvalid(ValueError):
    """Raised when provider or stored cost data cannot be trusted."""


class CostProviderUnavailable(RuntimeError):
    """Stable provider failure that contains no response or credential data."""

    def __init__(self, source: CostSourceName, code: str) -> None:
        self.source = source
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProviderDailyCost:
    cost_date: date
    category: CostCategory
    amount_usd: Decimal
    estimated: bool
    components: tuple[tuple[str, Decimal], ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderDailyRate:
    rate_date: date
    usd_jpy: Decimal


@dataclass(frozen=True, slots=True)
class CostCheckpoint:
    source: CostSourceName
    next_date: date
    initial_complete: bool
    last_success_at: datetime | None = None
    last_failure_code: str | None = None
    last_failure_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CollectionSummary:
    source: CostSourceName
    windows: int
    days: int
    initial_complete: bool


class CostCollectionFailed(RuntimeError):
    """One scheduled mode failed after preserving independent successes."""

    def __init__(
        self,
        *,
        summaries: tuple[CollectionSummary, ...],
        failures: tuple[CostProviderUnavailable, ...],
    ) -> None:
        self.summaries = summaries
        self.failures = failures
        super().__init__("cost_collection_failed")


class AwsCostSource(Protocol):
    def fetch(self, *, start: date, end: date) -> tuple[ProviderDailyCost, ...]: ...


class OpenAICostSource(Protocol):
    def fetch(self, *, start: date, end: date) -> tuple[ProviderDailyCost, ...]: ...


class ExchangeRateSource(Protocol):
    def fetch(self, *, start: date, end: date) -> tuple[ProviderDailyRate, ...]: ...


class CostLedgerStore(Protocol):
    def load_checkpoint(self, source: CostSourceName) -> CostCheckpoint | None: ...

    def save_cost_window(
        self,
        *,
        source: Literal["AWS", "OPENAI"],
        costs: tuple[ProviderDailyCost, ...],
        next_date: date,
        initial_complete: bool,
        collected_at: datetime,
    ) -> None: ...

    def save_rate_window(
        self,
        *,
        rates: tuple[ProviderDailyRate, ...],
        next_date: date,
        initial_complete: bool,
        collected_at: datetime,
    ) -> None: ...

    def save_failure(
        self,
        *,
        checkpoint: CostCheckpoint,
        code: str,
        failed_at: datetime,
    ) -> None: ...


class CostCollectionService:
    """Refresh bounded provider windows while checkpointing each committed window."""

    def __init__(
        self,
        *,
        aws: AwsCostSource,
        openai: OpenAICostSource,
        exchange: ExchangeRateSource,
        store: CostLedgerStore,
    ) -> None:
        self._aws = aws
        self._openai = openai
        self._exchange = exchange
        self._store = store

    def refresh(
        self,
        *,
        mode: Literal["aws_fx", "openai"],
        now: datetime,
    ) -> tuple[CollectionSummary, ...]:
        now = _aware_utc(now)
        today = now.astimezone(JST).date()
        if mode == "openai":
            try:
                return (self._refresh_cost_source("OPENAI", today=today, now=now),)
            except CostProviderUnavailable as error:
                raise CostCollectionFailed(summaries=(), failures=(error,)) from error
            except CostDataInvalid as error:
                failure = CostProviderUnavailable("OPENAI", "cost_data_invalid")
                raise CostCollectionFailed(summaries=(), failures=(failure,)) from error

        summaries: list[CollectionSummary] = []
        failures: list[CostProviderUnavailable] = []
        for source in ("AWS", "FRANKFURTER"):
            try:
                summary = (
                    self._refresh_cost_source("AWS", today=today, now=now)
                    if source == "AWS"
                    else self._refresh_exchange(today=today, now=now)
                )
                summaries.append(summary)
            except CostProviderUnavailable as error:
                failures.append(error)
            except CostDataInvalid:
                failures.append(CostProviderUnavailable(source, "cost_data_invalid"))
        if failures:
            raise CostCollectionFailed(summaries=tuple(summaries), failures=tuple(failures))
        return tuple(summaries)

    def _refresh_cost_source(
        self,
        source: Literal["AWS", "OPENAI"],
        *,
        today: date,
        now: datetime,
    ) -> CollectionSummary:
        checkpoint = self._store.load_checkpoint(source)
        windows = _collection_windows(checkpoint, today=today)
        progress = checkpoint or CostCheckpoint(
            source=source,
            next_date=windows[0][0],
            initial_complete=False,
        )
        days = 0
        for start, end, complete in windows:
            try:
                costs = (
                    self._aws.fetch(start=start, end=end)
                    if source == "AWS"
                    else self._openai.fetch(start=start, end=end)
                )
                _validate_cost_window(costs, source=source, start=start, end=end)
            except CostProviderUnavailable as error:
                self._store.save_failure(checkpoint=progress, code=error.code, failed_at=now)
                raise
            except CostDataInvalid:
                self._store.save_failure(
                    checkpoint=progress,
                    code="cost_data_invalid",
                    failed_at=now,
                )
                raise
            self._store.save_cost_window(
                source=source,
                costs=costs,
                next_date=end,
                initial_complete=complete,
                collected_at=now,
            )
            days += (end - start).days
            progress = CostCheckpoint(
                source=source,
                next_date=end,
                initial_complete=complete,
                last_success_at=now,
            )
        return CollectionSummary(
            source=source,
            windows=len(windows),
            days=days,
            initial_complete=windows[-1][2],
        )

    def _refresh_exchange(self, *, today: date, now: datetime) -> CollectionSummary:
        checkpoint = self._store.load_checkpoint("FRANKFURTER")
        windows = _collection_windows(checkpoint, today=today)
        progress = checkpoint or CostCheckpoint(
            source="FRANKFURTER",
            next_date=windows[0][0],
            initial_complete=False,
        )
        days = 0
        for start, end, complete in windows:
            try:
                rates = self._exchange.fetch(start=start, end=end)
                _validate_rate_window(
                    rates,
                    start=start,
                    end=end,
                    allow_trailing_pending=end == today + timedelta(days=1),
                )
            except CostProviderUnavailable as error:
                self._store.save_failure(checkpoint=progress, code=error.code, failed_at=now)
                raise
            except CostDataInvalid:
                self._store.save_failure(
                    checkpoint=progress,
                    code="cost_data_invalid",
                    failed_at=now,
                )
                raise
            self._store.save_rate_window(
                rates=rates,
                next_date=end,
                initial_complete=complete,
                collected_at=now,
            )
            days += (end - start).days
            progress = CostCheckpoint(
                source="FRANKFURTER",
                next_date=end,
                initial_complete=complete,
                last_success_at=now,
            )
        return CollectionSummary(
            source="FRANKFURTER",
            windows=len(windows),
            days=days,
            initial_complete=windows[-1][2],
        )


@dataclass(frozen=True, slots=True)
class StoredDailyCost:
    cost_date: date
    category: CostCategory
    amount_usd: Decimal
    estimated: bool
    collected_at: datetime


@dataclass(frozen=True, slots=True)
class StoredDailyRate:
    rate_date: date
    usd_jpy: Decimal
    collected_at: datetime


CreditUsd = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]


class OpenAISubscriptionRenewal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_date: date
    amount_usd: CreditUsd


class OpenAISubscriptionGrant(OpenAISubscriptionRenewal):
    end_date: date

    @model_validator(mode="after")
    def check_window(self) -> Self:
        if self.end_date <= self.start_date:
            raise ValueError("grant must end after it starts")
        return self


class OpenAISubscription(BaseModel):
    """Verified grants and the expected monthly renewal for the tracked project."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grants: tuple[OpenAISubscriptionGrant, ...] = ()
    monthly_renewal: OpenAISubscriptionRenewal | None = None

    @model_validator(mode="after")
    def check_renewal(self) -> Self:
        if self.monthly_renewal and any(
            grant.end_date > self.monthly_renewal.start_date for grant in self.grants
        ):
            raise ValueError("confirmed grants overlap the renewal schedule")
        return self

    def windows(
        self, *, first_cost_date: date, today: date
    ) -> tuple[tuple[OpenAISubscriptionGrant, ...], OpenAISubscriptionRenewal | None]:
        if self.monthly_renewal is None:
            return self.grants, None
        schedule = self.monthly_renewal
        anchor = schedule.start_date
        month = max(
            0, 12 * (first_cost_date.year - anchor.year) + first_cost_date.month - anchor.month - 1
        )
        start = _renewal_date(anchor, month)
        grants = list(self.grants)
        while start <= today:
            month += 1
            end = _renewal_date(anchor, month)
            grants.append(
                OpenAISubscriptionGrant(
                    start_date=start, end_date=end, amount_usd=schedule.amount_usd
                )
            )
            start = end
        return tuple(grants), OpenAISubscriptionRenewal(
            start_date=start, amount_usd=schedule.amount_usd
        )


def _renewal_date(anchor: date, month_offset: int) -> date:
    year_offset, month_index = divmod(anchor.month - 1 + month_offset, 12)
    year, month = anchor.year + year_offset, month_index + 1
    return date(year, month, min(anchor.day, monthrange(year, month)[1]))


@dataclass(frozen=True, slots=True)
class CostView:
    period: CostPeriod
    start_date: date
    end_date: date
    amounts_jpy: dict[CostCategory, str]
    total_jpy: str
    updated_at: datetime | None
    conversion_updated_at: datetime | None
    status: Literal["partial", "final", "unavailable"]
    openai_subscription_credit_jpy: str
    openai_next_subscription_credit: OpenAISubscriptionRenewal | None


def build_cost_view(
    *,
    costs: tuple[StoredDailyCost, ...],
    rates: tuple[StoredDailyRate, ...],
    period: CostPeriod,
    now: datetime,
    openai_subscription: OpenAISubscription | None = None,
) -> CostView:
    """Convert USD with same-day rates or the prior rate for the in-progress JST day."""

    now = _aware_utc(now)
    today = now.astimezone(JST).date()
    all_dates = tuple(cost.cost_date for cost in costs)
    start_date, end_date = period_bounds(period, today=today, stored_dates=all_dates)
    selected = tuple(cost for cost in costs if start_date <= cost.cost_date <= end_date)
    rate_records = tuple(rate for rate in rates if start_date <= rate.rate_date <= end_date)
    selected_rates = {rate.rate_date: rate for rate in rate_records}
    if len(selected_rates) != len(rate_records):
        raise CostDataInvalid("duplicate exchange rate")
    previous_day_rates = tuple(
        rate for rate in rates if rate.rate_date == today - timedelta(days=1)
    )
    if len(previous_day_rates) > 1:
        raise CostDataInvalid("duplicate exchange rate")
    previous_day_rate = previous_day_rates[0] if previous_day_rates else None

    deductions: dict[date, Decimal] = {}
    incomplete_credit_dates: set[date] = set()
    next_credit = None
    if openai_subscription is not None:
        grants, next_credit = openai_subscription.windows(
            first_cost_date=min(all_dates, default=now.date()), today=now.date()
        )
        deductions, incomplete_credit_dates = _openai_deductions(costs, grants)

    amount_by_category = {category: Decimal(0) for category in COST_CATEGORIES}
    seen: set[tuple[date, CostCategory]] = set()
    latest_cost_timestamp: dict[CostCategory, datetime] = {}
    used_rate_timestamps: list[datetime] = []
    convertible_records = 0
    partial = end_date >= today
    credit_jpy = Decimal(0)
    for cost in selected:
        key = (cost.cost_date, cost.category)
        if key in seen:
            raise CostDataInvalid("duplicate daily cost")
        seen.add(key)
        _nonnegative_decimal(cost.amount_usd, field="stored amount")
        rate = selected_rates.get(cost.cost_date)
        if rate is None and cost.cost_date == today and previous_day_rate is not None:
            rate = previous_day_rate
            partial = True
        if cost.amount_usd != 0 and rate is None:
            partial = True
            continue
        collected_at = _aware_utc(cost.collected_at)
        latest_cost_timestamp[cost.category] = max(
            collected_at,
            latest_cost_timestamp.get(cost.category, collected_at),
        )
        convertible_records += 1
        if rate is not None:
            _positive_decimal(rate.usd_jpy, field="stored exchange rate")
            used_rate_timestamps.append(_aware_utc(rate.collected_at))
            amount = cost.amount_usd
            if cost.category == "OPENAI":
                credit = deductions.get(cost.cost_date, Decimal(0))
                amount -= credit
                credit_jpy += credit * rate.usd_jpy
            amount_by_category[cost.category] += amount * rate.usd_jpy
        if cost.estimated:
            partial = True

    expected_dates = tuple(_dates(start_date, end_date + timedelta(days=1)))
    for expected_date in expected_dates:
        if expected_date in incomplete_credit_dates or any(
            (expected_date, category) not in seen for category in COST_CATEGORIES
        ):
            partial = True

    if not selected or convertible_records == 0:
        status: Literal["partial", "final", "unavailable"] = "unavailable"
    else:
        status = "partial" if partial else "final"
    rounded = {category: _jpy_string(amount_by_category[category]) for category in COST_CATEGORIES}
    total = sum((Decimal(value) for value in rounded.values()), start=Decimal(0))
    return CostView(
        period=period,
        start_date=start_date,
        end_date=end_date,
        amounts_jpy=rounded,
        total_jpy=f"{total:.6f}",
        updated_at=(
            min(latest_cost_timestamp.values()).astimezone(JST) if latest_cost_timestamp else None
        ),
        conversion_updated_at=(
            max(used_rate_timestamps).astimezone(JST) if used_rate_timestamps else None
        ),
        status=status,
        openai_subscription_credit_jpy=_jpy_string(credit_jpy),
        openai_next_subscription_credit=next_credit,
    )


def _openai_deductions(
    costs: tuple[StoredDailyCost, ...], grants: tuple[OpenAISubscriptionGrant, ...]
) -> tuple[dict[date, Decimal], set[date]]:
    """Spend the earliest expiring credit first, including days outside the view."""
    deductions: dict[date, Decimal] = {}
    incomplete: set[date] = set()
    ordered_grants = sorted(grants, key=lambda grant: grant.end_date)
    remaining = [grant.amount_usd for grant in ordered_grants]
    ordered_costs = sorted(
        (cost for cost in costs if cost.category == "OPENAI"), key=lambda cost: cost.cost_date
    )
    previous_day: date | None = None
    continuous_start = date.min
    for cost in ordered_costs:
        day = cost.cost_date
        if day == previous_day:
            raise CostDataInvalid("duplicate daily cost")
        if previous_day is None or day - previous_day != timedelta(days=1):
            continuous_start = day
        previous_day = day
        _nonnegative_decimal(cost.amount_usd, field="stored amount")
        uncovered = cost.amount_usd
        for index, grant in enumerate(ordered_grants):
            if remaining[index] == 0 or not grant.start_date <= day < grant.end_date:
                continue
            if grant.start_date < continuous_start:
                incomplete.add(day)
                continue
            applied = min(uncovered, remaining[index])
            remaining[index] -= applied
            uncovered -= applied
        deductions[day] = cost.amount_usd - uncovered
    return deductions, incomplete


def period_bounds(
    period: CostPeriod,
    *,
    today: date,
    stored_dates: tuple[date, ...],
) -> tuple[date, date]:
    if period == "today":
        return today, today
    if period == "week":
        return today - timedelta(days=6), today
    if period == "month":
        return today.replace(day=1), today
    return (min(stored_dates) if stored_dates else today), today


def _collection_windows(
    checkpoint: CostCheckpoint | None,
    *,
    today: date,
) -> tuple[tuple[date, date, bool], ...]:
    end_limit = today + timedelta(days=1)
    if checkpoint is not None and checkpoint.initial_complete:
        return ((today - timedelta(days=REFRESH_DAYS - 1), end_limit, True),)

    initial_start = today - timedelta(days=INITIAL_DAYS - 1)
    start = checkpoint.next_date if checkpoint is not None else initial_start
    if start < initial_start or start > end_limit:
        raise CostDataInvalid("cost checkpoint date is invalid")
    windows: list[tuple[date, date, bool]] = []
    while start < end_limit and len(windows) < MAX_INITIAL_WINDOWS:
        end = min(start + timedelta(days=WINDOW_DAYS), end_limit)
        windows.append((start, end, end == end_limit))
        start = end
    if not windows:
        windows.append((today - timedelta(days=REFRESH_DAYS - 1), end_limit, True))
    return tuple(windows)


def _validate_cost_window(
    costs: tuple[ProviderDailyCost, ...],
    *,
    source: Literal["AWS", "OPENAI"],
    start: date,
    end: date,
) -> None:
    expected_categories: tuple[CostCategory, ...] = (
        ("FARGATE", "LAMBDA", "OTHER_AWS") if source == "AWS" else ("OPENAI",)
    )
    expected = {(day, category) for day in _dates(start, end) for category in expected_categories}
    actual: set[tuple[date, CostCategory]] = set()
    for cost in costs:
        key = (cost.cost_date, cost.category)
        if key in actual or key not in expected:
            raise CostDataInvalid("provider cost window is incomplete")
        actual.add(key)
        _nonnegative_decimal(cost.amount_usd, field="provider amount")
        component_names: set[str] = set()
        component_total = Decimal(0)
        for name, value in cost.components:
            if not name or name in component_names:
                raise CostDataInvalid("provider components are invalid")
            component_names.add(name)
            component_total += _nonnegative_decimal(value, field="provider component")
        if cost.category == "OTHER_AWS":
            if not component_names or component_total != cost.amount_usd:
                raise CostDataInvalid("provider components are invalid")
        elif component_names:
            raise CostDataInvalid("provider components are invalid")
    if actual != expected:
        raise CostDataInvalid("provider cost window is incomplete")


def _validate_rate_window(
    rates: tuple[ProviderDailyRate, ...],
    *,
    start: date,
    end: date,
    allow_trailing_pending: bool = False,
) -> None:
    expected = set(_dates(start, end))
    actual: set[date] = set()
    for rate in rates:
        if rate.rate_date in actual or rate.rate_date not in expected:
            raise CostDataInvalid("provider exchange rate window is incomplete")
        actual.add(rate.rate_date)
        _positive_decimal(rate.usd_jpy, field="provider exchange rate")
    missing = expected - actual
    if missing and not (allow_trailing_pending and missing == {end - timedelta(days=1)}):
        raise CostDataInvalid("provider exchange rate window is incomplete")


def _dates(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=index) for index in range((end - start).days))


def _nonnegative_decimal(value: Decimal, *, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise CostDataInvalid(f"{field} is invalid")
    return value


def _positive_decimal(value: Decimal, *, field: str) -> Decimal:
    if _nonnegative_decimal(value, field=field) == 0:
        raise CostDataInvalid(f"{field} is invalid")
    return value


def _jpy_string(value: Decimal) -> str:
    return f"{value.quantize(JPY_QUANTUM, rounding=ROUND_HALF_EVEN):.6f}"


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CostDataInvalid("timestamp must be timezone-aware")
    return value.astimezone(UTC)
