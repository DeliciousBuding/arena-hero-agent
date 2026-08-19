"""TenantRuntime service orchestration, readiness, and telemetry isolation tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import cast

import pytest

from arena_hero_agent.application import (
    ComponentHealth,
    MemoryRuntimeTelemetry,
    ReconnectLimitExceeded,
    RuntimeStatus,
    TenantRuntime,
    TickLoopConfig,
    TickLoopResult,
    TickResult,
    TurnObservation,
)
from arena_hero_agent.application.tick_loop import (
    DeadlineOutcome,
    StoppedReason,
    SubmitOutcome,
    SubmitResult,
    TurnStream,
)
from arena_hero_agent.application.turns import Decision, PlayerLifecycle
from arena_hero_agent.domain import (
    DeadlineBudget,
    RulesVersion,
    TenantId,
    WorldProjection,
)

TENANT = TenantId("tenant-a")


def _observation(tick: int) -> TurnObservation:
    return TurnObservation(
        tick=tick,
        lifecycle=PlayerLifecycle.ACTIVE,
        resources=1,
        population=1,
        projection=WorldProjection(tick=tick, rules_version=RulesVersion.V0_14),
    )


def _config(*, max_reconnects: int = 3) -> TickLoopConfig:
    return TickLoopConfig(
        tenant_id=TENANT,
        tick_budget=DeadlineBudget.from_milliseconds(100),
        max_reconnects=max_reconnects,
        backoff=lambda _attempt: 0.0,
    )


class RecordingDecider:
    def __init__(self) -> None:
        self.calls: list[TurnObservation] = []

    def __call__(self, observation: TurnObservation, budget: DeadlineBudget) -> Decision:
        self.calls.append(observation)
        return Decision(tick=observation.tick)


class RecordingSubmitter:
    def __init__(self) -> None:
        self.calls: list[tuple[Decision, TurnObservation]] = []

    async def __call__(self, decision: Decision, observation: TurnObservation) -> SubmitOutcome:
        self.calls.append((decision, observation))
        return SubmitOutcome(accepted=True)


class _Raise:
    def __init__(self, error: Exception) -> None:
        self.error = error


class BlockGate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        self.entered.set()
        await self.release.wait()


class _Stream(AsyncIterator[TurnObservation]):
    def __init__(self, source: ScriptedTickSource, items: tuple[object, ...]) -> None:
        self._source = source
        self._items = items
        self.aclose_calls = 0

    def __aiter__(self) -> AsyncIterator[TurnObservation]:
        return self

    async def __anext__(self) -> TurnObservation:
        if not self._items:
            raise StopAsyncIteration
        item = self._items[0]
        if isinstance(item, _Raise):
            self._items = self._items[1:]
            raise item.error
        if isinstance(item, BlockGate):
            self._items = self._items[1:]
            await item.wait()
            raise AssertionError("unreachable")
        self._items = self._items[1:]
        return cast(TurnObservation, item)

    async def aclose(self) -> None:
        self.aclose_calls += 1


class ScriptedTickSource:
    def __init__(self, *attempts: Sequence[object]) -> None:
        self._attempts = [tuple(attempt) for attempt in attempts]
        self.stream_calls = 0

    def stream(self) -> TurnStream:
        self.stream_calls += 1
        items = self._attempts.pop(0) if self._attempts else ()
        return _Stream(self, items)


class RecordingRecorder:
    def __init__(
        self,
        *,
        fail_init: bool = False,
        fail_record_tick: bool = False,
        fail_record_loop: bool = False,
    ) -> None:
        self.fail_init = fail_init
        self.fail_record_tick = fail_record_tick
        self.fail_record_loop = fail_record_loop
        self.ticks: list[TickResult] = []
        self.loop: TickLoopResult | None = None
        self.close_count = 0

    def record_tick(self, result: TickResult) -> None:
        if self.fail_record_tick:
            raise RuntimeError("recorder tick failure")
        self.ticks.append(result)

    def record_tick_state(self, observation, decision, result: TickResult, decider_state=None) -> None:
        pass

    def record_loop(self, result: TickLoopResult) -> None:
        if self.fail_record_loop:
            raise RuntimeError("recorder loop failure")
        self.loop = result

    def read_ticks(self) -> tuple[TickResult, ...]:
        return tuple(self.ticks)

    def read_loop(self) -> TickLoopResult | None:
        if self.fail_init:
            raise RuntimeError("recorder init failure")
        return self.loop

    def close(self) -> None:
        self.close_count += 1


class EmitFailSink(MemoryRuntimeTelemetry):
    async def emit_tick(self, result: TickResult) -> None:
        raise RuntimeError("sink emit_tick failure")

    async def emit_loop(self, result: TickLoopResult) -> None:
        raise RuntimeError("sink emit_loop failure")


class FlushFailSink(MemoryRuntimeTelemetry):
    async def flush(self) -> None:
        raise RuntimeError("sink flush failure")


class CloseFailSink(MemoryRuntimeTelemetry):
    async def close(self) -> None:
        raise RuntimeError("sink close failure")


def _component(snapshot_components: tuple[ComponentHealth, ...], name: str) -> ComponentHealth:
    for component in snapshot_components:
        if component.name == name:
            return component
    raise AssertionError(f"missing component {name!r}")


async def test_happy_path_records_and_emits() -> None:
    source = ScriptedTickSource((_observation(1), _observation(2), _observation(3)))
    decider = RecordingDecider()
    submitter = RecordingSubmitter()
    recorder = RecordingRecorder()
    telemetry = MemoryRuntimeTelemetry()
    runtime = TenantRuntime(_config(), recorder=recorder, telemetry=telemetry, run_id="run-abc")

    result = await runtime.run(source, decider, submitter)

    assert result.ticks_processed == 3
    assert result.stopped_reason is StoppedReason.STREAM_ENDED
    assert recorder.ticks == list(result.outcomes)
    assert recorder.loop == result
    assert telemetry.ticks == list(result.outcomes)
    assert telemetry.loops == [result]
    assert telemetry.flush_count == 1
    assert telemetry.close_count == 1

    snapshot = runtime.snapshot()
    assert snapshot.status is RuntimeStatus.STOPPED
    assert snapshot.ready is False
    assert snapshot.tenant_id is TENANT
    assert snapshot.run_id == "run-abc"
    assert snapshot.last_tick == 3
    assert snapshot.ticks_processed == 3
    assert snapshot.stopped_reason is StoppedReason.STREAM_ENDED
    assert _component(snapshot.components, "source").healthy
    assert _component(snapshot.components, "recorder").healthy
    assert _component(snapshot.components, "telemetry").healthy


async def test_not_started_before_run() -> None:
    runtime = TenantRuntime(_config())

    snapshot = runtime.snapshot()

    assert snapshot.status is RuntimeStatus.NOT_STARTED
    assert snapshot.ready is False
    assert snapshot.last_tick is None
    assert _component(snapshot.components, "source").healthy is False


async def test_ready_while_loop_is_running() -> None:
    gate = BlockGate()
    source = ScriptedTickSource((_observation(1), gate))
    runtime = TenantRuntime(
        _config(), recorder=RecordingRecorder(), telemetry=MemoryRuntimeTelemetry()
    )

    task = asyncio.create_task(runtime.run(source, RecordingDecider(), RecordingSubmitter()))
    await gate.entered.wait()

    snapshot = runtime.snapshot()
    assert snapshot.status is RuntimeStatus.READY
    assert snapshot.ready is True
    assert _component(snapshot.components, "source").healthy
    assert _component(snapshot.components, "recorder").healthy
    assert _component(snapshot.components, "telemetry").healthy

    gate.release.set()
    result = await task
    assert result.ticks_processed == 1


async def test_telemetry_emit_failure_never_affects_tick_results() -> None:
    gate = BlockGate()
    source = ScriptedTickSource((_observation(1), gate))
    recorder = RecordingRecorder()
    telemetry = EmitFailSink()
    runtime = TenantRuntime(_config(), recorder=recorder, telemetry=telemetry)

    task = asyncio.create_task(runtime.run(source, RecordingDecider(), RecordingSubmitter()))
    await gate.entered.wait()

    snapshot = runtime.snapshot()
    assert snapshot.status is RuntimeStatus.DEGRADED
    assert snapshot.ready is True
    assert _component(snapshot.components, "telemetry").healthy is False
    assert _component(snapshot.components, "recorder").healthy
    assert _component(snapshot.components, "source").healthy

    gate.release.set()
    result = await task

    assert result.ticks_processed == 1
    assert result.outcomes[0].deadline_outcome is DeadlineOutcome.CANDIDATE
    assert result.outcomes[0].submit_result is SubmitResult.ACCEPTED
    assert recorder.ticks == list(result.outcomes)
    assert recorder.loop == result
    assert runtime.snapshot().status is RuntimeStatus.STOPPED


async def test_telemetry_flush_failure_never_affects_tick_results() -> None:
    recorder = RecordingRecorder()
    telemetry = FlushFailSink()
    runtime = TenantRuntime(_config(), recorder=recorder, telemetry=telemetry)

    result = await runtime.run(
        ScriptedTickSource((_observation(1), _observation(2))),
        RecordingDecider(),
        RecordingSubmitter(),
    )

    assert result.ticks_processed == 2
    assert recorder.ticks == list(result.outcomes)
    assert recorder.loop == result
    snapshot = runtime.snapshot()
    assert snapshot.status is RuntimeStatus.STOPPED
    assert _component(snapshot.components, "telemetry").healthy is False


async def test_telemetry_close_failure_never_affects_tick_results() -> None:
    recorder = RecordingRecorder()
    runtime = TenantRuntime(_config(), recorder=recorder, telemetry=CloseFailSink())

    result = await runtime.run(
        ScriptedTickSource((_observation(1),)),
        RecordingDecider(),
        RecordingSubmitter(),
    )

    assert result.ticks_processed == 1
    assert recorder.ticks == list(result.outcomes)
    assert runtime.snapshot().status is RuntimeStatus.STOPPED


async def test_recorder_failure_degrades_but_loop_keeps_running() -> None:
    gate = BlockGate()
    source = ScriptedTickSource((_observation(1), gate))
    recorder = RecordingRecorder(fail_record_tick=True)
    telemetry = MemoryRuntimeTelemetry()
    runtime = TenantRuntime(_config(), recorder=recorder, telemetry=telemetry)

    task = asyncio.create_task(runtime.run(source, RecordingDecider(), RecordingSubmitter()))
    await gate.entered.wait()

    snapshot = runtime.snapshot()
    assert snapshot.status is RuntimeStatus.DEGRADED
    assert snapshot.ready is False
    assert _component(snapshot.components, "recorder").healthy is False
    assert _component(snapshot.components, "source").healthy
    assert _component(snapshot.components, "telemetry").healthy

    gate.release.set()
    result = await task

    assert result.ticks_processed == 1
    assert recorder.ticks == []
    assert telemetry.ticks == list(result.outcomes)
    assert runtime.snapshot().status is RuntimeStatus.STOPPED


async def test_recorder_init_failure_is_not_ready() -> None:
    gate = BlockGate()
    source = ScriptedTickSource((_observation(1), gate))
    recorder = RecordingRecorder(fail_init=True, fail_record_tick=True)
    runtime = TenantRuntime(_config(), recorder=recorder)

    task = asyncio.create_task(runtime.run(source, RecordingDecider(), RecordingSubmitter()))
    await gate.entered.wait()

    snapshot = runtime.snapshot()
    assert snapshot.status is RuntimeStatus.DEGRADED
    assert snapshot.ready is False
    assert _component(snapshot.components, "recorder").healthy is False

    gate.release.set()
    await task


async def test_source_failure_stops_and_reports_not_ready() -> None:
    source = ScriptedTickSource((_Raise(RuntimeError("source down")),))
    runtime = TenantRuntime(_config(max_reconnects=0), recorder=RecordingRecorder())

    with pytest.raises(ReconnectLimitExceeded):
        await runtime.run(source, RecordingDecider(), RecordingSubmitter())

    snapshot = runtime.snapshot()
    assert snapshot.status is RuntimeStatus.STOPPED
    assert snapshot.ready is False
    assert _component(snapshot.components, "source").healthy is False
    assert "tick source failed" in (snapshot.last_error or "")


async def test_run_while_running_raises() -> None:
    gate = BlockGate()
    source = ScriptedTickSource((_observation(1), gate))
    runtime = TenantRuntime(_config())

    task = asyncio.create_task(runtime.run(source, RecordingDecider(), RecordingSubmitter()))
    await gate.entered.wait()

    with pytest.raises(RuntimeError, match="already running"):
        await runtime.run(ScriptedTickSource(), RecordingDecider(), RecordingSubmitter())

    gate.release.set()
    await task
