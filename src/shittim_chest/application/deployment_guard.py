"""SDK-independent deployment admission and deployment-lock contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum, unique

from shittim_chest.application.scale_to_zero import RuntimeActivity, RuntimeState, RuntimeStatus

DEPLOYMENT_LOCK_RECORD_SCHEMA_VERSION = 1
DEPLOYMENT_GUARD_AUDIT_SCHEMA_VERSION = 2
_COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_ACTOR_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_RUN_ID_PATTERN = re.compile(r"[1-9][0-9]{0,19}\Z")
_GUARD_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)


def validate_deployment_actor(value: str) -> str:
    """Return one safe GitHub actor or reject it before an adapter call."""

    if not isinstance(value, str) or _ACTOR_PATTERN.fullmatch(value) is None:
        raise ValueError("actor must be a safe GitHub login")
    return value


def validate_deployment_guard_id(value: str) -> str:
    """Return one canonical UUIDv7 guard ID or reject it before an adapter call."""

    if not isinstance(value, str) or _GUARD_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("guard ID must be a canonical UUIDv7")
    return value


@unique
class DeploymentMode(StrEnum):
    """Deployment admission mode selected by the trusted workflow."""

    NORMAL = "normal"
    BREAK_GLASS = "break-glass"


@unique
class BreakGlassReason(StrEnum):
    """Content-free reasons accepted for exceptional deployment admission."""

    INCIDENT_RESPONSE = "incident-response"
    SECURITY_INVESTIGATION = "security-investigation"
    SERVICE_RECOVERY = "service-recovery"


@unique
class DeploymentLockState(StrEnum):
    """Persistent ingress-admission lock state."""

    OPEN = "open"
    LOCKED = "locked"


@unique
class DeploymentGuardCode(StrEnum):
    """Stable content-free deployment decision codes."""

    SAFE = "safe"
    BREAK_GLASS_OVERRIDE = "break_glass_override"
    DEPLOYMENT_LOCKED = "deployment_locked"
    RUNTIME_NOT_QUIESCENT = "runtime_not_quiescent"
    RUNTIME_DEGRADED = "runtime_degraded"
    DURABLE_ACTIVITY_PRESENT = "durable_activity_present"
    SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"


@dataclass(frozen=True, slots=True)
class DeploymentGuardContext:
    """Public-safe immutable metadata supplied by one trusted workflow run."""

    commit_sha: str
    actor: str
    run_id: str
    environment: str
    mode: DeploymentMode = DeploymentMode.NORMAL
    reason: BreakGlassReason | None = None

    def __post_init__(self) -> None:
        if _COMMIT_SHA_PATTERN.fullmatch(self.commit_sha) is None:
            raise ValueError("commit SHA must be 40 lowercase hexadecimal characters")
        validate_deployment_actor(self.actor)
        if _RUN_ID_PATTERN.fullmatch(self.run_id) is None:
            raise ValueError("run ID must be a positive decimal identifier")
        if self.environment != "production":
            raise ValueError("deployment guard environment must be production")
        if (self.mode is DeploymentMode.BREAK_GLASS) is not (self.reason is not None):
            raise ValueError("break-glass mode and reason must be supplied together")


@dataclass(frozen=True, slots=True)
class DeploymentLock:
    """Versioned lock that closes ingress admission during a deployment."""

    state: DeploymentLockState
    fencing_token: int
    version: int
    updated_at: datetime
    guard_id: str | None = None
    owner: str | None = None
    acquired_at: datetime | None = None
    expires_at: datetime | None = None
    mode: DeploymentMode | None = None
    reason: BreakGlassReason | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("deployment lock fencing token", self.fencing_token),
            ("deployment lock version", self.version),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        _require_utc(self.updated_at, label="deployment lock update timestamp")
        optional_values = (
            self.guard_id,
            self.owner,
            self.acquired_at,
            self.expires_at,
            self.mode,
        )
        if self.state is DeploymentLockState.OPEN:
            if any(value is not None for value in optional_values) or self.reason is not None:
                raise ValueError("open deployment lock cannot retain ownership")
            return
        if any(value is None for value in optional_values):
            raise ValueError("locked deployment lock requires complete ownership")
        if self.guard_id is None:
            raise ValueError("locked deployment lock requires a canonical UUIDv7 guard ID")
        validate_deployment_guard_id(self.guard_id)
        if self.owner is None:
            raise ValueError("locked deployment lock requires a safe owner")
        validate_deployment_actor(self.owner)
        if self.acquired_at is None or self.expires_at is None or self.mode is None:
            raise ValueError("locked deployment lock is incomplete")
        _require_utc(self.acquired_at, label="deployment lock acquisition timestamp")
        _require_utc(self.expires_at, label="deployment lock expiry timestamp")
        if not self.updated_at == self.acquired_at < self.expires_at:
            raise ValueError("deployment lock timestamps are inconsistent")
        if (self.mode is DeploymentMode.BREAK_GLASS) is not (self.reason is not None):
            raise ValueError("locked break-glass mode and reason must agree")

    @classmethod
    def open(cls, *, at: datetime) -> DeploymentLock:
        """Create the initial unlocked control record."""

        return cls(
            state=DeploymentLockState.OPEN,
            fencing_token=0,
            version=0,
            updated_at=at,
        )


@dataclass(frozen=True, slots=True)
class DeploymentGuardSnapshot:
    """One strongly consistent, content-free deployment snapshot."""

    runtime: RuntimeState
    activity: RuntimeActivity
    deployment_lock: DeploymentLock


@dataclass(frozen=True, slots=True)
class DeploymentGuardAssessment:
    """Stable deployment decision safe to emit into a workflow audit file."""

    allowed: bool
    code: DeploymentGuardCode
    context: DeploymentGuardContext
    evaluated_at: datetime
    runtime_status: RuntimeStatus
    runtime_generation: int
    runtime_version: int
    activity_clear: bool
    deployment_lock_state: DeploymentLockState
    deployment_lock_fencing_token: int

    def __post_init__(self) -> None:
        _require_utc(self.evaluated_at, label="deployment guard evaluation timestamp")
        allowed_codes = {DeploymentGuardCode.SAFE, DeploymentGuardCode.BREAK_GLASS_OVERRIDE}
        if self.allowed != (self.code in allowed_codes):
            raise ValueError("deployment guard result and decision code disagree")


def assess_deployment(
    snapshot: DeploymentGuardSnapshot,
    *,
    context: DeploymentGuardContext,
    evaluated_at: datetime,
) -> DeploymentGuardAssessment:
    """Fail closed unless normal deployment is quiescent or break-glass is explicit."""

    _require_utc(evaluated_at, label="deployment guard evaluation timestamp")
    if snapshot.deployment_lock.state is not DeploymentLockState.OPEN:
        return _assessment(
            snapshot,
            context=context,
            evaluated_at=evaluated_at,
            code=DeploymentGuardCode.DEPLOYMENT_LOCKED,
        )
    if context.mode is DeploymentMode.BREAK_GLASS:
        return _assessment(
            snapshot,
            context=context,
            evaluated_at=evaluated_at,
            code=DeploymentGuardCode.BREAK_GLASS_OVERRIDE,
        )
    if snapshot.runtime.status is RuntimeStatus.DEGRADED:
        return _assessment(
            snapshot,
            context=context,
            evaluated_at=evaluated_at,
            code=DeploymentGuardCode.RUNTIME_DEGRADED,
        )
    if snapshot.runtime.status not in {RuntimeStatus.STOPPED, RuntimeStatus.IDLE}:
        return _assessment(
            snapshot,
            context=context,
            evaluated_at=evaluated_at,
            code=DeploymentGuardCode.RUNTIME_NOT_QUIESCENT,
        )
    if not snapshot.activity.is_complete:
        return _assessment(
            snapshot,
            context=context,
            evaluated_at=evaluated_at,
            code=DeploymentGuardCode.DURABLE_ACTIVITY_PRESENT,
        )
    return _assessment(
        snapshot,
        context=context,
        evaluated_at=evaluated_at,
        code=DeploymentGuardCode.SAFE,
    )


def _assessment(
    snapshot: DeploymentGuardSnapshot,
    *,
    context: DeploymentGuardContext,
    evaluated_at: datetime,
    code: DeploymentGuardCode,
) -> DeploymentGuardAssessment:
    return DeploymentGuardAssessment(
        allowed=code in {DeploymentGuardCode.SAFE, DeploymentGuardCode.BREAK_GLASS_OVERRIDE},
        code=code,
        context=context,
        evaluated_at=evaluated_at,
        runtime_status=snapshot.runtime.status,
        runtime_generation=snapshot.runtime.generation,
        runtime_version=snapshot.runtime.version,
        activity_clear=snapshot.activity.is_complete,
        deployment_lock_state=snapshot.deployment_lock.state,
        deployment_lock_fencing_token=snapshot.deployment_lock.fencing_token,
    )


def _require_utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} must be timezone-aware UTC")


__all__ = (
    "DEPLOYMENT_GUARD_AUDIT_SCHEMA_VERSION",
    "DEPLOYMENT_LOCK_RECORD_SCHEMA_VERSION",
    "BreakGlassReason",
    "DeploymentGuardAssessment",
    "DeploymentGuardCode",
    "DeploymentGuardContext",
    "DeploymentGuardSnapshot",
    "DeploymentLock",
    "DeploymentLockState",
    "DeploymentMode",
    "assess_deployment",
    "validate_deployment_actor",
    "validate_deployment_guard_id",
)
