from __future__ import annotations

from importlib import import_module
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel

from arena_hero_agent.application import (
    CoreAction,
    CoreIntent,
    Decision,
    TurnObservation,
    UnitAction,
    UnitIntent,
)
from arena_hero_agent.domain import UnitRole, codepoint_order_key

from .bindings import load_sdk_bindings
from .errors import SdkContractViolationError
from .mapping import to_sdk_direction

_UNIT_ACTIONS_BY_ROLE = {
    UnitRole.WORKER: frozenset(
        {
            UnitAction.WAIT,
            UnitAction.MOVE,
            UnitAction.HARVEST,
            UnitAction.DEPOSIT,
            UnitAction.PICKUP_BEACON,
            UnitAction.DROP_BEACON,
            UnitAction.SELF_DESTRUCT,
            UnitAction.HEAL,
        }
    ),
    UnitRole.VANGUARD: frozenset(
        {
            UnitAction.WAIT,
            UnitAction.MOVE,
            UnitAction.SWEEP,
            UnitAction.PICKUP_BEACON,
            UnitAction.DROP_BEACON,
            UnitAction.SELF_DESTRUCT,
            UnitAction.HEAL,
        }
    ),
    UnitRole.RANGER: frozenset(
        {
            UnitAction.WAIT,
            UnitAction.MOVE,
            UnitAction.SHOOT,
            UnitAction.PICKUP_BEACON,
            UnitAction.DROP_BEACON,
            UnitAction.SELF_DESTRUCT,
            UnitAction.HEAL,
        }
    ),
}


def _violation(message: str) -> SdkContractViolationError:
    return SdkContractViolationError("build-plan", message)


def _uuid(value: str, label: str) -> UUID:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise _violation(f"{label} is not an SDK UUID: {value!r}") from exc
    if str(parsed) != value:
        raise _violation(f"{label} is not a canonical SDK UUID: {value!r}")
    return parsed


def _unit_action(intent: UnitIntent, *, sdk: Any) -> Any:
    action = intent.action
    try:
        if action is UnitAction.WAIT:
            return sdk.WaitAction()
        if action is UnitAction.MOVE:
            if intent.direction is None:
                raise _violation("move intent requires direction")
            return sdk.MoveAction(direction=to_sdk_direction(intent.direction))
        if action is UnitAction.HARVEST:
            return sdk.HarvestAction()
        if action is UnitAction.DEPOSIT:
            return sdk.DepositAction()
        if action is UnitAction.SWEEP:
            if intent.direction is None:
                raise _violation("sweep intent requires direction")
            return sdk.SweepAction(direction=to_sdk_direction(intent.direction))
        if action is UnitAction.SHOOT:
            if intent.expected_cell is None:
                raise _violation("shoot intent requires expected_cell")
            return sdk.ShootAction(
                target_id=None
                if intent.target_id is None
                else _uuid(intent.target_id.value, "target id"),
                expected_cell=(intent.expected_cell.x, intent.expected_cell.y),
            )
        if action is UnitAction.PICKUP_BEACON:
            return sdk.PickupBeaconAction()
        if action is UnitAction.DROP_BEACON:
            return sdk.DropBeaconAction()
        if action is UnitAction.SELF_DESTRUCT:
            return sdk.SelfDestructAction()
        if action is UnitAction.HEAL:
            return sdk.HealAction()
        raise _violation(f"unsupported unit intent {action!r}")
    except SdkContractViolationError:
        raise
    except Exception as exc:
        raise _violation(f"SDK rejected unit action {action.value!r}: {exc}") from exc


def _core_action(intent: CoreIntent, *, sdk: Any) -> Any:
    action = intent.action
    try:
        if action is CoreAction.WAIT:
            return sdk.WaitAction()
        if action is CoreAction.SPAWN:
            role_names = {
                UnitRole.WORKER: "WORKER",
                UnitRole.VANGUARD: "VANGUARD",
                UnitRole.RANGER: "RANGER",
            }
            if intent.unit_role is None:
                raise _violation("spawn intent requires unit_role")
            try:
                unit_type = sdk.UnitType[role_names[intent.unit_role]]
            except (KeyError, TypeError) as exc:
                raise _violation("SDK UnitType does not support the requested spawn role") from exc
            return sdk.SpawnAction(unit_type=unit_type)
        if action is CoreAction.REPAIR_SHIELD:
            return sdk.RepairShieldAction()
        if action is CoreAction.START_MOVE:
            if intent.direction is None:
                raise _violation("start-move intent requires direction")
            return sdk.StartMoveAction(direction=to_sdk_direction(intent.direction))
        if action is CoreAction.CANCEL_MOVE:
            return sdk.CancelMoveAction()
        if action is CoreAction.PICKUP_BEACON:
            return sdk.PickupBeaconAction()
        if action is CoreAction.DROP_BEACON:
            return sdk.DropBeaconAction()
        if action is CoreAction.SELF_DESTRUCT:
            return sdk.SelfDestructAction()
        if action is CoreAction.HEAL:
            return sdk.HealAction()
        raise _violation(f"unsupported core intent {action!r}")
    except SdkContractViolationError:
        raise
    except Exception as exc:
        raise _violation(f"SDK rejected core action {action.value!r}: {exc}") from exc


def build_command_plan(decision: Decision, observation: TurnObservation) -> Any:
    """Build a deterministic SDK plan after validating it against the observed tick."""

    if type(decision) is not Decision:
        raise _violation("expected an exact application Decision")
    if type(observation) is not TurnObservation:
        raise _violation("expected an exact application TurnObservation")
    if decision.tick != observation.tick:
        raise _violation("decision tick does not match observation tick")

    sdk = import_module("arena_hero")
    bindings = load_sdk_bindings()
    known_units = {unit.id.value: unit for unit in observation.projection.units}
    known_entities = {entity.id.value for entity in observation.projection.entities} | {
        unit.id.value for unit in observation.projection.units
    }
    if observation.projection.core is not None:
        known_entities.add(observation.projection.core.id.value)

    unit_actions: dict[UUID, Any] = {}
    for intent in sorted(
        decision.unit_intents, key=lambda item: codepoint_order_key(item.unit_id.value)
    ):
        if type(intent) is not UnitIntent:
            raise _violation("decision contains an unknown unit intent object")
        unit_id = _uuid(intent.unit_id.value, "unit id")
        unit = known_units.get(intent.unit_id.value)
        if unit is None:
            raise _violation(
                f"unit intent references unknown controlled unit {intent.unit_id.value!r}"
            )
        if intent.action not in _UNIT_ACTIONS_BY_ROLE[unit.role]:
            raise _violation(
                f"action {intent.action.value!r} is invalid for role {unit.role.value!r}"
            )
        if intent.target_id is not None and intent.target_id.value not in known_entities:
            raise _violation(f"shoot intent references unknown target {intent.target_id.value!r}")
        if unit_id in unit_actions:
            raise _violation(f"duplicate unit intent {intent.unit_id.value!r}")
        unit_actions[unit_id] = _unit_action(intent, sdk=sdk)

    core_action = None
    if decision.core_intent is not None:
        if type(decision.core_intent) is not CoreIntent:
            raise _violation("decision contains an unknown core intent object")
        if observation.projection.core is None:
            raise _violation("core intent requires an observed controlled core")
        core_action = _core_action(decision.core_intent, sdk=sdk)

    try:
        plan = sdk.CommandPlan(
            tick=decision.tick,
            unit_actions=unit_actions,
            core_action=core_action,
        )
    except Exception as exc:
        raise _violation(f"SDK rejected constructed CommandPlan: {exc}") from exc
    if type(plan) is not bindings.command_plan_type:
        raise _violation(f"unexpected SDK CommandPlan shape {type(plan).__name__}")
    return plan


def command_plan_payload(plan: object) -> dict[str, object]:
    """Return the environment-neutral canonical public payload for fixtures/digests."""

    bindings = load_sdk_bindings()
    if type(plan) is not bindings.command_plan_type:
        raise _violation("expected an exact SDK CommandPlan")
    plan_value = cast(Any, plan)
    unit_actions = []
    for unit_id, action in sorted(plan_value.unit_actions.items(), key=lambda item: str(item[0])):
        if not isinstance(unit_id, UUID):
            raise _violation("CommandPlan contains a non-UUID unit identity")
        if not isinstance(action, BaseModel):
            raise _violation("CommandPlan contains an unknown action object")
        unit_actions.append(
            {"unit_id": str(unit_id), "action": action.model_dump(mode="json", exclude_none=True)}
        )
    core_action_value = plan_value.core_action
    if core_action_value is not None and not isinstance(core_action_value, BaseModel):
        raise _violation("CommandPlan contains an unknown core action object")
    core = (
        None
        if core_action_value is None
        else core_action_value.model_dump(mode="json", exclude_none=True)
    )
    return {"tick": plan_value.tick, "unit_actions": unit_actions, "core_action": core}
