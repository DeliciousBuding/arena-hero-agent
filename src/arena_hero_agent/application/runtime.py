"""Single-tenant runtime service: loop orchestration, readiness, best-effort telemetry.

P4-6 composes the P4-4 tick loop with the P4-5 recorder and the telemetry port.
Telemetry is never readiness-affecting: sink emit/flush/close failures are
swallowed and only mark the ``telemetry`` component unhealthy. Readiness
requires the source loop to have started and the recorder to be healthy, so
critical recorder or source failures surface as ``degraded`` / not ready while
decisions and submissions keep flowing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from arena_hero_agent.domain import TenantId
from arena_hero_agent.telemetry import DEFAULT_PROCESS_RUN_ID

from .recorder import TickRecorder
from .telemetry import RuntimeTelemetry, default_run_id
from .tick_loop import (
    Decider,
    SingleTenantTickLoop,
    StoppedReason,
    Submitter,
    TickLoopConfig,
    TickLoopResult,
    TickResult,
    TickSource,
)

_COMPONENT_SOURCE = "source"
_COMPONENT_RECORDER = "recorder"
_COMPONENT_TELEMETRY = "telemetry"


class RuntimeStatus(StrEnum):
    """Lifecycle status of one tenant runtime."""

    NOT_STARTED = "not_started"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """Readiness signal for one runtime component."""

    name: str
    healthy: bool
    message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("component name must be a non-empty string")
        if not isinstance(self.healthy, bool):
            raise TypeError("healthy must be a boolean")
        if self.message is not None and not isinstance(self.message, str):
            raise TypeError("message must be a string or None")


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Queryable point-in-time view of a tenant runtime."""

    status: RuntimeStatus
    ready: bool
    tenant_id: TenantId
    process_run_id: str
    run_id: str
    started_at_ns: int | None
    updated_at_ns: int
    last_tick: int | None
    ticks_processed: int | None
    duplicate_ticks: int | None
    out_of_order_ticks: int | None
    gap_ticks: int | None
    reconnect_count: int | None
    stopped_reason: StoppedReason | None
    components: tuple[ComponentHealth, ...]
    last_error: str | None = None


class TenantRuntime:
    """Orchestrate one tenant's offline tick loop with best-effort side effects.

    Decisions and submissions are owned by ``SingleTenantTickLoop``; this
    service only observes outcomes. Recorder and telemetry failures never
    change the returned ``TickLoopResult``.
    """

    def __init__(
        self,
        config: TickLoopConfig,
        *,
        recorder: TickRecorder | None = None,
        telemetry: RuntimeTelemetry | None = None,
        process_run_id: str = DEFAULT_PROCESS_RUN_ID,
        run_id: str | None = None,
    ) -> None:
        self._config = config
        self._recorder = recorder
        self._telemetry = telemetry
        self._process_run_id = process_run_id
        self._run_id = (
            run_id if run_id is not None else default_run_id(process_run_id, config.tenant_id)
        )
        self._status = RuntimeStatus.NOT_STARTED
        self._components: dict[str, ComponentHealth] = {
            _COMPONENT_SOURCE: ComponentHealth(_COMPONENT_SOURCE, False, "not_started"),
            _COMPONENT_RECORDER: ComponentHealth(
                _COMPONENT_RECORDER,
                True,
                "not_configured" if recorder is None else "initialized",
            ),
            _COMPONENT_TELEMETRY: ComponentHealth(
                _COMPONENT_TELEMETRY,
                True,
                "not_configured" if telemetry is None else "initialized",
            ),
        }
        self._running = False
        self._started_at_ns: int | None = None
        self._updated_at_ns = config.clock.monotonic_ns()
        self._last_result: TickLoopResult | None = None
        self._last_error: str | None = None

    @property
    def status(self) -> RuntimeStatus:
        return self._status

    def snapshot(self) -> RuntimeSnapshot:
        result = self._last_result
        return RuntimeSnapshot(
            status=self._status,
            ready=self._is_ready(),
            tenant_id=self._config.tenant_id,
            process_run_id=self._process_run_id,
            run_id=self._run_id,
            started_at_ns=self._started_at_ns,
            updated_at_ns=self._updated_at_ns,
            last_tick=result.last_tick if result is not None else None,
            ticks_processed=result.ticks_processed if result is not None else None,
            duplicate_ticks=result.duplicate_ticks if result is not None else None,
            out_of_order_ticks=result.out_of_order_ticks if result is not None else None,
            gap_ticks=result.gap_ticks if result is not None else None,
            reconnect_count=result.reconnect_count if result is not None else None,
            stopped_reason=result.stopped_reason if result is not None else None,
            components=tuple(self._components.values()),
            last_error=self._last_error,
        )

    async def run(
        self,
        source: TickSource,
        decide: Decider,
        submit: Submitter,
    ) -> TickLoopResult:
        """Run one tick loop and return its exact result.

        The source loop, recorder, and telemetry are best-effort with respect
        to one another: failures in recorder or telemetry never change
        decisions, submissions, or the returned result.
        """
        if self._running:
            raise RuntimeError("runtime is already running")
        self._running = True
        try:
            return await self._run(source, decide, submit)
        finally:
            self._running = False

    async def _run(
        self,
        source: TickSource,
        decide: Decider,
        submit: Submitter,
    ) -> TickLoopResult:
        clock = self._config.clock
        self._started_at_ns = clock.monotonic_ns()
        self._last_result = None
        self._last_error = None
        self._touch()
        self._status = RuntimeStatus.STARTING

        if self._recorder is not None:
            try:
                self._recorder.read_loop()
                self._set_component(_COMPONENT_RECORDER, True, "initialized")
            except Exception as exc:
                self._set_component(_COMPONENT_RECORDER, False, f"init failed: {exc}")

        loop = SingleTenantTickLoop(self._config)

        async def on_tick(result: TickResult) -> None:
            if self._recorder is not None:
                try:
                    self._recorder.record_tick(result)
                    self._set_component(_COMPONENT_RECORDER, True, "ok")
                except Exception as exc:
                    self._set_component(_COMPONENT_RECORDER, False, f"record_tick failed: {exc}")
            if self._telemetry is not None:
                try:
                    await self._telemetry.emit_tick(result)
                    self._set_component(_COMPONENT_TELEMETRY, True, "ok")
                except Exception as exc:
                    self._set_component(_COMPONENT_TELEMETRY, False, f"emit_tick failed: {exc}")
            self._touch()

        self._set_component(_COMPONENT_SOURCE, True, "started")
        self._status = self._target_status()

        try:
            result = await loop.run(source, decide, submit, on_tick=on_tick)
        except asyncio.CancelledError:
            self._set_component(_COMPONENT_SOURCE, False, "cancelled")
            self._last_error = "cancelled"
            self._status = RuntimeStatus.STOPPED
            raise
        except Exception as exc:
            self._set_component(_COMPONENT_SOURCE, False, f"{type(exc).__name__}: {exc}")
            self._last_error = str(exc)
            self._status = RuntimeStatus.STOPPED
            raise

        self._status = RuntimeStatus.STOPPING
        if self._recorder is not None:
            try:
                self._recorder.record_loop(result)
                self._set_component(_COMPONENT_RECORDER, True, "ok")
            except Exception as exc:
                self._set_component(_COMPONENT_RECORDER, False, f"record_loop failed: {exc}")
        if self._telemetry is not None:
            try:
                await self._telemetry.emit_loop(result)
                self._set_component(_COMPONENT_TELEMETRY, True, "ok")
            except Exception as exc:
                self._set_component(_COMPONENT_TELEMETRY, False, f"emit_loop failed: {exc}")
            try:
                await self._telemetry.flush()
                self._set_component(_COMPONENT_TELEMETRY, True, "ok")
            except Exception as exc:
                self._set_component(_COMPONENT_TELEMETRY, False, f"flush failed: {exc}")
            try:
                await self._telemetry.close()
            except Exception as exc:
                self._set_component(_COMPONENT_TELEMETRY, False, f"close failed: {exc}")
        self._last_result = result
        self._status = RuntimeStatus.STOPPED
        self._touch()
        return result

    def _is_ready(self) -> bool:
        if self._status not in (RuntimeStatus.READY, RuntimeStatus.DEGRADED):
            return False
        return (
            self._components[_COMPONENT_SOURCE].healthy
            and self._components[_COMPONENT_RECORDER].healthy
        )

    def _target_status(self) -> RuntimeStatus:
        healthy = all(
            self._components[name].healthy
            for name in (_COMPONENT_SOURCE, _COMPONENT_RECORDER, _COMPONENT_TELEMETRY)
        )
        return RuntimeStatus.READY if healthy else RuntimeStatus.DEGRADED

    def _set_component(self, name: str, healthy: bool, message: str | None) -> None:
        self._components[name] = ComponentHealth(name, healthy, message)
        self._touch()
        if self._status in (RuntimeStatus.READY, RuntimeStatus.DEGRADED):
            self._status = self._target_status()

    def _touch(self) -> None:
        self._updated_at_ns = self._config.clock.monotonic_ns()


__all__ = [
    "ComponentHealth",
    "RuntimeSnapshot",
    "RuntimeStatus",
    "TenantRuntime",
]
