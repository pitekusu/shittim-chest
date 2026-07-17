"""Stable application-boundary errors."""

from __future__ import annotations

from typing import ClassVar


class ApplicationError(Exception):
    """Base class for errors that adapters may map to user-safe messages."""

    code: ClassVar[str]


class RuntimeNotReady(ApplicationError):
    """Raised when all required Discord identities are not ready."""

    code = "runtime_not_ready"


class RequestNotAllowed(ApplicationError):
    """Raised when a request or actor is outside the configured policy."""

    code = "request_not_allowed"


class DebateNotFound(ApplicationError):
    """Raised when a debate identifier is not present."""

    code = "debate_not_found"


class InvalidApplicationOperation(ApplicationError):
    """Raised when a use case cannot run for the current state."""

    code = "invalid_application_operation"
