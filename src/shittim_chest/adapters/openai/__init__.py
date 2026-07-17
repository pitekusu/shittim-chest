"""OpenAI Responses API adapter with strict structured-output boundaries."""

from shittim_chest.adapters.openai.config import OpenAIAdapterConfig, PersonaPrompts
from shittim_chest.adapters.openai.errors import (
    OpenAIAdapterError,
    OpenAIConfigurationError,
    OpenAIIncompleteResponse,
    OpenAIInvalidOutput,
    OpenAIRateLimited,
    OpenAIRefusal,
    OpenAIUnavailable,
)
from shittim_chest.adapters.openai.observability import (
    NullOpenAIUsageRecorder,
    OpenAIFailureRecord,
    OpenAIUsageRecord,
    OpenAIUsageRecorder,
)
from shittim_chest.adapters.openai.service import OpenAIResponsesService, create_openai_client

__all__ = (
    "NullOpenAIUsageRecorder",
    "OpenAIAdapterConfig",
    "OpenAIAdapterError",
    "OpenAIConfigurationError",
    "OpenAIFailureRecord",
    "OpenAIIncompleteResponse",
    "OpenAIInvalidOutput",
    "OpenAIRateLimited",
    "OpenAIRefusal",
    "OpenAIResponsesService",
    "OpenAIUnavailable",
    "OpenAIUsageRecord",
    "OpenAIUsageRecorder",
    "PersonaPrompts",
    "create_openai_client",
)
