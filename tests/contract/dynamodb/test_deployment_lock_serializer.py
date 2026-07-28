"""Deployment lock serialization rejects drift and preserves fenced ownership."""

from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.adapters.dynamodb.serializer import (
    PersistenceFormatError,
    deserialize_deployment_lock,
    serialize_deployment_lock,
)
from shittim_chest.application.deployment_guard import (
    BreakGlassReason,
    DeploymentLock,
    DeploymentLockState,
    DeploymentMode,
)

NOW = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)


def test_open_and_owned_lock_round_trip() -> None:
    opened = DeploymentLock.open(at=NOW)
    locked = DeploymentLock(
        state=DeploymentLockState.LOCKED,
        fencing_token=4,
        version=8,
        updated_at=NOW,
        guard_id="019d2c1f-0000-7000-8000-a00000000003",
        owner="pitekusu",
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        mode=DeploymentMode.BREAK_GLASS,
        reason=BreakGlassReason.INCIDENT_RESPONSE,
    )

    assert deserialize_deployment_lock(serialize_deployment_lock(opened)) == opened
    assert deserialize_deployment_lock(serialize_deployment_lock(locked)) == locked


def test_lock_deserializer_rejects_unknown_attributes_and_partial_ownership() -> None:
    with pytest.raises(PersistenceFormatError, match="unknown attributes"):
        deserialize_deployment_lock(
            {**serialize_deployment_lock(DeploymentLock.open(at=NOW)), "unexpected": "value"}
        )
    with pytest.raises(PersistenceFormatError, match="invalid deployment lock"):
        deserialize_deployment_lock(
            {
                **serialize_deployment_lock(DeploymentLock.open(at=NOW)),
                "lock_state": "locked",
                "guard_id": "019d2c1f-0000-7000-8000-a00000000003",
            }
        )
