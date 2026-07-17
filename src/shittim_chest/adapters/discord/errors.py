"""Stable Discord adapter errors without provider response content."""

from __future__ import annotations

from shittim_chest.application.discord import DiscordErrorCode


class DiscordAdapterError(RuntimeError):
    """Base class for a Discord delivery-boundary failure."""

    __slots__ = ("code", "retryable")

    code: str
    retryable: bool

    def __init__(self, code: DiscordErrorCode, message: str, *, retryable: bool) -> None:
        self.code = code.value
        self.retryable = retryable
        super().__init__(message)


class DiscordIdentityUnavailable(DiscordAdapterError):
    """The configured Bot identity is not ready for REST delivery."""

    def __init__(self) -> None:
        super().__init__(
            DiscordErrorCode.BOTS_NOT_READY,
            "the configured Discord identity is not ready",
            retryable=True,
        )


class DiscordOutboxNotFound(DiscordAdapterError):
    """The requested operation was not persisted before publication."""

    def __init__(self) -> None:
        super().__init__(
            DiscordErrorCode.OUTBOX_NOT_FOUND,
            "the persisted Discord outbox operation does not exist",
            retryable=False,
        )


class DiscordThreadUnavailable(DiscordAdapterError):
    """The persisted thread cannot be resolved or crossed a Guild boundary."""

    def __init__(self) -> None:
        super().__init__(
            DiscordErrorCode.THREAD_UNAVAILABLE,
            "the persisted Discord thread is unavailable",
            retryable=False,
        )


class DiscordThreadLocked(DiscordAdapterError):
    """The thread is locked and must not be unlocked automatically."""

    def __init__(self) -> None:
        super().__init__(
            DiscordErrorCode.THREAD_LOCKED,
            "the Discord thread is locked",
            retryable=False,
        )


class DiscordPermissionDenied(DiscordAdapterError):
    """The Bot lacks a permission required to publish or reconcile."""

    def __init__(self) -> None:
        super().__init__(
            DiscordErrorCode.PERMISSION_DENIED,
            "Discord denied a required permission",
            retryable=False,
        )


class DiscordDeliveryConflict(DiscordAdapterError):
    """Discord returned or stored a message inconsistent with the outbox record."""

    def __init__(self) -> None:
        super().__init__(
            DiscordErrorCode.OUTBOX_CONFLICT,
            "Discord delivery conflicts with the persisted outbox operation",
            retryable=False,
        )


class DiscordRateLimited(DiscordAdapterError):
    """discord.py exhausted its bounded Retry-After handling."""

    def __init__(self) -> None:
        super().__init__(
            DiscordErrorCode.RATE_LIMITED,
            "Discord rate limit handling was exhausted",
            retryable=True,
        )


class DiscordUnavailable(DiscordAdapterError):
    """Discord remained unavailable after the SDK transport policy."""

    def __init__(self) -> None:
        super().__init__(
            DiscordErrorCode.UNAVAILABLE,
            "Discord is temporarily unavailable",
            retryable=True,
        )


class DiscordDeliveryRejected(DiscordAdapterError):
    """Discord rejected a non-retryable delivery request."""

    def __init__(self) -> None:
        super().__init__(
            DiscordErrorCode.DELIVERY_REJECTED,
            "Discord rejected the delivery request",
            retryable=False,
        )
