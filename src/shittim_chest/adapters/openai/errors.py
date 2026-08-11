"""Stable adapter errors that never expose raw provider output."""

from __future__ import annotations

from shittim_chest.application.errors import GenerationProviderError


class OpenAIAdapterError(GenerationProviderError):
    """Base class for an OpenAI boundary failure."""

    __slots__ = ("diagnostic_context", "diagnostic_kind")

    code: str
    diagnostic_context: str | None
    diagnostic_kind: str | None
    retryable: bool

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        diagnostic_context: str | None = None,
        diagnostic_kind: str | None = None,
    ) -> None:
        self.diagnostic_context = diagnostic_context
        self.diagnostic_kind = diagnostic_kind
        super().__init__(code, message, retryable=retryable)


class OpenAIRefusal(OpenAIAdapterError):
    """The provider explicitly refused to generate the requested output."""

    def __init__(self) -> None:
        super().__init__("openai_refusal", "the model refused the request", retryable=False)


class OpenAIIncompleteResponse(OpenAIAdapterError):
    """The provider stopped before producing a complete structured output."""

    def __init__(
        self,
        *,
        diagnostic_context: str | None = None,
        diagnostic_kind: str | None = None,
    ) -> None:
        super().__init__(
            "openai_incomplete",
            "the model response was incomplete",
            retryable=False,
            diagnostic_context=diagnostic_context,
            diagnostic_kind=diagnostic_kind,
        )


class OpenAIInvalidOutput(OpenAIAdapterError):
    """The response completed without one valid structured result."""

    def __init__(
        self,
        *,
        diagnostic_context: str | None = None,
        diagnostic_kind: str | None = None,
    ) -> None:
        super().__init__(
            "openai_invalid_output",
            "the model returned no valid structured output",
            retryable=False,
            diagnostic_context=diagnostic_context,
            diagnostic_kind=diagnostic_kind,
        )


class OpenAIRateLimited(OpenAIAdapterError):
    """The SDK exhausted its bounded retry policy after rate limiting."""

    def __init__(self) -> None:
        super().__init__("openai_rate_limited", "OpenAI rate limit exceeded", retryable=True)


class OpenAIUnavailable(OpenAIAdapterError):
    """The SDK exhausted retries for a transient transport or server failure."""

    def __init__(self) -> None:
        super().__init__("openai_unavailable", "OpenAI is temporarily unavailable", retryable=True)


class OpenAIConfigurationError(OpenAIAdapterError):
    """Authentication, authorization, or model configuration is invalid."""

    def __init__(self) -> None:
        super().__init__(
            "openai_configuration",
            "OpenAI authentication or model configuration failed",
            retryable=False,
        )
