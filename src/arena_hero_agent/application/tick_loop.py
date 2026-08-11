"""Single-tenant offline tick loop with deterministic deadline accounting.

P4-4 implements the minimal closed loop for one tenant: consume a stream of
``TurnObservation`` values, decide once per tick within an explicit per-tick
deadline budget, submit the resulting decision, and recover from stream
failures by reopening the source and resuming from the last observed tick.

Design constraints:

- SDK-free: this module depends only on domain value objects, application
  DTOs, and the ``Clock`` port. It never imports adapters, filesystem, or HTTP.
- No telemetry emission here (the P4-6 slice wires results to a sink).
- No real strategy here (the P4-8 slice supplies deciders); ``decide`` and
  ``submit`` are injected boundaries and must be deterministic per tick.
- Deadline and submit outcome vocabulary matches the values already fixed by
  the telemetry schema (``candidate`` / ``soft_deadline`` /
  ``selection_timeout`` / ``not_applicable`` / ``error`` and ``accepted`` /
  ``rejected`` / ``not_submitted``). This module intentionally does not import
  the telemetry package.
- Deadline accounting is per tick: each tick starts with a fresh copy of the
  configured budget. A tick whose budget is exhausted before decision stops
  with ``soft_deadline`` without submitting. A tick whose decision exceeds the
  budget stops with ``selection_timeout`` and is never submitted. A submission
  that already completed records its actual outcome because an in-flight
  submission cannot be retracted.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from arena_hero_agent.domain import DeadlineBudget, DecisionId, StateDigest, TenantId
from arena_hero_agent.ports import Clock

from .turns import Decision, TurnObservation


class DeadlineOutcome(StrEnum):
    """Per-tick deadline outcome, matching the telemetry schema vocabulary."""

    CANDIDATE = "candidate"
    SOFT_DEADLINE = "soft_deadline"
    SELECTION_TIMEOUT = "selection_timeout"
    NOT_APPLICABLE = "not_applicable"
    ERROR = "error"


class SubmitResult(StrEnum):
    """Per-tick submission result, matching the telemetry schema vocabulary."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NOT_SUBMITTED = "not_submitted"


class SubmitErrorPolicy(StrEnum):
    """How a rejected submission is handled by the loop."""

    CONTINUE = "continue"
    STOP = "stop"


class StoppedReason(StrEnum):
    """Why the loop returned without an exception."""

    STREAM_ENDED = "stream_ended"
    SOFT_DEADLINE = "soft_deadline"
    SELECTION_TIMEOUT = "selection_timeout"
    GAP = "gap"
    SUBMIT_FAILURE = "submit_failure"


class ReconnectLimitExceeded(RuntimeError):
    """Raised when a tick source keeps failing past the configured retry bound."""


class TurnStream(Protocol):
    """Async iterator over turns that can also be closed best-effort."""

    def __aiter__(self) -> AsyncIterator[TurnObservation]: ...

    async def __anext__(self) -> TurnObservation: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class TickSource(Protocol):
    """Reopenable source of application turn observations.

    Each call to :meth:`stream` returns a fresh iterator. The loop reopens the
    source after a stream failure and resumes from the last observed tick, so
    an iterator may replay already-seen ticks.
    """

    def stream(self) -> TurnStream:
        """Return a fresh iterator over observed turns."""
        ...


class SystemClock:
    """Monotonic process-local clock backed by ``time.monotonic_ns``."""

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def exponential_backoff(
    attempt: int,
    *,
    base_seconds: float = 0.25,
    max_seconds: float = 5.0,
) -> float:
    """Return the seconds to wait before the given 1-based retry attempt."""
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise TypeError("attempt must be an integer")
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    return min(base_seconds * (2 ** (attempt - 1)), max_seconds)


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    """Result of one injected submission."""

    accepted: bool
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted must be a boolean")
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("error must be a string or None")
        if self.accepted and self.error is not None:
            raise ValueError("accepted outcomes cannot carry an error")
        if not self.accepted and self.error is None:
            raise ValueError("rejected outcomes require an error message")


Decider = Callable[[TurnObservation, DeadlineBudget], Decision]
Submitter = Callable[[Decision, TurnObservation], Awaitable[SubmitOutcome]]
Backoff = Callable[[int], float]


@dataclass(frozen=True, slots=True)
class TickResult:
    """Outcome of one processed tick; skipped ticks are only counted."""

    tick: int
    decision_id: DecisionId
    deadline_outcome: DeadlineOutcome
    submit_result: SubmitResult = SubmitResult.NOT_SUBMITTED
    submit_error: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.tick, bool) or not isinstance(self.tick, int):
            raise TypeError("tick must be an integer")
        if self.tick < 1:
            raise ValueError("tick must be at least 1")
        if not isinstance(self.decision_id, DecisionId):
            raise TypeError("decision_id must be a DecisionId")
        if not isinstance(self.deadline_outcome, DeadlineOutcome):
            raise TypeError("deadline_outcome must be a DeadlineOutcome")
        if not isinstance(self.submit_result, SubmitResult):
            raise TypeError("submit_result must be a SubmitResult")
        if self.submit_error is not None and not isinstance(self.submit_error, str):
            raise TypeError("submit_error must be a string or None")
        if self.deadline_outcome is DeadlineOutcome.CANDIDATE:
            if self.submit_result is SubmitResult.NOT_SUBMITTED:
                raise ValueError("candidate ticks must record a submission result")
        elif self.submit_result is not SubmitResult.NOT_SUBMITTED:
            raise ValueError("non-candidate ticks cannot record a submission result")
        if self.submit_result is not SubmitResult.REJECTED and self.submit_error is not None:
            raise ValueError("only rejected ticks may carry submit_error")


@dataclass(frozen=True, slots=True)
class TickLoopResult:
    """Summary of one tick loop run."""

    tenant_id: TenantId
    last_tick: int
    ticks_processed: int
    duplicate_ticks: int
    out_of_order_ticks: int
    gap_ticks: int
    reconnect_count: int
    stopped_reason: StoppedReason
    outcomes: tuple[TickResult, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("tenant_id must be a TenantId")
        for name in (
            "last_tick",
            "ticks_processed",
            "duplicate_ticks",
            "out_of_order_ticks",
            "gap_ticks",
            "reconnect_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if not isinstance(self.stopped_reason, StoppedReason):
            raise TypeError("stopped_reason must be a StoppedReason")
        if isinstance(self.outcomes, str) or not isinstance(self.outcomes, tuple):
            raise TypeError("outcomes must be a tuple of TickResult")
        if any(not isinstance(outcome, TickResult) for outcome in self.outcomes):
            raise TypeError("outcomes must contain only TickResult values")


@dataclass(frozen=True, slots=True)
class TickLoopConfig:
    """Configuration for one single-tenant tick loop run."""

    tenant_id: TenantId
    tick_budget: DeadlineBudget
    max_reconnects: int = 3
    continue_on_gap: bool = True
    submit_error_policy: SubmitErrorPolicy = SubmitErrorPolicy.CONTINUE
    backoff: Backoff = field(default_factory=lambda: exponential_backoff)
    clock: Clock = field(default_factory=SystemClock)

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("tenant_id must be a TenantId")
        if not isinstance(self.tick_budget, DeadlineBudget):
            raise TypeError("tick_budget must be a DeadlineBudget")
        if isinstance(self.max_reconnects, bool) or not isinstance(self.max_reconnects, int):
            raise TypeError("max_reconnects must be an integer")
        if self.max_reconnects < 0:
            raise ValueError("max_reconnects cannot be negative")
        if not isinstance(self.continue_on_gap, bool):
            raise TypeError("continue_on_gap must be a boolean")
        if not isinstance(self.submit_error_policy, SubmitErrorPolicy):
            raise TypeError("submit_error_policy must be a SubmitErrorPolicy")


async def _close_iterator(iterator: TurnStream) -> None:
    """Best-effort close that tolerates already-terminated iterators."""
    try:
        await iterator.aclose()
    except RuntimeError:
        return


class SingleTenantTickLoop:
    """Drive one tenant's offline tick stream within explicit deadlines."""

    def __init__(self, config: TickLoopConfig) -> None:
        self._config = config

    async def run(
        self,
        source: TickSource,
        decide: Decider,
        submit: Submitter,
    ) -> TickLoopResult:
        """Consume the source until it ends, a deadline fires, or a stop policy applies.

        ``CancelledError`` always propagates; the active stream is closed
        exactly once before the exception leaves this method.
        """
        config = self._config
        last_tick = 0
        ticks_processed = 0
        duplicate_ticks = 0
        out_of_order_ticks = 0
        gap_ticks = 0
        reconnect_count = 0
        outcomes: list[TickResult] = []
        stopped_reason = StoppedReason.STREAM_ENDED

        stream = source.stream()
        stream_open = True

        async def close_active_stream() -> None:
            nonlocal stream_open
            if stream_open:
                stream_open = False
                await _close_iterator(stream)

        try:
            while True:
                try:
                    observation = await stream.__anext__()
                except StopAsyncIteration:
                    stopped_reason = StoppedReason.STREAM_ENDED
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if reconnect_count >= config.max_reconnects:
                        raise ReconnectLimitExceeded(
                            f"tick source failed after {reconnect_count} reconnects"
                        ) from error
                    reconnect_count += 1
                    delay = config.backoff(reconnect_count)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    await _close_iterator(stream)
                    stream = source.stream()
                    stream_open = True
                    continue

                tick = observation.tick
                if tick < last_tick:
                    out_of_order_ticks += 1
                    continue
                if tick == last_tick:
                    duplicate_ticks += 1
                    continue
                if tick > last_tick + 1:
                    gap_ticks += 1
                    if not config.continue_on_gap:
                        stopped_reason = StoppedReason.GAP
                        break

                decision_id = DecisionId.from_deterministic_input(
                    (config.tenant_id, tick, StateDigest.from_state(observation))
                )
                budget = DeadlineBudget(config.tick_budget.nanoseconds)
                if budget.exhausted:
                    stopped_reason = StoppedReason.SOFT_DEADLINE
                    outcomes.append(
                        TickResult(
                            tick=tick,
                            decision_id=decision_id,
                            deadline_outcome=DeadlineOutcome.SOFT_DEADLINE,
                            submit_result=SubmitResult.NOT_SUBMITTED,
                        )
                    )
                    break

                started = config.clock.monotonic_ns()
                decision = decide(observation, budget)
                remaining = budget.consume(config.clock.monotonic_ns() - started)
                if remaining.exhausted:
                    stopped_reason = StoppedReason.SELECTION_TIMEOUT
                    outcomes.append(
                        TickResult(
                            tick=tick,
                            decision_id=decision_id,
                            deadline_outcome=DeadlineOutcome.SELECTION_TIMEOUT,
                            submit_result=SubmitResult.NOT_SUBMITTED,
                        )
                    )
                    break

                outcome = await submit(decision, observation)
                if outcome.accepted:
                    submit_result = SubmitResult.ACCEPTED
                    submit_error: str | None = None
                else:
                    submit_result = SubmitResult.REJECTED
                    submit_error = outcome.error
                outcomes.append(
                    TickResult(
                        tick=tick,
                        decision_id=decision_id,
                        deadline_outcome=DeadlineOutcome.CANDIDATE,
                        submit_result=submit_result,
                        submit_error=submit_error,
                    )
                )
                last_tick = tick
                ticks_processed += 1
                if (
                    submit_result is SubmitResult.REJECTED
                    and config.submit_error_policy is SubmitErrorPolicy.STOP
                ):
                    stopped_reason = StoppedReason.SUBMIT_FAILURE
                    break
        finally:
            await close_active_stream()

        return TickLoopResult(
            tenant_id=config.tenant_id,
            last_tick=last_tick,
            ticks_processed=ticks_processed,
            duplicate_ticks=duplicate_ticks,
            out_of_order_ticks=out_of_order_ticks,
            gap_ticks=gap_ticks,
            reconnect_count=reconnect_count,
            stopped_reason=stopped_reason,
            outcomes=tuple(outcomes),
        )
