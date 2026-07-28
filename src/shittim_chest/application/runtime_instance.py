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
    """Bind and advance state owned by the sole physical ECS task.

    Production composition supplies an identity derived from task metadata.
    Safe takeover therefore relies on the ECS service invariant that at most
    one physical task is active; process-random identities are not sufficient.
    """

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
        self._owned_generation: int | None = None

    @property
    def runtime_instance_id(self) -> str:
        """Expose the content-free owner used by ingress and debate fencing."""

        return self._runtime_instance_id

    async def mark_started(self) -> RuntimeState:
        """Bind the current STARTING generation before external clients start."""

        stale_owner: str | None = None
        attempted_unbound_bind = False
        for _attempt in range(self._cas_attempts):
            current = await self._require_state()
            if self._is_current_ready_state(current):
                return self._remember_owned_generation(current)
            if self._is_foreign_bound_state(current):
                if stale_owner is None and attempted_unbound_bind:
                    raise RuntimeNotReady("runtime replacement lost the ownership race")
                if stale_owner is not None and current.runtime_instance_id != stale_owner:
                    raise RuntimeNotReady("runtime replacement lost the ownership race")
                stale_owner = current.runtime_instance_id
                updated = current.fence_stale_instance(
                    at=max(self._clock.now(), current.updated_at),
                )
                try:
                    await self._repository.replace(expected=current, updated=updated)
                except RepositoryConflict:
                    continue
                continue
            if current.status is not RuntimeStatus.STARTING:
                raise RuntimeNotReady("persisted runtime is not starting")
            if current.runtime_instance_id not in {None, self._runtime_instance_id}:
                raise RuntimeNotReady("runtime generation belongs to another instance")
            if (
                current.runtime_instance_id == self._runtime_instance_id
                and current.started_at is not None
            ):
                return self._remember_owned_generation(current)
            if current.runtime_instance_id is None:
                attempted_unbound_bind = True
            updated = current.mark_started(
                at=max(self._clock.now(), current.updated_at),
                runtime_instance_id=self._runtime_instance_id,
            )
            try:
                persisted = await self._repository.replace(expected=current, updated=updated)
            except RepositoryConflict:
                continue
            return self._remember_owned_generation(persisted)
        raise RuntimeNotReady("runtime start lost repeated state races")

    async def mark_ready(self, *, active: bool) -> RuntimeState:
        """Open the persisted READY/BUSY state only after recovery initialization."""

        if not isinstance(active, bool):
            raise TypeError("runtime activity flag must be a boolean")
        for _attempt in range(self._cas_attempts):
            current = await self._require_state()
            self._require_current_instance(current)
            if current.status is RuntimeStatus.BUSY:
                return self._remember_owned_generation(current)
            if current.status is RuntimeStatus.READY:
                if not active:
                    return self._remember_owned_generation(current)
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
                persisted = await self._repository.replace(expected=current, updated=updated)
            except RepositoryConflict:
                continue
            return self._remember_owned_generation(persisted)
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
        if current.runtime_instance_id not in {None, self._runtime_instance_id}:
            raise RuntimeNotReady("runtime generation belongs to another instance")
        await self.mark_started()
        return True

    async def mark_busy(self) -> RuntimeState:
        """Ensure accepted or recovered work is represented as BUSY."""

        return await self.mark_ready(active=True)

    async def mark_shutdown_complete(self) -> RuntimeState:
        """Fence this process' generation after its owned cleanup completes.

        A reconciler-driven scale-down reaches ``STOPPING`` before ECS stops the
        task, so that exact owned generation may converge to ``STOPPED``.  Any
        other bound live state means the process disappeared unexpectedly and
        must leave a fresh unbound ``STARTING`` generation for replacement.
        A newer generation or owner always wins and is returned unchanged.
        """

        for _attempt in range(self._cas_attempts):
            current = await self._require_state()
            if current.runtime_instance_id != self._runtime_instance_id:
                return current
            if self._owned_generation is None:
                raise RuntimeNotReady("runtime shutdown has no owned generation")
            if current.generation != self._owned_generation:
                return current
            at = max(self._clock.now(), current.updated_at)
            if current.status is RuntimeStatus.STOPPING:
                updated = current.transition(
                    RuntimeStatus.STOPPED,
                    at=at,
                    runtime_instance_id=self._runtime_instance_id,
                )
            elif current.status in {
                RuntimeStatus.STARTING,
                RuntimeStatus.READY,
                RuntimeStatus.BUSY,
            }:
                updated = current.fence_stale_instance(at=at)
            elif current.status in {RuntimeStatus.IDLE, RuntimeStatus.DEGRADED}:
                updated = current.resume_for_work(at=at)
            else:
                raise RuntimeNotReady("persisted runtime cannot complete shutdown")
            current.validate_replacement(updated)
            try:
                return await self._repository.replace(expected=current, updated=updated)
            except RepositoryConflict:
                continue
        raise RuntimeNotReady("runtime shutdown lost repeated state races")

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

    def _is_foreign_bound_state(self, state: RuntimeState) -> bool:
        return (
            state.runtime_instance_id is not None
            and state.runtime_instance_id != self._runtime_instance_id
            and state.status
            in {
                RuntimeStatus.STARTING,
                RuntimeStatus.READY,
                RuntimeStatus.BUSY,
            }
        )

    def _require_current_instance(self, state: RuntimeState) -> None:
        if state.runtime_instance_id != self._runtime_instance_id:
            raise RuntimeNotReady("runtime generation belongs to another instance")

    def _remember_owned_generation(self, state: RuntimeState) -> RuntimeState:
        self._require_current_instance(state)
        self._owned_generation = state.generation
        return state


__all__ = ("DEFAULT_RUNTIME_STATE_CAS_ATTEMPTS", "RuntimeInstanceState")
