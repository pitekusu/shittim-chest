"""Collect content-free OpenAI usage and failures for manual evaluation tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from shittim_chest.adapters.openai import OpenAIFailureRecord, OpenAIUsageRecord


@dataclass(slots=True)
class UsageCollector:
    usages: list[OpenAIUsageRecord] = field(default_factory=list)
    failures: list[OpenAIFailureRecord] = field(default_factory=list)

    def record_usage(self, record: OpenAIUsageRecord) -> None:
        self.usages.append(record)

    def record_failure(self, record: OpenAIFailureRecord) -> None:
        self.failures.append(record)
