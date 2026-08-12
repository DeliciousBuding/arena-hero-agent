"""Strict SDK AsyncTurn to application TurnObservation adaptation tests."""

from __future__ import annotations

import pytest
from arena_hero import (
    Accepted,
    AsyncTurn,
    ChampionBeacon,
    CommandPlan,
    CoreState,
    CoreView,
    PlayerState,
    PlayerStatus,
    ResolutionEvent,
    TerrainView,
    Tick,
    UnitType,
    UnitView,
)

import arena_hero_agent.adapters.sdk.turns as turns_module
from arena_hero_agent.adapters.sdk import SdkContractViolationError, adapt_async_turn
from arena_hero_agent.application import PlayerLifecycle
from arena_hero_agent.domain import BeaconStatus, EntityKind, TerrainState, UnitRole

from ._sdk_fakes import FuturePlayerStatus, FutureUnitType, fake_arena_hero_module

WORKER_ID = "aaaaaaaa-0000-0000-0000-000000000001"
RANGER_ID = "aaaaaaaa-0000-0000-0000-000000000002"
ENEMY_ID = "bbbbbbbb-0000-0000-0000-000000000003"
CORE_ID = "cccccccc-0000-0000-0000-000000000004"
EVENT_ID = "dddddddd-0000-0000-0000-000000000005"


class _UnknownObject:
    """An SDK-shaped object the adapter does not recognize, safe for Turn setup."""

    controlled = False


async def _noop_submit(plan: CommandPlan, idempotency_key: str | None = None) -> Accepted:
    raise AssertionError("offline adapter tests never submit")


def _unit(
    *,
    identifier: str,
    controlled: bool = True,
    x: int = 0,
    y: int = 0,
    unit_type: UnitType = UnitType.WORKER,
    hp: int = 4,
    cargo: int | None = None,
) -> UnitView:
    return UnitView(
        kind="UNIT",
        id=identifier,
        controlled=controlled,
        position=(x, y),
        hp=hp,
        unit_type=unit_type,
        cargo=cargo,
    )


def _core(
    *,
    identifier: str = CORE_ID,
    state: CoreState = CoreState.NORMAL,
    x: int = 0,
    y: int = 0,
) -> CoreView:
    return CoreView(
        kind="CORE",
        id=identifier,
        controlled=True,
        owner_username="player",
        position=(x, y),
        hp=10,
        shield=4,
        state=state,
    )


def _state(*, status: PlayerStatus = PlayerStatus.ACTIVE, **overrides: object) -> PlayerState:
    base: dict[str, object] = dict(
        status=status,
        resources=3,
        population=2,
        champion_beacon=ChampionBeacon(position=(1, 2)),
        objects=(),
        events=(),
    )
    base.update(overrides)
    return PlayerState(**base)  # type: ignore


def _turn(state: PlayerState, *, tick: int = 1) -> AsyncTurn:
    return AsyncTurn(tick=tick, state=state, submitter=_noop_submit)


def test_adapts_full_turn_into_observation() -> None:
    state = _state(
        objects=(
            _unit(identifier=WORKER_ID, x=0, y=0, cargo=1),
            _unit(identifier=RANGER_ID, x=2, y=1, unit_type=UnitType.RANGER, hp=3),
            _unit(
                identifier=ENEMY_ID,
                controlled=False,
                x=3,
                y=1,
                unit_type=UnitType.VANGUARD,
                hp=2,
            ),
            _core(),
            TerrainView(kind="OBSTACLE", positions=((4, 4), (5, 5))),
            TerrainView(kind="RESOURCE", positions=((6, 6),)),
        ),
        events=(
            ResolutionEvent(
                event_id=EVENT_ID,
                tick=1,
                event_type="HARVEST_SUCCEEDED",
                reason_code="OK",
                actor_id=WORKER_ID,
            ),
        ),
    )
    observation = adapt_async_turn(_turn(state, tick=1))

    assert observation.tick == 1
    assert observation.lifecycle is PlayerLifecycle.ACTIVE
    assert observation.resources == 3
    assert observation.population == 2

    units = {unit.id.value: unit for unit in observation.projection.units}
    assert units[WORKER_ID].role is UnitRole.WORKER
    assert units[WORKER_ID].cargo == 1
    assert units[RANGER_ID].role is UnitRole.RANGER

    entities = {entity.id.value: entity for entity in observation.projection.entities}
    assert entities[ENEMY_ID].kind is EntityKind.UNIT
    assert entities[ENEMY_ID].unit_role is UnitRole.VANGUARD

    assert observation.projection.core is not None
    assert observation.projection.core.id.value == CORE_ID
    assert observation.projection.core.shield == 4
    assert observation.projection.core.state.value == "normal"

    resources = [resource.position.cell_key for resource in observation.projection.resources]
    assert resources == ["6,6"]
    terrain = {
        observation.position.cell_key: observation.state
        for observation in observation.projection.terrain
    }
    assert terrain["4,4"] is TerrainState.BLOCKED
    assert terrain["5,5"] is TerrainState.BLOCKED

    assert observation.projection.beacon is not None
    assert observation.projection.beacon.status is BeaconStatus.UNKNOWN
    assert observation.projection.beacon.position.cell_key == "1,2"

    assert len(observation.events) == 1
    assert observation.events[0].kind == "HARVEST_SUCCEEDED"


def test_adapts_carried_beacon_with_carrier() -> None:
    state = _state(
        champion_beacon=ChampionBeacon(
            position=(2, 2),
            status="CARRIED",
            carrier_id=WORKER_ID,
        ),
        objects=(_unit(identifier=WORKER_ID, x=2, y=2),),
    )
    observation = adapt_async_turn(_turn(state))
    assert observation.projection.beacon is not None
    assert observation.projection.beacon.status is BeaconStatus.CARRIED
    assert observation.projection.beacon.carrier_id is not None
    assert observation.projection.beacon.carrier_id.value == WORKER_ID


def test_adapts_respawning_lifecycle() -> None:
    state = _state(status=PlayerStatus.RESPAWNING, respawn_at_tick=5, objects=())
    observation = adapt_async_turn(_turn(state))
    assert observation.lifecycle is PlayerLifecycle.RESPAWNING
    assert observation.respawn_at_tick == 5


def test_rejects_non_async_turn_input() -> None:
    with pytest.raises(SdkContractViolationError, match="AsyncTurn"):
        adapt_async_turn(Tick(tick=1))


def test_rejects_missing_player_state() -> None:
    turn = _turn(_state())
    turn.state = object()  # type: ignore
    with pytest.raises(SdkContractViolationError, match="PlayerState"):
        adapt_async_turn(turn)


@pytest.mark.parametrize("tick", [0, -1, True])
def test_rejects_invalid_turn_tick(tick: object) -> None:
    turn = AsyncTurn(tick=tick, state=_state(), submitter=_noop_submit)  # type: ignore
    with pytest.raises(SdkContractViolationError, match="positive integer"):
        adapt_async_turn(turn)


def test_rejects_unknown_player_state_object() -> None:
    state = PlayerState.model_construct(
        status=PlayerStatus.ACTIVE,
        resources=0,
        population=0,
        champion_beacon=ChampionBeacon(position=(0, 0)),
        objects=(_UnknownObject(),),
        events=(),
    )
    with pytest.raises(SdkContractViolationError, match="unsupported PlayerState object"):
        adapt_async_turn(_turn(state))


def test_rejects_duplicate_object_identity() -> None:
    state = _state(
        objects=(
            _unit(identifier=WORKER_ID, x=0, y=0),
            _unit(identifier=WORKER_ID, x=1, y=1, hp=1),
        ),
    )
    with pytest.raises(SdkContractViolationError, match="duplicate object identity"):
        adapt_async_turn(_turn(state))


def test_rejects_duplicate_terrain_cells_across_batches() -> None:
    state = _state(
        objects=(
            TerrainView(kind="OBSTACLE", positions=((4, 4),)),
            TerrainView(kind="OBSTACLE", positions=((4, 4),)),
        ),
    )
    with pytest.raises(SdkContractViolationError, match="duplicate terrain"):
        adapt_async_turn(_turn(state))


def test_rejects_future_resolution_event_tick() -> None:
    state = _state(
        events=(ResolutionEvent(event_id=EVENT_ID, tick=2, event_type="SHOT_MISSED"),),
    )
    with pytest.raises(SdkContractViolationError, match="must not exceed turn tick"):
        adapt_async_turn(_turn(state, tick=1))


def test_accepts_historical_resolution_event_tick() -> None:
    # The live server bundles resolutions from earlier ticks into the current
    # turn (outcome confirmations for prior submissions). The adapter must
    # keep them with their own tick instead of rejecting the whole turn.
    state = _state(
        events=(
            ResolutionEvent(event_id=EVENT_ID, tick=1, event_type="HARVEST_SUCCEEDED"),
            ResolutionEvent(
                event_id="dddddddd-0000-0000-0000-000000000006",
                tick=2,
                event_type="DEPOSIT_SUCCEEDED",
            ),
        ),
    )
    observation = adapt_async_turn(_turn(state, tick=2))
    assert [event.tick for event in observation.events] == [1, 2]
    assert [event.kind for event in observation.events] == [
        "HARVEST_SUCCEEDED",
        "DEPOSIT_SUCCEEDED",
    ]


def test_rejects_unknown_player_status_member(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_arena_hero_module(override={"PlayerStatus": FuturePlayerStatus})
    monkeypatch.setattr(turns_module, "import_module", lambda _name: fake)
    state = PlayerState.model_construct(
        status=FuturePlayerStatus.FUTURE,
        resources=0,
        population=0,
        champion_beacon=ChampionBeacon(position=(0, 0)),
        objects=(),
        events=(),
    )
    with pytest.raises(SdkContractViolationError, match="unsupported SDK PlayerStatus member"):
        adapt_async_turn(_turn(state))


def test_rejects_unknown_unit_type_member(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_arena_hero_module(override={"UnitType": FutureUnitType})
    monkeypatch.setattr(turns_module, "import_module", lambda _name: fake)
    state = PlayerState.model_construct(
        status=PlayerStatus.ACTIVE,
        resources=0,
        population=0,
        champion_beacon=ChampionBeacon(position=(0, 0)),
        objects=(
            UnitView.model_construct(
                kind="UNIT",
                id=WORKER_ID,
                controlled=True,
                position=(0, 0),
                hp=4,
                unit_type=FutureUnitType.TITAN,
            ),
        ),
        events=(),
    )
    with pytest.raises(SdkContractViolationError, match="unsupported SDK UnitType member"):
        adapt_async_turn(_turn(state))
