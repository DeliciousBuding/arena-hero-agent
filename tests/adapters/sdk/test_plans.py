"""Decision to SDK CommandPlan construction and payload digest tests."""

from __future__ import annotations

from uuid import UUID

import pytest
from arena_hero import CommandPlan, MoveAction, ShootAction, SpawnAction

import arena_hero_agent.adapters.sdk.plans as plans_module
from arena_hero_agent.adapters.sdk import (
    SdkContractViolationError,
    build_command_plan,
    command_plan_payload,
)
from arena_hero_agent.application import (
    CoreAction,
    CoreIntent,
    Decision,
    PlayerLifecycle,
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
    RulesVersion,
    UnitObservation,
    UnitRole,
    WorldProjection,
    canonical_sha256,
)

from ._sdk_fakes import fake_arena_hero_module

WORKER_ID = "aaaaaaaa-0000-0000-0000-000000000001"
RANGER_ID = "aaaaaaaa-0000-0000-0000-000000000002"
VANGUARD_ID = "aaaaaaaa-0000-0000-0000-000000000003"
ENEMY_ID = "bbbbbbbb-0000-0000-0000-000000000004"
CORE_ID = "cccccccc-0000-0000-0000-000000000005"


def _core() -> CoreObservation:
    return CoreObservation(
        id=EntityId(CORE_ID),
        position=Coordinate(0, 0),
        health=5,
        shield=4,
        state=CoreState.NORMAL,
        owner="player",
    )


def _unit(identifier: str, x: int, role: UnitRole) -> UnitObservation:
    return UnitObservation(
        id=EntityId(identifier),
        position=Coordinate(x, 0),
        role=role,
        health=2,
    )


def _observation(*, tick: int = 1) -> TurnObservation:
    projection = WorldProjection(
        tick=tick,
        rules_version=RulesVersion.V0_14,
        core=_core(),
        units=(
            _unit(WORKER_ID, 0, UnitRole.WORKER),
            _unit(RANGER_ID, 2, UnitRole.RANGER),
            _unit(VANGUARD_ID, 3, UnitRole.VANGUARD),
        ),
        entities=(
            EntityObservation(
                id=EntityId(ENEMY_ID),
                kind=EntityKind.UNIT,
                position=Coordinate(5, 1),
                health=2,
                owner="opponent",
                unit_role=UnitRole.VANGUARD,
            ),
        ),
        beacon=BeaconObservation(position=Coordinate(0, 1), status=BeaconStatus.UNKNOWN),
    )
    return TurnObservation(
        tick=tick,
        lifecycle=PlayerLifecycle.ACTIVE,
        resources=0,
        population=0,
        projection=projection,
    )


def _decision(
    *,
    tick: int = 1,
    unit_intents: tuple[UnitIntent, ...] = (),
    core_intent: CoreIntent | None = None,
) -> Decision:
    return Decision(tick=tick, unit_intents=unit_intents, core_intent=core_intent)


def test_builds_plan_with_unit_and_core_actions() -> None:
    observation = _observation()
    decision = _decision(
        unit_intents=(
            UnitIntent(
                unit_id=EntityId(WORKER_ID),
                action=UnitAction.MOVE,
                direction=Direction.EAST,
            ),
            UnitIntent(
                unit_id=EntityId(RANGER_ID),
                action=UnitAction.SHOOT,
                target_id=EntityId(ENEMY_ID),
                expected_cell=Coordinate(5, 1),
            ),
        ),
        core_intent=CoreIntent(action=CoreAction.SPAWN, unit_role=UnitRole.WORKER),
    )
    plan = build_command_plan(decision, observation)

    assert plan.tick == 1
    assert type(plan.unit_actions[UUID(WORKER_ID)]) is MoveAction
    assert type(plan.unit_actions[UUID(RANGER_ID)]) is ShootAction
    assert plan.core_action is not None
    assert type(plan.core_action) is SpawnAction


def test_rejects_tick_mismatch() -> None:
    with pytest.raises(SdkContractViolationError, match="tick does not match"):
        build_command_plan(_decision(tick=2), _observation(tick=1))


def test_rejects_unknown_controlled_unit() -> None:
    decision = _decision(
        unit_intents=(
            UnitIntent(
                unit_id=EntityId("dddddddd-0000-0000-0000-000000000099"),
                action=UnitAction.WAIT,
            ),
        ),
    )
    with pytest.raises(SdkContractViolationError, match="unknown controlled unit"):
        build_command_plan(decision, _observation())


def test_rejects_action_invalid_for_role() -> None:
    decision = _decision(
        unit_intents=(
            UnitIntent(
                unit_id=EntityId(WORKER_ID),
                action=UnitAction.SHOOT,
                target_id=EntityId(ENEMY_ID),
                expected_cell=Coordinate(5, 1),
            ),
        ),
    )
    with pytest.raises(SdkContractViolationError, match="invalid for role"):
        build_command_plan(decision, _observation())


def test_rejects_shoot_unknown_target() -> None:
    decision = _decision(
        unit_intents=(
            UnitIntent(
                unit_id=EntityId(RANGER_ID),
                action=UnitAction.SHOOT,
                target_id=EntityId("eeeeeeee-0000-0000-0000-000000000099"),
                expected_cell=Coordinate(9, 9),
            ),
        ),
    )
    with pytest.raises(SdkContractViolationError, match="unknown target"):
        build_command_plan(decision, _observation())


def test_rejects_core_intent_without_observed_core() -> None:
    projection = WorldProjection(
        tick=1,
        rules_version=RulesVersion.V0_14,
        core=None,
        units=(_unit(WORKER_ID, 0, UnitRole.WORKER),),
        beacon=BeaconObservation(position=Coordinate(0, 1), status=BeaconStatus.UNKNOWN),
    )
    observation = TurnObservation(
        tick=1,
        lifecycle=PlayerLifecycle.ACTIVE,
        resources=0,
        population=0,
        projection=projection,
    )
    decision = _decision(
        core_intent=CoreIntent(action=CoreAction.SPAWN, unit_role=UnitRole.WORKER),
    )
    with pytest.raises(SdkContractViolationError, match="observed controlled core"):
        build_command_plan(decision, observation)


def test_rejects_non_exact_decision_or_observation() -> None:
    with pytest.raises(SdkContractViolationError, match="exact application Decision"):
        build_command_plan(object(), _observation())  # type: ignore
    with pytest.raises(SdkContractViolationError, match="exact application TurnObservation"):
        build_command_plan(_decision(), object())  # type: ignore


def test_rejects_missing_sdk_action_member(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_arena_hero_module(remove={"SweepAction"})
    monkeypatch.setattr(plans_module, "import_module", lambda _name: fake)
    decision = _decision(
        unit_intents=(
            UnitIntent(
                unit_id=EntityId(VANGUARD_ID),
                action=UnitAction.SWEEP,
                direction=Direction.NORTH,
            ),
        ),
    )
    with pytest.raises(SdkContractViolationError, match="SDK rejected unit action"):
        build_command_plan(decision, _observation())


def test_payload_is_deterministic_across_intent_order() -> None:
    observation = _observation()
    first = _decision(
        unit_intents=(
            UnitIntent(
                unit_id=EntityId(WORKER_ID),
                action=UnitAction.MOVE,
                direction=Direction.EAST,
            ),
            UnitIntent(
                unit_id=EntityId(RANGER_ID),
                action=UnitAction.SHOOT,
                target_id=EntityId(ENEMY_ID),
                expected_cell=Coordinate(5, 1),
            ),
        ),
    )
    second = _decision(
        unit_intents=(
            UnitIntent(
                unit_id=EntityId(RANGER_ID),
                action=UnitAction.SHOOT,
                target_id=EntityId(ENEMY_ID),
                expected_cell=Coordinate(5, 1),
            ),
            UnitIntent(
                unit_id=EntityId(WORKER_ID),
                action=UnitAction.MOVE,
                direction=Direction.EAST,
            ),
        ),
    )
    payload_first = command_plan_payload(build_command_plan(first, observation))
    payload_second = command_plan_payload(build_command_plan(second, observation))
    assert payload_first == payload_second
    assert canonical_sha256(payload_first) == canonical_sha256(payload_second)


def test_command_plan_payload_rejects_non_plan() -> None:
    with pytest.raises(SdkContractViolationError, match="CommandPlan"):
        command_plan_payload(object())


class _NoModelDumpAction:
    """An SDK-shaped action lacking Pydantic v2's ``model_dump`` (future drift)."""

    type = "WAIT"

    def model_dump(self, *args: object, **kwargs: object) -> object:
        raise AttributeError("model_dump is only available on Pydantic v2 models")


def test_command_plan_payload_rejects_core_action_without_model_dump() -> None:
    plan = CommandPlan.model_construct(
        tick=1,
        unit_actions={},
        core_action=_NoModelDumpAction(),
    )
    with pytest.raises(SdkContractViolationError, match="unknown core action object"):
        command_plan_payload(plan)


def test_rejects_non_canonical_unit_uuid_before_lookup() -> None:
    decision = _decision(
        unit_intents=(
            UnitIntent(
                unit_id=EntityId("AAAAAAAA-0000-0000-0000-000000000001"),
                action=UnitAction.WAIT,
            ),
        ),
    )
    with pytest.raises(SdkContractViolationError, match="canonical SDK UUID"):
        build_command_plan(decision, _observation())
