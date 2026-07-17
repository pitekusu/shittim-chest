"""Stable adapter errors that never expose raw provider output."""

from __future__ import annotations


class OpenAIAdapterError(RuntimeError):
    """Base class for an OpenAI boundary failure."""

    __slots__ = ("code", "retryable")

    code: str
    retryable: bool

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class OpenAIRefusal(OpenAIAdapterError):
    """The provider explicitly refused to generate the requested output."""

    def __init__(self) -> None:
        super().__init__("openai_refusal", "the model refused the request", retryable=False)


class OpenAIIncompleteResponse(OpenAIAdapterError):
    """The provider stopped before producing a complete structured output."""

    def __init__(self) -> None:
        super().__init__("openai_incomplete", "the model response was incomplete", retryable=False)


class OpenAIInvalidOutput(OpenAIAdapterError):
    """The response completed without one valid structured result."""

    def __init__(self) -> None:
        super().__init__(
            "openai_invalid_output",
            "the model returned no valid structured output",
            retryable=False,
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
