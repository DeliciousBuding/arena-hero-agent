"""Immutable application turn/decision DTO validation tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from arena_hero_agent.application import (
    CoreAction,
    CoreIntent,
    Decision,
    PlayerLifecycle,
    TurnEvent,
    TurnObservation,
    UnitAction,
    UnitIntent,
)
from arena_hero_agent.domain import (
    BeaconObservation,
    BeaconStatus,
    Coordinate,
    CoreObservation,
    CoreState,
    Direction,
    EntityId,
    EntityKind,
    EntityObservation,
    ResourceObservation,
    RulesVersion,
    TerrainObservation,
    UnitObservation,
    UnitRole,
    WorldProjection,
)


def _core(identifier: str = "core-a") -> CoreObservation:
    return CoreObservation(
        id=EntityId(identifier),
        position=Coordinate(0, 0),
        health=5,
        shield=4,
        state=CoreState.NORMAL,
        owner="player",
    )


def _unit(identifier: str, x: int, role: UnitRole = UnitRole.WORKER) -> UnitObservation:
    return UnitObservation(
        id=EntityId(identifier),
        position=Coordinate(x, 0),
        role=role,
        health=2,
    )


def _entity(identifier: str, x: int) -> EntityObservation:
    return EntityObservation(
        id=EntityId(identifier),
        kind=EntityKind.UNIT,
        position=Coordinate(x, 1),
        health=3,
        owner="opponent",
        unit_role=UnitRole.VANGUARD,
    )


def _projection(
    *,
    tick: int = 1,
    core: CoreObservation | None = None,
    units: tuple[UnitObservation, ...] = (),
    entities: tuple[EntityObservation, ...] = (),
    resources: tuple[ResourceObservation, ...] = (),
    terrain: tuple[TerrainObservation, ...] = (),
) -> WorldProjection:
    return WorldProjection(
        tick=tick,
        rules_version=RulesVersion.V0_14,
        core=core,
        units=units,
        entities=entities,
        resources=resources,
        terrain=terrain,
        beacon=BeaconObservation(position=Coordinate(0, 1), status=BeaconStatus.UNKNOWN),
    )


def _observation(
    *,
    tick: int = 1,
    lifecycle: PlayerLifecycle = PlayerLifecycle.ACTIVE,
    events: tuple[TurnEvent, ...] = (),
    projection: WorldProjection | None = None,
) -> TurnObservation:
    return TurnObservation(
        tick=tick,
        lifecycle=lifecycle,
        resources=0,
        population=0,
        projection=projection or _projection(tick=tick),
        events=events,
    )


def _event(identifier: str = "event-a", tick: int = 1) -> TurnEvent:
    return TurnEvent(
        id=EntityId(identifier),
        tick=tick,
        kind="HARVEST_SUCCEEDED",
    )


class TestTurnEvent:
    def test_valid_event(self) -> None:
        event = TurnEvent(
            id=EntityId("event-a"),
            tick=3,
            kind="SHOT_MISSED",
            reason="TARGET_EVADED",
            actor_id=EntityId("unit-a"),
            target_id=EntityId("unit-b"),
            position=Coordinate(1, 2),
        )
        assert event.tick == 3
        assert event.kind == "SHOT_MISSED"

    @pytest.mark.parametrize("tick", [0, -1, True])
    def test_rejects_invalid_tick(self, tick: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            TurnEvent(id=EntityId("event-a"), tick=tick, kind="X")  # type: ignore

    @pytest.mark.parametrize("kind", ["", "  ", "  X"])
    def test_rejects_non_canonical_kind(self, kind: str) -> None:
        with pytest.raises(ValueError):
            TurnEvent(id=EntityId("event-a"), tick=1, kind=kind)

    def test_rejects_malformed_optional_fields(self) -> None:
        with pytest.raises(TypeError):
            TurnEvent(
                id=EntityId("event-a"),
                tick=1,
                kind="X",
                actor_id="unit-a",  # type: ignore
            )
        with pytest.raises(TypeError):
            TurnEvent(
                id=EntityId("event-a"),
                tick=1,
                kind="X",
                position=(1, 2),  # type: ignore
            )


class TestTurnObservation:
    def test_rejects_projection_tick_mismatch(self) -> None:
        with pytest.raises(ValueError, match="projection tick"):
            TurnObservation(
                tick=2,
                lifecycle=PlayerLifecycle.ACTIVE,
                resources=0,
                population=0,
                projection=_projection(tick=1),
            )

    def test_respawning_requires_respawn_at_tick(self) -> None:
        with pytest.raises(ValueError, match="respawn_at_tick"):
            TurnObservation(
                tick=1,
                lifecycle=PlayerLifecycle.RESPAWNING,
                resources=0,
                population=0,
                projection=_projection(tick=1),
            )

    def test_active_rejects_respawn_at_tick(self) -> None:
        with pytest.raises(ValueError, match="active turns"):
            TurnObservation(
                tick=1,
                lifecycle=PlayerLifecycle.ACTIVE,
                resources=0,
                population=0,
                projection=_projection(tick=1),
                respawn_at_tick=5,
            )

    def test_rejects_duplicate_event_identities(self) -> None:
        with pytest.raises(ValueError, match="duplicate event"):
            _observation(events=(_event("event-a"), _event("event-a")))

    def test_rejects_non_turn_event_members(self) -> None:
        with pytest.raises(TypeError, match="TurnEvent"):
            _observation(events=("event-a",))  # type: ignore

    def test_events_are_sorted_deterministically(self) -> None:
        observation = _observation(
            events=(_event("zz-event"), _event("aa-event"), _event("mm-event"))
        )
        assert [event.id.value for event in observation.events] == [
            "aa-event",
            "mm-event",
            "zz-event",
        ]

    def test_observation_is_immutable(self) -> None:
        observation = _observation()
        with pytest.raises(FrozenInstanceError):
            observation.tick = 2  # type: ignore


class TestUnitIntent:
    @pytest.mark.parametrize("action", [UnitAction.MOVE, UnitAction.SWEEP])
    def test_direction_actions_require_direction(self, action: UnitAction) -> None:
        with pytest.raises(ValueError, match="direction"):
            UnitIntent(unit_id=EntityId("unit-a"), action=action)

    def test_move_accepts_direction(self) -> None:
        intent = UnitIntent(
            unit_id=EntityId("unit-a"),
            action=UnitAction.MOVE,
            direction=Direction.EAST,
        )
        assert intent.direction is Direction.EAST

    def test_shoot_requires_expected_cell(self) -> None:
        with pytest.raises(ValueError, match="expected_cell"):
            UnitIntent(unit_id=EntityId("unit-a"), action=UnitAction.SHOOT)

    def test_only_shoot_may_declare_target_or_expected_cell(self) -> None:
        with pytest.raises(ValueError, match="only shoot intents"):
            UnitIntent(
                unit_id=EntityId("unit-a"),
                action=UnitAction.MOVE,
                direction=Direction.NORTH,
                target_id=EntityId("unit-b"),
            )
        with pytest.raises(ValueError, match="only shoot intents"):
            UnitIntent(
                unit_id=EntityId("unit-a"),
                action=UnitAction.WAIT,
                expected_cell=Coordinate(1, 1),
            )

    def test_rejects_non_unit_action(self) -> None:
        with pytest.raises(TypeError):
            UnitIntent(unit_id=EntityId("unit-a"), action="move")  # type: ignore


class TestCoreIntent:
    def test_start_move_requires_direction(self) -> None:
        with pytest.raises(ValueError, match="direction"):
            CoreIntent(action=CoreAction.START_MOVE)

    def test_spawn_requires_unit_role(self) -> None:
        with pytest.raises(ValueError, match="unit_role"):
            CoreIntent(action=CoreAction.SPAWN)

    def test_spawn_accepts_unit_role(self) -> None:
        intent = CoreIntent(action=CoreAction.SPAWN, unit_role=UnitRole.WORKER)
        assert intent.unit_role is UnitRole.WORKER

    def test_plain_actions_reject_unexpected_fields(self) -> None:
        with pytest.raises(ValueError, match="exactly one direction"):
            CoreIntent(action=CoreAction.WAIT, direction=Direction.NORTH)


class TestDecision:
    def test_rejects_duplicate_unit_identities(self) -> None:
        with pytest.raises(ValueError, match="duplicate unit intent"):
            Decision(
                tick=1,
                unit_intents=(
                    UnitIntent(unit_id=EntityId("unit-a"), action=UnitAction.WAIT),
                    UnitIntent(unit_id=EntityId("unit-a"), action=UnitAction.HEAL),
                ),
            )

    def test_rejects_non_unit_intent_members(self) -> None:
        with pytest.raises(TypeError, match="UnitIntent"):
            Decision(tick=1, unit_intents=("wait",))  # type: ignore

    def test_unit_intents_are_sorted_deterministically(self) -> None:
        decision = Decision(
            tick=1,
            unit_intents=(
                UnitIntent(unit_id=EntityId("zz-unit"), action=UnitAction.WAIT),
                UnitIntent(unit_id=EntityId("aa-unit"), action=UnitAction.HEAL),
            ),
        )
        assert [intent.unit_id.value for intent in decision.unit_intents] == [
            "aa-unit",
            "zz-unit",
        ]

    def test_rejects_invalid_tick(self) -> None:
        with pytest.raises((TypeError, ValueError)):
            Decision(tick=0)
        with pytest.raises((TypeError, ValueError)):
            Decision(tick=True)

    def test_rejects_unknown_core_intent(self) -> None:
        with pytest.raises(TypeError):
            Decision(tick=1, core_intent="spawn")  # type: ignore
