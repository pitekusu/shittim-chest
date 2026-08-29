"""Bounded translation of untrusted Amazon Inspector descriptions."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

INSPECTOR_TRANSLATION_MODEL = "gpt-5.6-luna"
INSPECTOR_SUMMARY_MIN_CHARS = 100
INSPECTOR_SUMMARY_MAX_CHARS = 300
INSPECTOR_TRANSLATION_BATCH_SIZE = 10
INSPECTOR_TRANSLATIONS_PER_RUN = 50
MAX_INSPECTOR_DESCRIPTION_CHARS = 1_024
MAX_INSPECTOR_TRANSLATION_SOURCES = 2_000
_VULNERABILITY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TRANSLATION_KEY_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class InspectorTranslationUnavailable(RuntimeError):
    """Stable translation failure without provider content or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class InspectorDescription:
    """One normalized, untrusted description identified without retaining an ARN."""

    key: str
    vulnerability_id: str
    source_sha256: str
    description: str

    def __post_init__(self) -> None:
        if _TRANSLATION_KEY_PATTERN.fullmatch(self.key) is None:
            raise ValueError("Inspector translation key is invalid")
        if _VULNERABILITY_ID_PATTERN.fullmatch(self.vulnerability_id) is None:
            raise ValueError("Inspector vulnerability identifier is invalid")
        if _TRANSLATION_KEY_PATTERN.fullmatch(self.source_sha256) is None:
            raise ValueError("Inspector source digest is invalid")
        if self.description != normalize_inspector_description(self.description):
            raise ValueError("Inspector description is not normalized")


@dataclass(frozen=True, slots=True)
class InspectorJapaneseSummary:
    """Validated Japanese summary cached without the provider description."""

    key: str
    vulnerability_id: str
    source_sha256: str
    summary_ja: str
    translated_at: datetime
    model: str = INSPECTOR_TRANSLATION_MODEL

    def __post_init__(self) -> None:
        if _TRANSLATION_KEY_PATTERN.fullmatch(self.key) is None:
            raise ValueError("Inspector translation key is invalid")
        if _VULNERABILITY_ID_PATTERN.fullmatch(self.vulnerability_id) is None:
            raise ValueError("Inspector vulnerability identifier is invalid")
        if _TRANSLATION_KEY_PATTERN.fullmatch(self.source_sha256) is None:
            raise ValueError("Inspector source digest is invalid")
        if self.summary_ja != normalize_inspector_summary(self.summary_ja):
            raise ValueError("Inspector Japanese summary is not normalized")
        if self.translated_at.tzinfo is None or self.translated_at.utcoffset() is None:
            raise ValueError("Inspector translation timestamp must be timezone-aware")
        if self.model != INSPECTOR_TRANSLATION_MODEL:
            raise ValueError("Inspector translation model is invalid")


@dataclass(frozen=True, slots=True)
class InspectorTranslationCollectionSummary:
    discovered: int
    cached: int
    translated: int
    remaining: int


class InspectorDescriptionSource(Protocol):
    def list_descriptions(self) -> tuple[InspectorDescription, ...]: ...


class InspectorSummaryTranslator(Protocol):
    def translate(
        self,
        descriptions: tuple[InspectorDescription, ...],
        *,
        translated_at: datetime,
    ) -> tuple[InspectorJapaneseSummary, ...]: ...


class InspectorTranslationStore(Protocol):
    def load(self, keys: tuple[str, ...]) -> Mapping[str, InspectorJapaneseSummary]: ...

    def save(self, summaries: tuple[InspectorJapaneseSummary, ...]) -> None: ...


class NullInspectorTranslationStore:
    """Read-only empty cache used by isolated status-source tests."""

    def load(self, keys: tuple[str, ...]) -> Mapping[str, InspectorJapaneseSummary]:
        del keys
        return {}

    def save(self, summaries: tuple[InspectorJapaneseSummary, ...]) -> None:
        if summaries:
            raise ValueError("Null Inspector translation store cannot save summaries")


class InspectorTranslationService:
    """Translate only unseen descriptions in deterministic bounded batches."""

    def __init__(
        self,
        *,
        source: InspectorDescriptionSource,
        translator: InspectorSummaryTranslator,
        store: InspectorTranslationStore,
    ) -> None:
        self._source = source
        self._translator = translator
        self._store = store

    def refresh(self, *, now: datetime) -> InspectorTranslationCollectionSummary:
        translated_at = _aware_utc(now)
        descriptions = self._source.list_descriptions()
        if len(descriptions) > MAX_INSPECTOR_TRANSLATION_SOURCES:
            raise InspectorTranslationUnavailable("source_limit_exceeded")
        keys = tuple(item.key for item in descriptions)
        if len(set(keys)) != len(keys):
            raise InspectorTranslationUnavailable("source_identity_conflict")
        cached = self._store.load(keys)
        if not set(cached) <= set(keys):
            raise InspectorTranslationUnavailable("cache_identity_invalid")
        for item in descriptions:
            existing = cached.get(item.key)
            if existing is not None and (
                existing.vulnerability_id != item.vulnerability_id
                or existing.source_sha256 != item.source_sha256
            ):
                raise InspectorTranslationUnavailable("cache_identity_invalid")

        missing = tuple(item for item in descriptions if item.key not in cached)
        selected = missing[:INSPECTOR_TRANSLATIONS_PER_RUN]
        translated = 0
        for offset in range(0, len(selected), INSPECTOR_TRANSLATION_BATCH_SIZE):
            batch = selected[offset : offset + INSPECTOR_TRANSLATION_BATCH_SIZE]
            summaries = self._translator.translate(batch, translated_at=translated_at)
            expected = {item.key for item in batch}
            if {summary.key for summary in summaries} != expected or len(summaries) != len(batch):
                raise InspectorTranslationUnavailable("provider_output_invalid")
            self._store.save(summaries)
            translated += len(summaries)
        return InspectorTranslationCollectionSummary(
            discovered=len(descriptions),
            cached=len(cached),
            translated=translated,
            remaining=len(missing) - translated,
        )


def inspector_description(
    *,
    vulnerability_id: str,
    description: object,
) -> InspectorDescription:
    """Normalize one provider description and derive content-free cache identities."""

    if _VULNERABILITY_ID_PATTERN.fullmatch(vulnerability_id) is None:
        raise ValueError("Inspector vulnerability identifier is invalid")
    normalized = normalize_inspector_description(description)
    source_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    key = hashlib.sha256(
        f"inspector-translation-v1\0{vulnerability_id}\0{normalized}".encode()
    ).hexdigest()
    return InspectorDescription(
        key=key,
        vulnerability_id=vulnerability_id,
        source_sha256=source_sha256,
        description=normalized,
    )


def normalize_inspector_description(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Inspector description is invalid")
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    ).strip()
    if not normalized or len(normalized) > MAX_INSPECTOR_DESCRIPTION_CHARS or "\x00" in normalized:
        raise ValueError("Inspector description is invalid")
    return normalized


def normalize_inspector_summary(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Inspector Japanese summary is invalid")
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    ).strip()
    if (
        not INSPECTOR_SUMMARY_MIN_CHARS <= len(normalized) <= INSPECTOR_SUMMARY_MAX_CHARS
        or not normalized
        or "\x00" in normalized
    ):
        raise ValueError("Inspector Japanese summary is invalid")
    return normalized


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("translation time must be timezone-aware")
    return value.astimezone(UTC)
