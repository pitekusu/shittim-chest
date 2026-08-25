"""Read-only ADMIN status aggregation with a bounded in-memory cache."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from shittim_records.admin import AdminFailure
from shittim_records.contracts import (
    AdminStatusOverall,
    AdminStatusResponse,
    AdminStatusSection,
)

STATUS_CACHE_TTL = timedelta(seconds=60)
STATUS_SERVICES = (
    "ecs",
    "ecr",
    "inspector",
    "s3",
    "dynamodb",
    "lambda",
    "cloudfront",
    "sqs",
)


@dataclass(frozen=True, slots=True)
class AdminStatusCollection:
    overall: AdminStatusOverall
    sections: tuple[AdminStatusSection, ...]


class AdminStatusSource(Protocol):
    def collect(self, *, now: datetime) -> AdminStatusCollection: ...


class AdminStatusService:
    """Reuse one sanitized snapshot for 60 seconds inside a warm Lambda process."""

    def __init__(self, source: AdminStatusSource) -> None:
        self._source = source
        self._cached: AdminStatusResponse | None = None

    def get(self, *, now: datetime) -> AdminStatusResponse:
        now = _utc(now)
        if self._cached is None:
            return self._collect(now=now)
        return self._with_staleness(self._cached, now=now)

    def refresh(self, *, now: datetime) -> AdminStatusResponse:
        now = _utc(now)
        if self._cached is not None and now < self._cached.expires_at:
            return self._with_staleness(self._cached, now=now)
        return self._collect(now=now)

    def _collect(self, *, now: datetime) -> AdminStatusResponse:
        try:
            collection = self._source.collect(now=now)
        except AdminFailure:
            raise
        except Exception as error:
            raise AdminFailure("ADMIN_STATUS_UNAVAILABLE", 503) from error
        services = tuple(section.service for section in collection.sections)
        if services != STATUS_SERVICES:
            raise AdminFailure("ADMIN_STATUS_INVALID", 503)
        response = AdminStatusResponse(
            schema_version=1,
            generated_at=now,
            expires_at=now + STATUS_CACHE_TTL,
            stale=False,
            overall=collection.overall,
            sections=collection.sections,
        )
        self._cached = response
        return response

    @staticmethod
    def _with_staleness(value: AdminStatusResponse, *, now: datetime) -> AdminStatusResponse:
        return value.model_copy(update={"stale": now >= value.expires_at})


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)
