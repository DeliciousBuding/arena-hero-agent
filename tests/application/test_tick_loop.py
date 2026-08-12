"""Offline single-tenant tick loop behavior tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from typing import cast

import pytest

from arena_hero_agent.application import (
    DeadlineOutcome,
    Decision,
    PlayerLifecycle,
    ReconnectLimitExceeded,
    SingleTenantTickLoop,
    StoppedReason,
    SubmitErrorPolicy,
    SubmitOutcome,
    SubmitResult,
    TickLoopConfig,
    TickResult,
    TurnObservation,
    TurnStream,
)
from arena_hero_agent.domain import (
    DeadlineBudget,
    DecisionId,
    RulesVersion,
    StateDigest,
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


def _config() -> TickLoopConfig:
    return TickLoopConfig(
        tenant_id=TENANT,
        tick_budget=DeadlineBudget.from_milliseconds(100),
        backoff=lambda _attempt: 0.0,
    )


class FakeClock:
    def __init__(self, *, start_ns: int = 0) -> None:
        self._now_ns = start_ns

    def monotonic_ns(self) -> int:
        return self._now_ns

    def advance(self, nanoseconds: int) -> None:
        self._now_ns += nanoseconds


class RecordingDecider:
    def __init__(self) -> None:
        self.calls: list[tuple[TurnObservation, DeadlineBudget]] = []

    def __call__(self, observation: TurnObservation, budget: DeadlineBudget) -> Decision:
        self.calls.append((observation, budget))
        return Decision(tick=observation.tick)


def _consuming_decider(
    clock: FakeClock, nanoseconds: int
) -> Callable[[TurnObservation, DeadlineBudget], Decision]:
    def decide(observation: TurnObservation, budget: DeadlineBudget) -> Decision:
        clock.advance(nanoseconds)
        return Decision(tick=observation.tick)

    return decide


class RecordingSubmitter:
    def __init__(self, *outcomes: SubmitOutcome) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[Decision, TurnObservation]] = []

    async def __call__(self, decision: Decision, observation: TurnObservation) -> SubmitOutcome:
        self.calls.append((decision, observation))
        if self._outcomes:
            return self._outcomes.pop(0)
        return SubmitOutcome(accepted=True)


class _Raise:
    def __init__(self, error: Exception) -> None:
        self.error = error


class BlockGate:
    """Signals when a stream blocks so a test can cancel deterministically."""

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
    """TickSource serving one scripted attempt per ``stream()`` call."""

    def __init__(self, *attempts: Sequence[object]) -> None:
        self._attempts = [tuple(attempt) for attempt in attempts]
        self.stream_calls = 0
        self.streams: list[_Stream] = []

    def stream(self) -> TurnStream:
        self.stream_calls += 1
        items = self._attempts.pop(0) if self._attempts else ()
        stream = _Stream(self, items)
        self.streams.append(stream)
        return stream


async def test_runs_sequential_ticks() -> None:
    source = ScriptedTickSource((_observation(1), _observation(2), _observation(3)))
    decider = RecordingDecider()
    submitter = RecordingSubmitter()
    loop = SingleTenantTickLoop(_config())

    result = await loop.run(source, decider, submitter)

    assert result.tenant_id is TENANT
    assert result.ticks_processed == 3
    assert result.last_tick == 3
    assert result.duplicate_ticks == 0
    assert result.out_of_order_ticks == 0
    assert result.gap_ticks == 0
    assert result.reconnect_count == 0
    assert result.stopped_reason is StoppedReason.STREAM_ENDED
    assert [obs.tick for obs, _ in decider.calls] == [1, 2, 3]
    assert [call[0].tick for call in submitter.calls] == [1, 2, 3]
    assert [outcome.tick for outcome in result.outcomes] == [1, 2, 3]
    assert all(outcome.deadline_outcome is DeadlineOutcome.CANDIDATE for outcome in result.outcomes)
    assert all(outcome.submit_result is SubmitResult.ACCEPTED for outcome in result.outcomes)


async def test_decision_ids_are_deterministic_per_tick() -> None:
    source = ScriptedTickSource((_observation(1),))
    loop = SingleTenantTickLoop(_config())

    result = await loop.run(source, RecordingDecider(), RecordingSubmitter())

    expected = DecisionId.from_deterministic_input(
        (TENANT, 1, StateDigest.from_state(_observation(1)))
    )
    assert result.outcomes[0].decision_id == expected
    assert result.outcomes[0].decision_id == DecisionId.from_deterministic_input(
        (TENANT, 1, StateDigest.from_state(_observation(1)))
    )


async def test_skips_duplicate_tick() -> None:
    source = ScriptedTickSource((_observation(1), _observation(1), _observation(2)))
    decider = RecordingDecider()
    loop = SingleTenantTickLoop(_config())

    result = await loop.run(source, decider, RecordingSubmitter())

    assert result.ticks_processed == 2
    assert result.duplicate_ticks == 1
    assert result.last_tick == 2
    assert [obs.tick for obs, _ in decider.calls] == [1, 2]
    assert [outcome.tick for outcome in result.outcomes] == [1, 2]


async def test_skips_out_of_order_tick() -> None:
    source = ScriptedTickSource((_observation(2), _observation(1), _observation(3)))
    decider = RecordingDecider()
    loop = SingleTenantTickLoop(_config())

    result = await loop.run(source, decider, RecordingSubmitter())

    assert result.ticks_processed == 2
    assert result.out_of_order_ticks == 1
    assert result.last_tick == 3
    assert [obs.tick for obs, _ in decider.calls] == [2, 3]
    assert [outcome.tick for outcome in result.outcomes] == [2, 3]


async def test_records_gap_and_continues() -> None:
    source = ScriptedTickSource((_observation(1), _observation(5)))
    loop = SingleTenantTickLoop(_config())

    result = await loop.run(source, RecordingDecider(), RecordingSubmitter())

    assert result.ticks_processed == 2
    assert result.gap_ticks == 1
    assert result.last_tick == 5
    assert result.stopped_reason is StoppedReason.STREAM_ENDED


async def test_gap_stop_when_configured() -> None:
    source = ScriptedTickSource((_observation(1), _observation(5)))
    loop = SingleTenantTickLoop(replace(_config(), continue_on_gap=False))

    result = await loop.run(source, RecordingDecider(), RecordingSubmitter())

    assert result.ticks_processed == 1
    assert result.gap_ticks == 1
    assert result.stopped_reason is StoppedReason.GAP
    assert [outcome.tick for outcome in result.outcomes] == [1]


async def test_stops_on_soft_deadline_before_decide() -> None:
    source = ScriptedTickSource((_observation(1), _observation(2)))
    decider = RecordingDecider()
    submitter = RecordingSubmitter()
    loop = SingleTenantTickLoop(replace(_config(), tick_budget=DeadlineBudget(0)))

    result = await loop.run(source, decider, submitter)

    assert result.ticks_processed == 0
    assert result.stopped_reason is StoppedReason.SOFT_DEADLINE
    assert decider.calls == []
    assert submitter.calls == []
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.tick == 1
    assert outcome.deadline_outcome is DeadlineOutcome.SOFT_DEADLINE
    assert outcome.submit_result is SubmitResult.NOT_SUBMITTED


async def test_selection_timeout_does_not_submit() -> None:
    clock = FakeClock()
    source = ScriptedTickSource((_observation(1), _observation(2)))
    submitter = RecordingSubmitter()
    loop = SingleTenantTickLoop(replace(_config(), clock=clock))
    overrun = DeadlineBudget.from_milliseconds(100).nanoseconds + 1

    result = await loop.run(source, _consuming_decider(clock, overrun), submitter)

    assert result.ticks_processed == 0
    assert result.stopped_reason is StoppedReason.SELECTION_TIMEOUT
    assert submitter.calls == []
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.tick == 1
    assert outcome.deadline_outcome is DeadlineOutcome.SELECTION_TIMEOUT
    assert outcome.submit_result is SubmitResult.NOT_SUBMITTED


async def test_selection_timeout_continues_when_tolerated() -> None:
    clock = FakeClock()
    source = ScriptedTickSource((_observation(1), _observation(2), _observation(3)))
    submitter = RecordingSubmitter()
    overrun = DeadlineBudget.from_milliseconds(100).nanoseconds + 1

    def decider(observation, budget):
        if observation.tick == 2:
            clock.advance(overrun)
        return Decision(tick=observation.tick)

    loop = SingleTenantTickLoop(replace(_config(), clock=clock, continue_on_selection_timeout=True))
    result = await loop.run(source, decider, submitter)

    assert result.stopped_reason is StoppedReason.STREAM_ENDED
    assert result.ticks_processed == 2
    assert [o.deadline_outcome for o in result.outcomes] == [
        DeadlineOutcome.CANDIDATE,
        DeadlineOutcome.SELECTION_TIMEOUT,
        DeadlineOutcome.CANDIDATE,
    ]
    assert result.outcomes[1].submit_result is SubmitResult.NOT_SUBMITTED
    assert len(submitter.calls) == 2


def test_continue_on_selection_timeout_must_be_bool() -> None:
    with pytest.raises(TypeError):
        replace(_config(), continue_on_selection_timeout="yes")  # type: ignore[arg-type]


async def test_reconnects_and_resumes_from_last_tick() -> None:
    source = ScriptedTickSource(
        (_observation(1), _observation(2), _Raise(RuntimeError("stream reset"))),
        (_observation(2), _observation(3)),
    )
    decider = RecordingDecider()
    loop = SingleTenantTickLoop(_config())

    result = await loop.run(source, decider, RecordingSubmitter())

    assert source.stream_calls == 2
    assert result.reconnect_count == 1
    assert result.ticks_processed == 3
    assert result.duplicate_ticks == 1
    assert result.last_tick == 3
    assert [obs.tick for obs, _ in decider.calls] == [1, 2, 3]
    assert [outcome.tick for outcome in result.outcomes] == [1, 2, 3]
    assert all(stream.aclose_calls == 1 for stream in source.streams)


async def test_reconnect_exhaustion_raises() -> None:
    source = ScriptedTickSource(
        (_Raise(RuntimeError("boom")),),
        (_Raise(RuntimeError("boom")),),
        (_Raise(RuntimeError("boom")),),
    )
    loop = SingleTenantTickLoop(replace(_config(), max_reconnects=2))

    with pytest.raises(ReconnectLimitExceeded, match="2 reconnects"):
        await loop.run(source, RecordingDecider(), RecordingSubmitter())


async def test_cancel_propagates_and_closes_once() -> None:
    gate = BlockGate()
    source = ScriptedTickSource((_observation(1), gate))
    loop = SingleTenantTickLoop(_config())
    task = asyncio.create_task(loop.run(source, RecordingDecider(), RecordingSubmitter()))

    await gate.entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert sum(stream.aclose_calls for stream in source.streams) == 1


async def test_submit_error_policy_continue() -> None:
    submitter = RecordingSubmitter(
        SubmitOutcome(accepted=False, error="rate limited"),
        SubmitOutcome(accepted=True),
    )
    source = ScriptedTickSource((_observation(1), _observation(2)))
    loop = SingleTenantTickLoop(_config())

    result = await loop.run(source, RecordingDecider(), submitter)

    assert result.ticks_processed == 2
    assert result.stopped_reason is StoppedReason.STREAM_ENDED
    first, second = result.outcomes
    assert first.deadline_outcome is DeadlineOutcome.CANDIDATE
    assert first.submit_result is SubmitResult.REJECTED
    assert first.submit_error == "rate limited"
    assert second.submit_result is SubmitResult.ACCEPTED


async def test_submit_error_policy_stop() -> None:
    submitter = RecordingSubmitter(SubmitOutcome(accepted=False, error="rate limited"))
    source = ScriptedTickSource((_observation(1), _observation(2)))
    loop = SingleTenantTickLoop(replace(_config(), submit_error_policy=SubmitErrorPolicy.STOP))

    result = await loop.run(source, RecordingDecider(), submitter)

    assert result.ticks_processed == 1
    assert result.stopped_reason is StoppedReason.SUBMIT_FAILURE
    assert result.outcomes[0].submit_result is SubmitResult.REJECTED
    assert result.outcomes[0].submit_error == "rate limited"


def test_config_rejects_invalid_max_reconnects() -> None:
    with pytest.raises(ValueError):
        replace(_config(), max_reconnects=-1)


def test_submit_outcome_rejects_ambiguous_values() -> None:
    with pytest.raises(ValueError):
        SubmitOutcome(accepted=True, error="unexpected")
    with pytest.raises(ValueError):
        SubmitOutcome(accepted=False)


def test_tick_result_rejects_inconsistent_submit_state() -> None:
    with pytest.raises(ValueError):
        TickResult(
            tick=1,
            decision_id=DecisionId("decision:unused"),
            deadline_outcome=DeadlineOutcome.SOFT_DEADLINE,
            submit_result=SubmitResult.ACCEPTED,
        )
    with pytest.raises(ValueError):
        TickResult(
            tick=1,
            decision_id=DecisionId("decision:unused"),
            deadline_outcome=DeadlineOutcome.CANDIDATE,
            submit_result=SubmitResult.NOT_SUBMITTED,
        )
    with pytest.raises(ValueError):
        TickResult(
            tick=1,
            decision_id=DecisionId("decision:unused"),
            deadline_outcome=DeadlineOutcome.CANDIDATE,
            submit_result=SubmitResult.ACCEPTED,
            submit_error="unexpected",
        )


async def test_stream_ended_reopens_when_configured() -> None:
    source = ScriptedTickSource(
        (_observation(1), _observation(2)),
        (_observation(3), _observation(4)),
    )
    decider = RecordingDecider()
    loop = SingleTenantTickLoop(replace(_config(), continue_on_stream_ended=True))

    result = await loop.run(source, decider, RecordingSubmitter())

    assert result.ticks_processed == 4
    assert result.last_tick == 4
    # The reopened stream ends and empty reopen attempts consume the bounded
    # reconnect budget too (fail-safe against an immediately-ending source).
    assert result.reconnect_count == 3
    assert result.stopped_reason is StoppedReason.STREAM_ENDED
    assert [obs.tick for obs, _ in decider.calls] == [1, 2, 3, 4]
    assert source.stream_calls == 4
    assert [outcome.tick for outcome in result.outcomes] == [1, 2, 3, 4]


async def test_stream_ended_reopen_respects_max_reconnects() -> None:
    source = ScriptedTickSource(
        (_observation(1),),
        (_observation(2),),
        (_observation(3),),
        (_observation(4),),
        (_observation(5),),
    )
    loop = SingleTenantTickLoop(replace(_config(), continue_on_stream_ended=True, max_reconnects=2))

    result = await loop.run(source, RecordingDecider(), RecordingSubmitter())

    assert result.ticks_processed == 3
    assert result.last_tick == 3
    assert result.reconnect_count == 2
    assert result.stopped_reason is StoppedReason.STREAM_ENDED
    assert source.stream_calls == 3


async def test_stream_ended_reopen_dedupes_replayed_tail() -> None:
    source = ScriptedTickSource(
        (_observation(1), _observation(2)),
        (_observation(2), _observation(3)),
    )
    loop = SingleTenantTickLoop(replace(_config(), continue_on_stream_ended=True))

    result = await loop.run(source, RecordingDecider(), RecordingSubmitter())

    assert result.ticks_processed == 3
    assert result.duplicate_ticks == 1
    assert result.last_tick == 3
    assert result.reconnect_count == 3


def test_continue_on_stream_ended_must_be_bool() -> None:
    with pytest.raises(TypeError, match="continue_on_stream_ended"):
        replace(_config(), continue_on_stream_ended="yes")  # type: ignore[arg-type]


class HangingSubmitter:
    """Submitter that never resolves until cancelled."""

    def __init__(self) -> None:
        self.calls: list[tuple[Decision, TurnObservation]] = []

    async def __call__(self, decision: Decision, observation: TurnObservation) -> SubmitOutcome:
        self.calls.append((decision, observation))
        await asyncio.sleep(60.0)
        return SubmitOutcome(accepted=True)


class RaisingSubmitter:
    """Submitter that raises a non-SDK error (fail-closed)."""

    async def __call__(self, decision: Decision, observation: TurnObservation) -> SubmitOutcome:
        raise RuntimeError("submit exploded")


def _raising_decider(observation: TurnObservation, budget: DeadlineBudget) -> Decision:
    raise RuntimeError("decide exploded")


async def test_submit_timeout_records_rejected_and_continues() -> None:
    source = ScriptedTickSource((_observation(1), _observation(2)))
    submitter = HangingSubmitter()
    loop = SingleTenantTickLoop(replace(_config(), submit_timeout_seconds=0.01))

    result = await loop.run(source, RecordingDecider(), submitter)

    assert result.ticks_processed == 2
    assert result.last_tick == 2
    assert result.stopped_reason is StoppedReason.STREAM_ENDED
    assert len(submitter.calls) == 2
    assert [o.submit_result for o in result.outcomes] == [
        SubmitResult.REJECTED,
        SubmitResult.REJECTED,
    ]
    assert [o.submit_error for o in result.outcomes] == ["submit timed out", "submit timed out"]
    assert [o.deadline_outcome for o in result.outcomes] == [
        DeadlineOutcome.CANDIDATE,
        DeadlineOutcome.CANDIDATE,
    ]


async def test_submit_timeout_respects_stop_policy() -> None:
    source = ScriptedTickSource((_observation(1), _observation(2)))
    submitter = HangingSubmitter()
    loop = SingleTenantTickLoop(
        replace(
            _config(),
            submit_timeout_seconds=0.01,
            submit_error_policy=SubmitErrorPolicy.STOP,
        )
    )

    result = await loop.run(source, RecordingDecider(), submitter)

    assert result.ticks_processed == 1
    assert result.last_tick == 1
    assert result.stopped_reason is StoppedReason.SUBMIT_FAILURE
    assert len(result.outcomes) == 1
    assert result.outcomes[0].submit_error == "submit timed out"


async def test_decide_within_budget_still_submits() -> None:
    """Positive timing-margin guard: 90% of the 100ms budget still submits."""
    clock = FakeClock()
    source = ScriptedTickSource((_observation(1),))
    submitter = RecordingSubmitter()
    loop = SingleTenantTickLoop(replace(_config(), clock=clock))

    result = await loop.run(source, _consuming_decider(clock, nanoseconds=90_000_000), submitter)

    assert result.ticks_processed == 1
    assert result.outcomes[0].deadline_outcome is DeadlineOutcome.CANDIDATE
    assert result.outcomes[0].submit_result is SubmitResult.ACCEPTED


async def test_decide_exception_propagates_fail_closed() -> None:
    source = ScriptedTickSource((_observation(1),))

    with pytest.raises(RuntimeError, match="decide exploded"):
        await SingleTenantTickLoop(_config()).run(source, _raising_decider, RecordingSubmitter())


async def test_submit_exception_propagates_fail_closed() -> None:
    source = ScriptedTickSource((_observation(1),))

    with pytest.raises(RuntimeError, match="submit exploded"):
        await SingleTenantTickLoop(replace(_config(), submit_timeout_seconds=0.01)).run(
            source, RecordingDecider(), RaisingSubmitter()
        )


def test_submit_timeout_requires_positive_or_none() -> None:
    for bad in (0, -1.0, True):
        with pytest.raises(ValueError):
            replace(_config(), submit_timeout_seconds=bad)
