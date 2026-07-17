"""discord.py delivery adapter for persisted outbox operations."""

from shittim_chest.adapters.discord.errors import (
    DiscordAdapterError,
    DiscordDeliveryConflict,
    DiscordDeliveryRejected,
    DiscordIdentityUnavailable,
    DiscordOutboxNotFound,
    DiscordPermissionDenied,
    DiscordRateLimited,
    DiscordThreadLocked,
    DiscordThreadUnavailable,
    DiscordUnavailable,
)
from shittim_chest.adapters.discord.publisher import DiscordPyPublisher

__all__ = (
    "DiscordAdapterError",
    "DiscordDeliveryConflict",
    "DiscordDeliveryRejected",
    "DiscordIdentityUnavailable",
    "DiscordOutboxNotFound",
    "DiscordPermissionDenied",
    "DiscordPyPublisher",
    "DiscordRateLimited",
    "DiscordThreadLocked",
    "DiscordThreadUnavailable",
    "DiscordUnavailable",
)
