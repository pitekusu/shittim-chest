"""Compatibility exports for records now owned by the application boundary."""

from shittim_chest.application.discord import (
    OutboxOperation,
    OutboxStatus,
    PanelOperation,
    PanelOperationKind,
)

__all__ = (
    "OutboxOperation",
    "OutboxStatus",
    "PanelOperation",
    "PanelOperationKind",
)
