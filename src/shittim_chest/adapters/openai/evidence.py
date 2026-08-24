"""Responses API web-search evidence adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from urllib.parse import urlsplit

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from openai.types.responses.parsed_response import ParsedResponse
from openai.types.responses.response_function_web_search import (
    ActionFind,
    ActionOpenPage,
    ActionSearch,
    ActionSearchSource,
    ResponseFunctionWebSearch,
)
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_refusal import ResponseOutputRefusal
from openai.types.responses.response_output_text import AnnotationURLCitation, ResponseOutputText
from openai.types.responses.response_reasoning_item import ResponseReasoningItem
from pydantic import ValidationError

from shittim_chest.adapters.openai.config import OpenAIAdapterConfig
from shittim_chest.adapters.openai.errors import (
    OpenAIAdapterError,
    OpenAIConfigurationError,
    OpenAIIncompleteResponse,
    OpenAIInvalidOutput,
    OpenAIRateLimited,
    OpenAIRefusal,
    OpenAIUnavailable,
)
from shittim_chest.adapters.openai.limiter import OpenAIRequestLimiter
from shittim_chest.adapters.openai.observability import (
    NullOpenAIUsageRecorder,
    OpenAIFailureRecord,
    OpenAIUsageRecord,
    OpenAIUsageRecorder,
)
from shittim_chest.adapters.openai.prompts import evidence_instructions
from shittim_chest.adapters.openai.schemas import EvidenceDigestOutputV2
from shittim_chest.domain import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceSearchStatus,
    SearchRequirement,
)


class _EvidenceStage(StrEnum):
    PROVIDER_RESPONSE = "provider_response"
    OUTPUT_VALIDATION = "output_validation"
    SOURCE_EXTRACTION = "source_extraction"
    BUNDLE_VALIDATION = "bundle_validation"
    USAGE_RECORDING = "usage_recording"


class _SourceDiagnosticContext(StrEnum):
    RESPONSE_OUTPUT = "response_output"
    WEB_SEARCH_ACTION = "web_search_action"
    WEB_SEARCH_SOURCES = "web_search_sources"
    WEB_SEARCH_SOURCE = "web_search_source"
    WEB_SEARCH_SOURCE_TYPE = "web_search_source_type"
    WEB_SEARCH_SOURCE_URL = "web_search_source_url"
    WEB_SEARCH_SOURCE_PROVIDER = "web_search_source_provider"
    WEB_SEARCH_SOURCE_SHAPE = "web_search_source_shape"
    MESSAGE_CONTENT = "message_content"
    OUTPUT_TEXT_ANNOTATIONS = "output_text_annotations"
    OUTPUT_TEXT_ANNOTATION = "output_text_annotation"
    URL_CITATION_URL = "url_citation_url"
    EVIDENCE_SOURCES = "evidence_sources"
    UNEXPECTED_EXCEPTION = "unexpected_exception"


class _DiagnosticKind(StrEnum):
    MISSING = "missing"
    NULL = "null"
    EMPTY_STRING = "empty_string"
    STRING = "string"
    BOOLEAN = "boolean"
    NUMBER = "number"
    ARRAY = "array"
    OBJECT = "object"
    OTHER = "other"
    ATTRIBUTE_ERROR = "attribute_error"
    KEY_ERROR = "key_error"
    RUNTIME_ERROR = "runtime_error"
    TYPE_ERROR = "type_error"
    VALUE_ERROR = "value_error"


_MISSING = object()
_ROUTER_RULES_VERSION = "agentic-search-v1"
_SEARCH_SKIPPED_REASON = "model_skipped_search"
_SEARCH_SELECTED_REASON = "model_selected_search"
_SEARCH_UNAVAILABLE_REASON = "agentic_search_unavailable"

_REALTIME_FEEDS = {
    "oai-finance": (
        "openai://web-search/oai-finance",
        "OpenAI real-time finance feed",
        "finance",
    ),
    "oai-sports": (
        "openai://web-search/oai-sports",
        "OpenAI real-time sports feed",
        "sports",
    ),
    "oai-weather": (
        "openai://web-search/oai-weather",
        "OpenAI real-time weather feed",
        "weather",
    ),
}


class _SourceExtractionError(ValueError):
    """Content-free classification for a malformed provider source field."""

    __slots__ = ("context", "kind")

    def __init__(self, context: _SourceDiagnosticContext, value: object) -> None:
        self.context = context
        self.kind = _diagnostic_kind(value)
        super().__init__(f"invalid provider source field: {context.value}/{self.kind.value}")


@dataclass(frozen=True, slots=True)
class _CitationExtraction:
    titles: dict[str, str]
    count: int
    title_fallback_kinds: tuple[_DiagnosticKind, ...]


@dataclass(frozen=True, slots=True)
class _SearchSourceExtraction:
    urls: tuple[str, ...]
    realtime_feeds: tuple[str, ...]
    rejected_url_kinds: tuple[_DiagnosticKind, ...]


@dataclass(frozen=True, slots=True)
class _SourceExtraction:
    items: tuple[EvidenceItem, ...]
    web_search_source_count: int
    web_search_source_rejected_kinds: tuple[_DiagnosticKind, ...]
    realtime_feed_kinds: tuple[str, ...]
    url_citation_count: int
    title_fallback_kinds: tuple[_DiagnosticKind, ...]


@dataclass(slots=True)
class OpenAIWebEvidenceService:
    """Prepare one immutable source-backed evidence bundle per debate."""

    client: AsyncOpenAI
    limiter: OpenAIRequestLimiter
    config: OpenAIAdapterConfig = field(default_factory=OpenAIAdapterConfig)
    recorder: OpenAIUsageRecorder = field(default_factory=NullOpenAIUsageRecorder)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    system_prompt: str | None = field(default=None, repr=False)
    moderator_prompt: str | None = field(default=None, repr=False)

    async def prepare_evidence(self, *, question: str) -> EvidenceBundle:
        try:
            return await self._search(question)
        except asyncio.CancelledError:
            raise
        except OpenAIAdapterError:
            return EvidenceBundle(
                required_search_satisfied=False,
                search_requirement=SearchRequirement.OPTIONAL,
                search_status=EvidenceSearchStatus.OPTIONAL_UNAVAILABLE,
                router_rules_version=_ROUTER_RULES_VERSION,
                routing_reason=_SEARCH_UNAVAILABLE_REASON,
            )

    async def _search(
        self,
        question: str,
    ) -> EvidenceBundle:
        started = monotonic()
        operation = "evidence_search"
        stage = _EvidenceStage.PROVIDER_RESPONSE
        try:
            async with self.limiter.slot():
                response = await self.client.responses.parse(
                    model=self.config.model,
                    instructions=evidence_instructions(
                        system_prompt=self.system_prompt,
                        moderator_prompt=self.moderator_prompt,
                    ),
                    input=json.dumps(
                        {"question": question},
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    text_format=EvidenceDigestOutputV2,
                    include=["web_search_call.action.sources"],
                    max_output_tokens=1_200,
                    reasoning={"effort": "medium"},
                    store=False,
                    tools=[{"type": "web_search", "search_context_size": "medium"}],
                    tool_choice="auto",
                    max_tool_calls=4,
                    parallel_tool_calls=False,
                    truncation="disabled",
                )
            stage = _EvidenceStage.OUTPUT_VALIDATION
            parsed, used_web_search = _validated_output(response)
            if not used_web_search:
                extraction = _empty_extraction()
                self.recorder.record_usage(
                    _usage_record(operation, response, extraction, started, self.config)
                )
                return EvidenceBundle(
                    router_rules_version=_ROUTER_RULES_VERSION,
                    routing_reason=_SEARCH_SKIPPED_REASON,
                )
            stage = _EvidenceStage.SOURCE_EXTRACTION
            try:
                extraction = _extract_sources(response, self.clock())
            except _SourceExtractionError as error:
                raise OpenAIInvalidOutput(
                    diagnostic_context=error.context.value,
                    diagnostic_kind=error.kind.value,
                ) from error
            except ValueError as error:
                raise OpenAIInvalidOutput() from error
            if not extraction.items:
                raise OpenAIInvalidOutput(
                    diagnostic_context=_SourceDiagnosticContext.EVIDENCE_SOURCES.value,
                    diagnostic_kind=_DiagnosticKind.MISSING.value,
                )
            if not parsed.summary.strip():
                raise OpenAIInvalidOutput(
                    diagnostic_context=_SourceDiagnosticContext.RESPONSE_OUTPUT.value,
                    diagnostic_kind=_DiagnosticKind.EMPTY_STRING.value,
                )
            stage = _EvidenceStage.BUNDLE_VALIDATION
            try:
                bundle = EvidenceBundle(
                    items=extraction.items,
                    summary=parsed.summary,
                    search_requirement=SearchRequirement.OPTIONAL,
                    search_status=EvidenceSearchStatus.COMPLETED,
                    search_response_id=response.id,
                    router_rules_version=_ROUTER_RULES_VERSION,
                    routing_reason=_SEARCH_SELECTED_REASON,
                )
            except ValueError as error:
                raise OpenAIInvalidOutput() from error
            stage = _EvidenceStage.USAGE_RECORDING
            self.recorder.record_usage(
                _usage_record(operation, response, extraction, started, self.config)
            )
        except asyncio.CancelledError:
            raise
        except ValidationError as error:
            invalid_output = OpenAIInvalidOutput()
            self._record_failure(operation, stage, invalid_output, started)
            raise invalid_output from error
        except RateLimitError as error:
            rate_limited = OpenAIRateLimited()
            self._record_failure(operation, stage, rate_limited, started)
            raise rate_limited from error
        except (AuthenticationError, PermissionDeniedError, NotFoundError) as error:
            configuration_error = OpenAIConfigurationError()
            self._record_failure(operation, stage, configuration_error, started)
            raise configuration_error from error
        except (APIConnectionError, APITimeoutError) as error:
            unavailable = OpenAIUnavailable()
            self._record_failure(operation, stage, unavailable, started)
            raise unavailable from error
        except APIStatusError as error:
            status_error: OpenAIAdapterError = (
                OpenAIUnavailable() if error.status_code >= 500 else OpenAIConfigurationError()
            )
            self._record_failure(operation, stage, status_error, started)
            raise status_error from error
        except OpenAIAdapterError as error:
            self._record_failure(operation, stage, error, started)
            raise
        except Exception as error:
            self._record_unclassified(operation, stage, started, error)
            raise
        return bundle

    def _record_failure(
        self,
        operation: str,
        stage: _EvidenceStage,
        error: OpenAIAdapterError,
        started: float,
    ) -> None:
        self.recorder.record_failure(
            OpenAIFailureRecord(
                _stage_operation(operation, stage),
                error.code,
                self.config.policy.policy_id.value,
                int((monotonic() - started) * 1000),
                error.diagnostic_context,
                error.diagnostic_kind,
            )
        )

    def _record_unclassified(
        self,
        operation: str,
        stage: _EvidenceStage,
        started: float,
        error: Exception,
    ) -> None:
        self.recorder.record_failure(
            OpenAIFailureRecord(
                _stage_operation(operation, stage),
                "openai_unclassified",
                self.config.policy.policy_id.value,
                int((monotonic() - started) * 1000),
                _SourceDiagnosticContext.UNEXPECTED_EXCEPTION.value,
                _unexpected_exception_kind(error).value,
            )
        )


def _stage_operation(operation: str, stage: _EvidenceStage) -> str:
    return f"{operation}.{stage.value}"


def _validated_output(
    response: ParsedResponse[EvidenceDigestOutputV2],
) -> tuple[EvidenceDigestOutputV2, bool]:
    if response.status != "completed":
        reason = response.status
        if response.status == "incomplete":
            reason = (
                response.incomplete_details.reason
                if response.incomplete_details is not None
                and response.incomplete_details.reason is not None
                else "missing"
            )
        raise OpenAIIncompleteResponse(
            diagnostic_context="response_status",
            diagnostic_kind=reason,
        )
    outputs = getattr(response, "output", _MISSING)
    if not isinstance(outputs, list):
        raise OpenAIInvalidOutput(
            diagnostic_context=_SourceDiagnosticContext.RESPONSE_OUTPUT.value,
            diagnostic_kind=_diagnostic_kind(outputs).value,
        )
    message_seen = False
    used_web_search = False
    for output in outputs:
        if isinstance(output, ResponseFunctionWebSearch):
            used_web_search = True
            if output.status != "completed":
                raise OpenAIIncompleteResponse(
                    diagnostic_context="web_search_status",
                    diagnostic_kind=output.status,
                )
            continue
        if isinstance(output, ResponseReasoningItem):
            continue
        if not isinstance(output, ResponseOutputMessage):
            raise OpenAIInvalidOutput(
                diagnostic_context=_SourceDiagnosticContext.RESPONSE_OUTPUT.value,
                diagnostic_kind=_DiagnosticKind.OTHER.value,
            )
        message_seen = True
        if output.status != "completed":
            raise OpenAIIncompleteResponse(
                diagnostic_context="message_status",
                diagnostic_kind=output.status,
            )
        for content in output.content:
            if isinstance(content, ResponseOutputRefusal):
                raise OpenAIRefusal()
            if not isinstance(content, ResponseOutputText):
                raise OpenAIInvalidOutput(
                    diagnostic_context=_SourceDiagnosticContext.MESSAGE_CONTENT.value,
                    diagnostic_kind=_DiagnosticKind.OTHER.value,
                )
    if not message_seen:
        raise OpenAIIncompleteResponse(
            diagnostic_context="message_status",
            diagnostic_kind="missing",
        )
    if response.output_parsed is None:
        raise OpenAIInvalidOutput()
    return response.output_parsed, used_web_search


def _empty_extraction() -> _SourceExtraction:
    return _SourceExtraction((), 0, (), (), 0, ())


def _extract_sources(
    response: ParsedResponse[EvidenceDigestOutputV2],
    retrieved_at: datetime,
) -> _SourceExtraction:
    titles: dict[str, str] = {}
    source_urls: list[str] = []
    realtime_feeds: dict[str, None] = {}
    rejected_source_kinds: list[_DiagnosticKind] = []
    citation_count = 0
    title_fallback_kinds: list[_DiagnosticKind] = []
    outputs = getattr(response, "output", _MISSING)
    if not isinstance(outputs, list):
        raise _SourceExtractionError(_SourceDiagnosticContext.RESPONSE_OUTPUT, outputs)
    for output in outputs:
        if isinstance(output, ResponseFunctionWebSearch):
            search_sources = _search_sources(output)
            source_urls.extend(search_sources.urls)
            realtime_feeds.update(dict.fromkeys(search_sources.realtime_feeds))
            rejected_source_kinds.extend(search_sources.rejected_url_kinds)
        if isinstance(output, ResponseOutputMessage):
            citations = _message_citation_titles(output)
            titles.update(citations.titles)
            citation_count += citations.count
            title_fallback_kinds.extend(citations.title_fallback_kinds)
    timestamp = retrieved_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    items: list[EvidenceItem] = []
    # URL citations remain the canonical references for web pages. The Responses
    # API exposes its fixed real-time feeds only through action.sources, so those
    # exact allowlisted identities are represented by stable non-HTTP URIs.
    for url in titles:
        title = titles.get(url, url)
        metadata = json.dumps(
            {"source_type": "url", "title": title, "url": url},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        items.append(
            EvidenceItem(
                source_url=url,
                title=title,
                source_metadata=metadata,
                retrieved_at=timestamp,
                content_hash=hashlib.sha256(metadata.encode()).hexdigest(),
            )
        )
    for provider in realtime_feeds:
        source_uri, title, _ = _REALTIME_FEEDS[provider]
        metadata = json.dumps(
            {"provider": provider, "source_type": "api", "source_uri": source_uri},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        items.append(
            EvidenceItem(
                source_url=source_uri,
                title=title,
                source_metadata=metadata,
                retrieved_at=timestamp,
                content_hash=hashlib.sha256(metadata.encode()).hexdigest(),
            )
        )
    return _SourceExtraction(
        items=tuple(items),
        web_search_source_count=len(source_urls) + len(realtime_feeds),
        web_search_source_rejected_kinds=tuple(rejected_source_kinds),
        realtime_feed_kinds=tuple(_REALTIME_FEEDS[name][2] for name in realtime_feeds),
        url_citation_count=citation_count,
        title_fallback_kinds=tuple(title_fallback_kinds),
    )


def _search_sources(output: ResponseFunctionWebSearch) -> _SearchSourceExtraction:
    action = getattr(output, "action", _MISSING)
    if isinstance(action, (ActionOpenPage, ActionFind)):
        return _SearchSourceExtraction((), (), ())
    if not isinstance(action, ActionSearch):
        raise _SourceExtractionError(_SourceDiagnosticContext.WEB_SEARCH_ACTION, action)
    sources = getattr(action, "sources", _MISSING)
    if sources is _MISSING or sources is None:
        return _SearchSourceExtraction((), (), ())
    if not isinstance(sources, list):
        raise _SourceExtractionError(_SourceDiagnosticContext.WEB_SEARCH_SOURCES, sources)
    urls: list[str] = []
    realtime_feeds: list[str] = []
    rejected_url_kinds: list[_DiagnosticKind] = []
    for source in sources:
        if not isinstance(source, ActionSearchSource):
            raise _SourceExtractionError(_SourceDiagnosticContext.WEB_SEARCH_SOURCE, source)
        source_type = getattr(source, "type", _MISSING)
        extras = source.model_extra or {}
        if source_type == "api":
            if set(extras) != {"name"}:
                raise _SourceExtractionError(
                    _SourceDiagnosticContext.WEB_SEARCH_SOURCE_SHAPE,
                    extras,
                )
            provider = extras["name"]
            if not isinstance(provider, str) or provider not in _REALTIME_FEEDS:
                raise _SourceExtractionError(
                    _SourceDiagnosticContext.WEB_SEARCH_SOURCE_PROVIDER,
                    provider,
                )
            if getattr(source, "url", None) is not None:
                raise _SourceExtractionError(
                    _SourceDiagnosticContext.WEB_SEARCH_SOURCE_SHAPE,
                    getattr(source, "url", None),
                )
            realtime_feeds.append(provider)
            continue
        if source_type != "url":
            raise _SourceExtractionError(
                _SourceDiagnosticContext.WEB_SEARCH_SOURCE_TYPE,
                source_type,
            )
        if extras:
            raise _SourceExtractionError(
                _SourceDiagnosticContext.WEB_SEARCH_SOURCE_SHAPE,
                extras,
            )
        value = getattr(source, "url", _MISSING)
        if not isinstance(value, str) or not value.strip():
            rejected_url_kinds.append(_diagnostic_kind(value))
            continue
        urls.append(value)
    return _SearchSourceExtraction(
        tuple(urls),
        tuple(realtime_feeds),
        tuple(rejected_url_kinds),
    )


def _message_citation_titles(output: ResponseOutputMessage) -> _CitationExtraction:
    content_items = getattr(output, "content", _MISSING)
    if not isinstance(content_items, list):
        raise _SourceExtractionError(_SourceDiagnosticContext.MESSAGE_CONTENT, content_items)
    titles: dict[str, str] = {}
    count = 0
    title_fallback_kinds: list[_DiagnosticKind] = []
    for content in content_items:
        if not isinstance(content, ResponseOutputText):
            continue
        annotations = getattr(content, "annotations", _MISSING)
        if not isinstance(annotations, list):
            raise _SourceExtractionError(
                _SourceDiagnosticContext.OUTPUT_TEXT_ANNOTATIONS,
                annotations,
            )
        for annotation in annotations:
            if not isinstance(annotation, AnnotationURLCitation):
                raise _SourceExtractionError(
                    _SourceDiagnosticContext.OUTPUT_TEXT_ANNOTATION,
                    annotation,
                )
            count += 1
            url = _required_source_url(
                getattr(annotation, "url", _MISSING),
                _SourceDiagnosticContext.URL_CITATION_URL,
            )
            title, fallback_kind = _source_title(
                getattr(annotation, "title", _MISSING),
                url,
            )
            titles[url] = title
            if fallback_kind is not None:
                title_fallback_kinds.append(fallback_kind)
    return _CitationExtraction(titles, count, tuple(title_fallback_kinds))


def _required_source_url(value: object, context: _SourceDiagnosticContext) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _SourceExtractionError(context, value)
    normalized = value.strip()
    try:
        parsed = urlsplit(normalized)
        _ = parsed.port
    except ValueError as error:
        raise _SourceExtractionError(context, value) from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or any(character.isspace() for character in normalized)
    ):
        raise _SourceExtractionError(context, value)
    return normalized


def _source_title(value: object, fallback_url: str) -> tuple[str, _DiagnosticKind | None]:
    if isinstance(value, str) and value.strip():
        return value, None
    return fallback_url, _diagnostic_kind(value)


def _diagnostic_kind(value: object) -> _DiagnosticKind:
    if value is _MISSING:
        return _DiagnosticKind.MISSING
    if value is None:
        return _DiagnosticKind.NULL
    if isinstance(value, str):
        return _DiagnosticKind.EMPTY_STRING if not value.strip() else _DiagnosticKind.STRING
    if isinstance(value, bool):
        return _DiagnosticKind.BOOLEAN
    if isinstance(value, (int, float)):
        return _DiagnosticKind.NUMBER
    if isinstance(value, (list, tuple)):
        return _DiagnosticKind.ARRAY
    if isinstance(value, dict):
        return _DiagnosticKind.OBJECT
    return _DiagnosticKind.OTHER


def _unexpected_exception_kind(error: Exception) -> _DiagnosticKind:
    kinds = {
        AttributeError: _DiagnosticKind.ATTRIBUTE_ERROR,
        KeyError: _DiagnosticKind.KEY_ERROR,
        RuntimeError: _DiagnosticKind.RUNTIME_ERROR,
        TypeError: _DiagnosticKind.TYPE_ERROR,
        ValueError: _DiagnosticKind.VALUE_ERROR,
    }
    return kinds.get(type(error), _DiagnosticKind.OTHER)


def _usage_record(
    operation: str,
    response: ParsedResponse[EvidenceDigestOutputV2],
    extraction: _SourceExtraction,
    started: float,
    config: OpenAIAdapterConfig,
) -> OpenAIUsageRecord:
    usage = response.usage
    return OpenAIUsageRecord(
        operation=operation,
        response_id=response.id,
        model=response.model,
        policy_id=config.policy.policy_id.value,
        reasoning_mode=config.policy.reasoning_mode.value,
        latency_ms=int((monotonic() - started) * 1000),
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        cached_input_tokens=(usage.input_tokens_details.cached_tokens if usage else 0),
        reasoning_tokens=(usage.output_tokens_details.reasoning_tokens if usage else 0),
        web_search_source_count=extraction.web_search_source_count,
        web_search_source_rejected_count=len(extraction.web_search_source_rejected_kinds),
        web_search_source_rejected_kinds=(
            ",".join(sorted({kind.value for kind in extraction.web_search_source_rejected_kinds}))
            or None
        ),
        realtime_feed_count=len(extraction.realtime_feed_kinds),
        realtime_feed_kinds=(",".join(extraction.realtime_feed_kinds) or None),
        url_citation_count=extraction.url_citation_count,
        evidence_source_count=len(extraction.items),
        title_fallback_count=len(extraction.title_fallback_kinds),
        title_fallback_kinds=(
            ",".join(sorted({kind.value for kind in extraction.title_fallback_kinds})) or None
        ),
    )
