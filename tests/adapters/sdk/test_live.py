"""Live turn source and submitter adapter tests (P4-21, no network)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import pytest
from arena_hero import (
    Accepted,
    AsyncGameEvent,
    AsyncTurn,
    ChampionBeacon,
    CommandPlan,
    CommandSource,
    CoreState,
    CoreView,
    PlayerState,
    PlayerStatus,
    Tick,
    UnitType,
    UnitView,
)

from arena_hero_agent.adapters.sdk import (
    LiveSubmitter,
    LiveTurnSource,
    SdkContractViolationError,
    SdkPermanentError,
    SdkRetryableError,
)
from arena_hero_agent.application import (
    Decision,
    PlayerLifecycle,
    TurnObservation,
)
from arena_hero_agent.domain import (
    CURRENT_RULES_VERSION,
    DecisionId,
    TenantId,
    WorldProjection,
)

WORKER_ID = "aaaaaaaa-0000-0000-0000-000000000001"
CORE_ID = "cccccccc-0000-0000-0000-000000000004"
TENANT = TenantId("t4")


async def _noop_submit(plan: CommandPlan, idempotency_key: str | None = None) -> Accepted:
    raise AssertionError("unit tests never submit through the SDK callback")


def _unit(
    *,
    identifier: str = WORKER_ID,
    x: int = 0,
    y: int = 0,
    unit_type: UnitType = UnitType.WORKER,
    cargo: int = 1,
) -> UnitView:
    return UnitView(
        kind="UNIT",
        id=identifier,
        controlled=True,
        position=(x, y),
        hp=4,
        unit_type=unit_type,
        cargo=cargo,
    )


def _core(*, identifier: str = CORE_ID) -> CoreView:
    return CoreView(
        kind="CORE",
        id=identifier,
        controlled=True,
        owner_username="player",
        position=(0, 0),
        hp=10,
        shield=4,
        state=CoreState.NORMAL,
    )


def _state(*, tick: int = 1, resources: int = 3) -> PlayerState:
    return PlayerState(
        status=PlayerStatus.ACTIVE,
        resources=resources,
        population=1,
        champion_beacon=ChampionBeacon(position=(1, 2)),
        objects=(_unit(), _core()),
        events=(),
    )


def _turn(*, tick: int = 1, resources: int = 3) -> AsyncTurn:
    return AsyncTurn(
        tick=tick, state=_state(tick=tick, resources=resources), submitter=_noop_submit
    )


def _events(*items: object) -> AsyncIterator[AsyncGameEvent]:
    async def iterate() -> AsyncIterator[AsyncGameEvent]:
        for item in items:
            yield cast(AsyncGameEvent, item)

    return iterate()


class FakeGameClient:
    """Minimal ``GameClient`` port double recording submits and closes."""

    def __init__(
        self,
        *,
        items: tuple[object, ...] = (),
        submit_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.items = items
        self.submit_error = submit_error
        self.close_error = close_error
        self.submissions: list[tuple[CommandPlan, DecisionId]] = []
        self.close_calls = 0

    def events(self) -> AsyncIterator[AsyncGameEvent]:
        return _events(*self.items)

    async def submit(self, plan: CommandPlan, *, decision_id: DecisionId) -> Accepted:
        self.submissions.append((plan, decision_id))
        if self.submit_error is not None:
            raise self.submit_error
        return Accepted(
            accepted=True,
            tick=plan.tick,
            source=CommandSource.AGENT,
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _observation(*, tick: int = 1, resources: int = 3) -> TurnObservation:
    projection = WorldProjection(
        tick=tick,
        rules_version=CURRENT_RULES_VERSION,
        core=None,
        units=(),
        entities=(),
        resources=(),
        terrain=(),
        beacon=None,
    )
    return TurnObservation(
        tick=tick,
        lifecycle=PlayerLifecycle.ACTIVE,
        resources=resources,
        population=1,
        projection=projection,
    )


def _decision(*, tick: int = 1) -> Decision:
    return Decision(
        tick=tick,
        unit_intents=(),
        core_intent=None,
    )


async def test_live_turn_source_skips_non_turn_events() -> None:
    source = LiveTurnSource(FakeGameClient(items=(Tick(tick=1), _turn(tick=1))))
    stream = source.stream()
    observations = [observation async for observation in stream]
    assert [observation.tick for observation in observations] == [1]
    assert [observation.resources for observation in observations] == [3]


async def test_live_turn_source_emits_only_async_turns_in_order() -> None:
    items: tuple[object, ...] = (
        Tick(tick=1),
        _turn(tick=1, resources=3),
        _turn(tick=2, resources=4),
        cast(object, "noise"),
    )
    source = LiveTurnSource(FakeGameClient(items=items))
    observations = [observation async for observation in source.stream()]
    assert [observation.tick for observation in observations] == [1, 2]
    assert [observation.resources for observation in observations] == [3, 4]


def test_live_turn_source_closed_after_close() -> None:
    source = LiveTurnSource(FakeGameClient(items=(_turn(tick=1),)))
    assert source.closed is False
    source.close()
    assert source.closed is True
    with pytest.raises(RuntimeError, match="live source is closed"):
        source.stream()


async def test_live_turn_stream_aclose_is_best_effort() -> None:
    client = FakeGameClient(items=(_turn(tick=1),))
    stream = LiveTurnSource(client).stream()
    await stream.aclose()
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


async def test_live_submitter_uses_deterministic_decision_id() -> None:
    client = FakeGameClient()
    submitter = LiveSubmitter(client, tenant_id=TENANT)
    outcome = await submitter(_decision(tick=3), _observation(tick=3, resources=7))

    assert outcome.accepted is True
    assert len(client.submissions) == 1
    plan, decision_id = client.submissions[0]
    assert plan.tick == 3
    assert decision_id.value.startswith("decision:")


async def test_live_submitter_translates_sdk_failures_to_rejected() -> None:
    cases = (
        (SdkRetryableError("submit", "boom"), "retryable submit failure"),
        (SdkPermanentError("submit", "boom"), "permanent submit failure"),
        (SdkContractViolationError("submit", "boom"), "submit contract violation"),
    )
    for error, expected in cases:
        client = FakeGameClient(submit_error=error)
        submitter = LiveSubmitter(client, tenant_id=TENANT)
        outcome = await submitter(_decision(), _observation())
        assert outcome.accepted is False
        assert outcome.error == expected


def test_live_submitter_rejects_non_tenant() -> None:
    with pytest.raises(TypeError, match="tenant_id must be a TenantId"):
        LiveSubmitter(FakeGameClient(), tenant_id="t4")  # type: ignore


def test_live_turn_source_structural_tick_source_check() -> None:
    from arena_hero_agent.application.tick_loop import TickSource

    source = LiveTurnSource(FakeGameClient(items=(_turn(tick=1),)))
    assert isinstance(source, TickSource)
