"""OpenAI Responses API adapter with strict structured-output boundaries."""

from shittim_chest.adapters.openai.config import (
    OpenAIAdapterConfig,
    ParticipantProfile,
    ParticipantProfiles,
)
from shittim_chest.adapters.openai.errors import (
    OpenAIAdapterError,
    OpenAIConfigurationError,
    OpenAIIncompleteResponse,
    OpenAIInvalidOutput,
    OpenAIRateLimited,
    OpenAIRefusal,
    OpenAIUnavailable,
)
from shittim_chest.adapters.openai.evidence import OpenAIWebEvidenceService
from shittim_chest.adapters.openai.farewell import OpenAIFarewellGenerator
from shittim_chest.adapters.openai.limiter import OpenAIRequestLimiter
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
    "OpenAIFarewellGenerator",
    "OpenAIIncompleteResponse",
    "OpenAIInvalidOutput",
    "OpenAIRateLimited",
    "OpenAIRefusal",
    "OpenAIRequestLimiter",
    "OpenAIResponsesService",
    "OpenAIUnavailable",
    "OpenAIUsageRecord",
    "OpenAIUsageRecorder",
    "OpenAIWebEvidenceService",
    "ParticipantProfile",
    "ParticipantProfiles",
    "create_openai_client",
)
