"""Live SDK adapters: a reopenable turn source and a deterministic submitter.

P4-21 bridges the application tick loop to the SDK game client without network
access here: both adapters are pure composition over the already-validated
``GameClient`` port and the strict turn/plan adapters.

- :class:`LiveTurnSource` implements the ``TickSource`` protocol: every
  ``AsyncTurn`` from the client stream is adapted through
  :func:`adapt_async_turn`; non-turn events are skipped. Reopening the stream
  re-subscribes to ``client.events()`` so the tick loop can reconnect and
  resume from its last tick.
- :class:`LiveSubmitter` implements the tick loop ``Submitter`` boundary: it
  builds the SDK ``CommandPlan`` from the decision and submits it with the
  deterministic ``DecisionId`` idempotency key, translating SDK failures into
  rejected outcomes with safe, path-free summaries.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from arena_hero_agent.application import (
    Decision,
    SubmitOutcome,
    TurnObservation,
)
from arena_hero_agent.application.tick_loop import TurnStream
from arena_hero_agent.domain import DecisionId, StateDigest, TenantId
from arena_hero_agent.ports import GameClient

from .errors import SdkContractViolationError, SdkPermanentError, SdkRetryableError
from .plans import build_command_plan
from .turns import adapt_async_turn


class LiveTurnSource:
    """Reopenable tick source over one game client's event stream.

    The source is bound to exactly one client; ``stream()`` returns a fresh
    iterator so the tick loop can reopen after a stream failure and resume
    from its last observed tick.
    """

    def __init__(self, client: GameClient) -> None:
        self._client = client
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def stream(self) -> TurnStream:
        if self._closed:
            raise RuntimeError("live source is closed")
        return _LiveTurnStream(self._client)

    def close(self) -> None:
        self._closed = True


class _LiveTurnStream:
    """One subscription over the client's event iterator."""

    def __init__(self, client: GameClient) -> None:
        from arena_hero import AsyncTurn

        self._events = client.events()
        self._turn_type = AsyncTurn
        self._closed = False

    def __aiter__(self) -> AsyncIterator[TurnObservation]:
        return self

    async def __anext__(self) -> TurnObservation:
        if self._closed:
            raise StopAsyncIteration
        async for event in self._events:
            if isinstance(event, self._turn_type):
                return adapt_async_turn(event)
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self._closed = True
        close = getattr(self._events, "aclose", None)
        if close is None:
            return
        try:
            await close()
        except RuntimeError:
            return


class LiveSubmitter:
    """Submit one decision through the client using a deterministic idempotency key."""

    def __init__(self, client: GameClient, *, tenant_id: TenantId) -> None:
        if not isinstance(tenant_id, TenantId):
            raise TypeError("tenant_id must be a TenantId")
        self._client = client
        self._tenant_id = tenant_id

    async def __call__(
        self,
        decision: Decision,
        observation: TurnObservation,
    ) -> SubmitOutcome:
        plan = build_command_plan(decision, observation)
        decision_id = DecisionId.from_deterministic_input(
            (self._tenant_id, decision.tick, StateDigest.from_state(observation))
        )
        try:
            await self._client.submit(plan, decision_id=decision_id)
        except SdkRetryableError:
            return SubmitOutcome(accepted=False, error="retryable submit failure")
        except SdkPermanentError:
            return SubmitOutcome(accepted=False, error="permanent submit failure")
        except SdkContractViolationError:
            return SubmitOutcome(accepted=False, error="submit contract violation")
        return SubmitOutcome(accepted=True)


__all__ = ["LiveSubmitter", "LiveTurnSource"]
