"""Stable application-boundary errors."""

from __future__ import annotations

from typing import ClassVar


class ApplicationError(Exception):
    """Base class for errors that adapters may map to user-safe messages."""

    code: ClassVar[str]


class GenerationProviderError(RuntimeError):
    """Content-free provider failure visible to generation orchestration."""

    __slots__ = ("code", "retryable")

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        if not code.strip():
            raise ValueError("generation provider error code must not be empty")
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class RuntimeNotReady(ApplicationError):
    """Raised when all required Discord identities are not ready."""

    code = "runtime_not_ready"


class RequiredEvidenceUnavailable(ApplicationError):
    """Raised when a current-fact question cannot obtain required evidence."""

    code = "required_evidence_unavailable"


class RequestNotAllowed(ApplicationError):
    """Raised when a request or actor is outside the configured policy."""

    code = "request_not_allowed"


class DebateNotFound(ApplicationError):
    """Raised when a debate identifier is not present."""

    code = "debate_not_found"


class InvalidApplicationOperation(ApplicationError):
    """Raised when a use case cannot run for the current state."""

    code = "invalid_application_operation"


class OutboxRecoveryFailed(ApplicationError):
    """Raised when a persisted Discord delivery cannot be recovered safely."""

    code = "outbox_recovery_failed"

    def __init__(self, delivery_code: str) -> None:
        if not delivery_code.strip():
            raise ValueError("delivery code must not be empty")
        self.delivery_code = delivery_code
        super().__init__("a persisted Discord delivery could not be recovered")


class OutboxRecoveryAbandoned(ApplicationError):
    """Raised when bounded delivery must converge through abandonment."""

    code = "outbox_recovery_abandoned"

    def __init__(self, reason: str, delivery_code: str | None = None) -> None:
        if not reason.strip():
            raise ValueError("outbox abandonment reason must not be empty")
        self.reason = reason
        self.delivery_code = delivery_code
        super().__init__("persisted Discord delivery reached its bounded stop condition")
