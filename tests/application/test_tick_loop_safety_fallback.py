"""Safety fallback behavior for an over-budget strategy decision."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import cast

from arena_hero_agent.application import (
    CoreAction,
    CoreIntent,
    DeadlineOutcome,
    Decision,
    PlayerLifecycle,
    SingleTenantTickLoop,
    SubmitOutcome,
    SubmitResult,
    TickLoopConfig,
    TurnObservation,
)
from arena_hero_agent.domain import DeadlineBudget, RulesVersion, TenantId, WorldProjection


def _observation(tick: int) -> TurnObservation:
    return TurnObservation(
        tick=tick,
        lifecycle=PlayerLifecycle.ACTIVE,
        resources=1,
        population=1,
        projection=WorldProjection(tick=tick, rules_version=RulesVersion.V0_14),
    )


class _Stream:
    def __init__(self, observations: Sequence[TurnObservation]) -> None:
        self._observations = iter(observations)

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> TurnObservation:
        try:
            return next(self._observations)
        except StopIteration as error:
            raise StopAsyncIteration from error

    async def aclose(self) -> None:
        return None


class _Source:
    def __init__(self, observations: Sequence[TurnObservation]) -> None:
        self._observations = observations

    def stream(self) -> _Stream:
        return _Stream(self._observations)


class _Clock:
    def __init__(self) -> None:
        self.now = 0

    def monotonic_ns(self) -> int:
        return self.now


class _OverrunningDecider:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.fallback_calls = 0

    def __call__(self, observation: TurnObservation, budget: DeadlineBudget) -> Decision:
        del budget
        self.clock.now += 101_000_000
        return Decision(tick=observation.tick)

    def safety_fallback(self, observation: TurnObservation) -> Decision:
        self.fallback_calls += 1
        return Decision(
            tick=observation.tick,
            core_intent=CoreIntent(action=CoreAction.WAIT),
        )


class _Submitter:
    def __init__(self) -> None:
        self.calls: list[Decision] = []

    async def __call__(self, decision: Decision, observation: TurnObservation) -> SubmitOutcome:
        del observation
        self.calls.append(decision)
        return SubmitOutcome(accepted=True)


async def test_overrun_submits_safety_fallback_instead_of_dropping_tick() -> None:
    clock = _Clock()
    decider = _OverrunningDecider(clock)
    submitter = _Submitter()
    config = TickLoopConfig(
        tenant_id=TenantId("tenant-a"),
        tick_budget=DeadlineBudget.from_milliseconds(100),
        clock=clock,
        continue_on_selection_timeout=True,
        backoff=lambda _attempt: 0.0,
    )

    result = await SingleTenantTickLoop(config).run(
        _Source((_observation(1),)),
        cast(Callable, decider),
        submitter,
    )

    assert decider.fallback_calls == 1
    assert len(submitter.calls) == 1
    assert result.ticks_processed == 1
    assert result.outcomes[0].deadline_outcome is DeadlineOutcome.SELECTION_TIMEOUT
    assert result.outcomes[0].submit_result is SubmitResult.ACCEPTED
    assert result.outcomes[0].selection_latency_ms == 101.0
