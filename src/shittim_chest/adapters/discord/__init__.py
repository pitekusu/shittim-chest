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
from shittim_chest.adapters.discord.gateway import (
    DiscordClientSupervisor,
    DiscordPyGateway,
    build_discord_clients,
)
from shittim_chest.adapters.discord.interactions import DiscordInteractionController
from shittim_chest.adapters.discord.publisher import DiscordPyPublisher
from shittim_chest.adapters.discord.recovery import DiscordOutboxRecovery

__all__ = (
    "DiscordAdapterError",
    "DiscordClientSupervisor",
    "DiscordDeliveryConflict",
    "DiscordDeliveryRejected",
    "DiscordIdentityUnavailable",
    "DiscordInteractionController",
    "DiscordOutboxNotFound",
    "DiscordOutboxRecovery",
    "DiscordPermissionDenied",
    "DiscordPyGateway",
    "DiscordPyPublisher",
    "DiscordRateLimited",
    "DiscordThreadLocked",
    "DiscordThreadUnavailable",
    "DiscordUnavailable",
    "build_discord_clients",
)
