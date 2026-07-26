"""Own the persisted lifecycle of one physical runtime instance."""

from __future__ import annotations

from typing import Protocol

from shittim_chest.application.errors import RuntimeNotReady
from shittim_chest.application.ports import (
    Clock,
    RepositoryConflict,
)
from shittim_chest.application.scale_to_zero import RuntimeState, RuntimeStatus

DEFAULT_RUNTIME_STATE_CAS_ATTEMPTS = 5


class _RuntimeStateStore(Protocol):
    """Narrow persistence boundary needed by one running process."""

    async def get(self) -> RuntimeState | None: ...

    async def replace(
        self,
        *,
        expected: RuntimeState,
        updated: RuntimeState,
    ) -> RuntimeState: ...


class RuntimeInstanceState:
    """Bind and advance only the Runtime State owned by this process."""

    def __init__(
        self,
        *,
        clock: Clock,
        repository: _RuntimeStateStore,
        runtime_instance_id: str,
        cas_attempts: int = DEFAULT_RUNTIME_STATE_CAS_ATTEMPTS,
    ) -> None:
        if not runtime_instance_id.strip():
            raise ValueError("runtime instance ID must not be empty")
        if isinstance(cas_attempts, bool) or not isinstance(cas_attempts, int):
            raise TypeError("runtime state CAS attempts must be an integer")
        if cas_attempts < 1:
            raise ValueError("runtime state CAS attempts must be positive")
        self._clock = clock
        self._repository = repository
        self._runtime_instance_id = runtime_instance_id
        self._cas_attempts = cas_attempts

    @property
    def runtime_instance_id(self) -> str:
        """Expose the content-free owner used by ingress and debate fencing."""

        return self._runtime_instance_id

    async def mark_started(self) -> RuntimeState:
        """Bind the current STARTING generation before external clients start."""

        for _attempt in range(self._cas_attempts):
            current = await self._require_state()
            if self._is_current_ready_state(current):
                return current
            if current.status is not RuntimeStatus.STARTING:
                raise RuntimeNotReady("persisted runtime is not starting")
            if current.runtime_instance_id not in {None, self._runtime_instance_id}:
                raise RuntimeNotReady("runtime generation belongs to another instance")
            if (
                current.runtime_instance_id == self._runtime_instance_id
                and current.started_at is not None
            ):
                return current
            updated = current.mark_started(
                at=max(self._clock.now(), current.updated_at),
                runtime_instance_id=self._runtime_instance_id,
            )
            try:
                return await self._repository.replace(expected=current, updated=updated)
            except RepositoryConflict:
                continue
        raise RuntimeNotReady("runtime start lost repeated state races")

    async def mark_ready(self, *, active: bool) -> RuntimeState:
        """Open the persisted READY/BUSY state only after recovery initialization."""

        if not isinstance(active, bool):
            raise TypeError("runtime activity flag must be a boolean")
        for _attempt in range(self._cas_attempts):
            current = await self._require_state()
            self._require_current_instance(current)
            if current.status is RuntimeStatus.BUSY:
                return current
            if current.status is RuntimeStatus.READY:
                if not active:
                    return current
                target = RuntimeStatus.BUSY
            elif current.status is RuntimeStatus.STARTING:
                target = RuntimeStatus.BUSY if active else RuntimeStatus.READY
            else:
                raise RuntimeNotReady("persisted runtime cannot accept ingress")
            updated = current.transition(
                target,
                at=max(self._clock.now(), current.updated_at),
                runtime_instance_id=self._runtime_instance_id,
            )
            try:
                return await self._repository.replace(expected=current, updated=updated)
            except RepositoryConflict:
                continue
        raise RuntimeNotReady("runtime readiness lost repeated state races")

    async def claim_woken_start(self) -> bool:
        """Bind a live process when an IDLE wake reset its state to STARTING.

        The initial read deliberately does not mutate non-STARTING states.  If a
        concurrent wake or owner claim changes the record, ``mark_started``
        applies the existing generation-fenced CAS rules before this process can
        reopen recovery or admission.
        """

        current = await self._require_state()
        if current.status is not RuntimeStatus.STARTING:
            return False
        await self.mark_started()
        return True

    async def mark_busy(self) -> RuntimeState:
        """Ensure accepted or recovered work is represented as BUSY."""

        return await self.mark_ready(active=True)

    async def _require_state(self) -> RuntimeState:
        current = await self._repository.get()
        if current is None:
            raise RuntimeNotReady("persisted runtime state is missing")
        return current

    def _is_current_ready_state(self, state: RuntimeState) -> bool:
        return state.runtime_instance_id == self._runtime_instance_id and state.status in {
            RuntimeStatus.READY,
            RuntimeStatus.BUSY,
        }

    def _require_current_instance(self, state: RuntimeState) -> None:
        if state.runtime_instance_id != self._runtime_instance_id:
            raise RuntimeNotReady("runtime generation belongs to another instance")


__all__ = ("DEFAULT_RUNTIME_STATE_CAS_ATTEMPTS", "RuntimeInstanceState")
