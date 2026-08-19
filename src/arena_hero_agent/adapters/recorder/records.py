"""Stable record schema shared by offline recorder backends.

Both backends persist the same logical records so storage can change without
changing the persisted contract:

- ``tick`` records hold one processed tick: the full ``TickResult`` fields,
  the owning tenant, and a wall-clock timestamp.
- ``loop`` records hold one completed tick-loop run summary; per-tick detail
  lives in ``tick`` records.

Field names are stable camelCase wire keys. ``RECORD_SCHEMA_VERSION`` is the
single version source: writers always stamp it and readers fail loudly on
unknown versions instead of guessing.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from arena_hero_agent.application import (
    DeadlineOutcome,
    StoppedReason,
    SubmitResult,
    TickLoopResult,
    TickResult,
)
from arena_hero_agent.application.turns import Decision, TurnObservation
from arena_hero_agent.domain import DecisionId, TenantId

from ._common import RecorderError

RECORD_SCHEMA_VERSION: Final = 1

RECORD_TYPE_TICK: Final = "tick"
RECORD_TYPE_LOOP: Final = "loop"
RECORD_TYPE_TICK_STATE: Final = "tick_state"

_MAX_STATE_EVENTS: Final = 20


def _required_int(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecorderError(f"{key} must be an integer; actual={value!r}")
    return value


def _require_enum(data: Mapping[str, object], key: str, enum_type: type[StrEnum]) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise RecorderError(f"{key} must be a string; actual={value!r}")
    allowed = {member.value for member in enum_type.__members__.values()}
    if value not in allowed:
        raise RecorderError(f"invalid {key} value {value!r}; expected one of {sorted(allowed)}")
    return value


def _check_common(data: Mapping[str, object], expected_tenant: TenantId, record_type: str) -> None:
    schema_version = data.get("schemaVersion")
    if schema_version != RECORD_SCHEMA_VERSION:
        raise RecorderError(
            "unsupported recorder schemaVersion "
            f"{schema_version!r}; expected {RECORD_SCHEMA_VERSION}"
        )
    actual_type = data.get("recordType")
    if actual_type != record_type:
        raise RecorderError(f"expected recordType {record_type!r}; actual={actual_type!r}")
    tenant = data.get("tenantId")
    if tenant != expected_tenant.value:
        raise RecorderError(
            f"record tenantId {tenant!r} does not match recorder tenant {expected_tenant.value!r}"
        )


def serialize_tick(
    result: TickResult, *, tenant_id: TenantId, recorded_at_ns: int
) -> dict[str, object]:
    """Serialize one tick outcome into a stable JSON object."""
    return {
        "schemaVersion": RECORD_SCHEMA_VERSION,
        "recordType": RECORD_TYPE_TICK,
        "tenantId": tenant_id.value,
        "recordedAtNs": recorded_at_ns,
        "tick": result.tick,
        "decisionId": result.decision_id.value,
        "deadlineOutcome": result.deadline_outcome.value,
        "submitResult": result.submit_result.value,
        "submitError": result.submit_error,
    }


def parse_tick(data: Mapping[str, object], *, expected_tenant: TenantId) -> TickResult:
    """Parse and validate one tick record, failing loudly on corruption."""
    _check_common(data, expected_tenant, RECORD_TYPE_TICK)
    tick = _required_int(data, "tick")
    decision_id = data.get("decisionId")
    if not isinstance(decision_id, str):
        raise RecorderError(f"decisionId must be a string; actual={decision_id!r}")
    deadline_outcome = DeadlineOutcome(_require_enum(data, "deadlineOutcome", DeadlineOutcome))
    submit_result = SubmitResult(_require_enum(data, "submitResult", SubmitResult))
    submit_error = data.get("submitError")
    if submit_error is not None and not isinstance(submit_error, str):
        raise RecorderError(f"submitError must be a string or null; actual={submit_error!r}")
    try:
        return TickResult(
            tick=tick,
            decision_id=DecisionId(decision_id),
            deadline_outcome=deadline_outcome,
            submit_result=submit_result,
            submit_error=submit_error,
        )
    except (TypeError, ValueError) as exc:
        raise RecorderError(f"invalid tick record: {exc}") from exc


def serialize_loop(result: TickLoopResult, *, recorded_at_ns: int) -> dict[str, object]:
    """Serialize one tick-loop run summary into a stable JSON object."""
    return {
        "schemaVersion": RECORD_SCHEMA_VERSION,
        "recordType": RECORD_TYPE_LOOP,
        "tenantId": result.tenant_id.value,
        "recordedAtNs": recorded_at_ns,
        "lastTick": result.last_tick,
        "ticksProcessed": result.ticks_processed,
        "duplicateTicks": result.duplicate_ticks,
        "outOfOrderTicks": result.out_of_order_ticks,
        "gapTicks": result.gap_ticks,
        "reconnectCount": result.reconnect_count,
        "stoppedReason": result.stopped_reason.value,
        "outcomeCount": len(result.outcomes),
    }


def parse_loop(data: Mapping[str, object], *, expected_tenant: TenantId) -> TickLoopResult:
    """Parse and validate one loop record, failing loudly on corruption."""
    _check_common(data, expected_tenant, RECORD_TYPE_LOOP)
    last_tick = _required_int(data, "lastTick")
    ticks_processed = _required_int(data, "ticksProcessed")
    duplicate_ticks = _required_int(data, "duplicateTicks")
    out_of_order_ticks = _required_int(data, "outOfOrderTicks")
    gap_ticks = _required_int(data, "gapTicks")
    reconnect_count = _required_int(data, "reconnectCount")
    stopped_reason = StoppedReason(_require_enum(data, "stoppedReason", StoppedReason))
    _required_int(data, "outcomeCount")  # informational; per-tick detail lives in tick records
    try:
        return TickLoopResult(
            tenant_id=expected_tenant,
            last_tick=last_tick,
            ticks_processed=ticks_processed,
            duplicate_ticks=duplicate_ticks,
            out_of_order_ticks=out_of_order_ticks,
            gap_ticks=gap_ticks,
            reconnect_count=reconnect_count,
            stopped_reason=stopped_reason,
            outcomes=(),
        )
    except (TypeError, ValueError) as exc:
        raise RecorderError(f"invalid loop record: {exc}") from exc


def _chebyshev(ax: int, ay: int, bx: int, by: int) -> int:
    """Grid distance (max of axis deltas) matching the engine's movement model."""
    return max(abs(ax - bx), abs(ay - by))


def _coordinate_pair(coordinate: object | None) -> list[int] | None:
    if coordinate is None:
        return None
    x = getattr(coordinate, "x", None)
    y = getattr(coordinate, "y", None)
    if (
        isinstance(x, bool)
        or not isinstance(x, int)
        or isinstance(y, bool)
        or not isinstance(y, int)
    ):
        return None
    return [x, y]


def _unit_counts_by_role(units: object) -> dict[str, int]:
    counts: dict[str, int] = {"worker": 0, "vanguard": 0, "ranger": 0}
    if isinstance(units, tuple | list):
        for unit in units:
            role = getattr(unit, "role", None)
            role_value = getattr(role, "value", role)
            if isinstance(role_value, str) and role_value in counts:
                counts[role_value] += 1
    return counts


def _unit_details(units: object) -> list[dict[str, object]]:
    """Per-unit id/role/position/hp/cargo so stalls are diagnosable from logs.

    Aggregated role counts hide where each worker actually stands; a terrain
    trap (a worker oscillating against an obstacle) is only visible when the
    individual positions are recorded tick by tick.
    """
    details: list[dict[str, object]] = []
    if isinstance(units, tuple | list):
        for unit in units:
            role = getattr(unit, "role", None)
            details.append(
                {
                    "id": getattr(getattr(unit, "id", None), "value", None),
                    "role": getattr(role, "value", role),
                    "pos": _coordinate_pair(getattr(unit, "position", None)),
                    "hp": getattr(unit, "health", None),
                    "cargo": getattr(unit, "cargo", None),
                }
            )
    return details


def _intent_counts_by_action(intents: object) -> dict[str, int]:
    counts: dict[str, int] = {}
    if isinstance(intents, tuple | list):
        for intent in intents:
            action = getattr(intent, "action", None)
            action_value = getattr(action, "value", action)
            if isinstance(action_value, str):
                counts[action_value] = counts.get(action_value, 0) + 1
    return counts


def _unit_intent_details(intents: object) -> list[dict[str, object]]:
    """Per-unit intent detail: unitId/action/direction/targetId/expectedCell.

    The aggregated action counts hide which unit wanted which move. The
    individual direction + expected cell make a failed move diagnosable (pair
    with the next tick's UNIT_MOVE_FAILED position) and expose per-worker
    stalls directly, e.g. a worker repeatedly MOVEing into the same obstacle.
    """
    details: list[dict[str, object]] = []
    if isinstance(intents, tuple | list):
        for intent in intents:
            unit_id = getattr(intent, "unit_id", None)
            direction = getattr(intent, "direction", None)
            target_id = getattr(intent, "target_id", None)
            expected_cell = getattr(intent, "expected_cell", None)
            details.append(
                {
                    "unitId": getattr(unit_id, "value", None),
                    "action": getattr(getattr(intent, "action", None), "value", None),
                    "direction": getattr(direction, "value", direction),
                    "targetId": (
                        getattr(target_id, "value", None) if target_id is not None else None
                    ),
                    "expectedCell": _coordinate_pair(expected_cell),
                }
            )
    return details


def _serialize_events(events: object) -> tuple[list[dict[str, object]], int]:
    """Return (capped event list, total event count) for a TurnEvent tuple."""
    if not isinstance(events, tuple | list):
        return [], 0
    total = len(events)
    serialized: list[dict[str, object]] = []
    for event in events[:_MAX_STATE_EVENTS]:
        actor_id = getattr(event, "actor_id", None)
        target_id = getattr(event, "target_id", None)
        serialized.append(
            {
                "id": getattr(getattr(event, "id", None), "value", None),
                "tick": getattr(event, "tick", None),
                "kind": getattr(event, "kind", None),
                "reason": getattr(event, "reason", None),
                "actorId": getattr(actor_id, "value", None) if actor_id is not None else None,
                "targetId": getattr(target_id, "value", None) if target_id is not None else None,
                "pos": _coordinate_pair(getattr(event, "position", None)),
            }
        )
    return serialized, total


def serialize_tick_state(
    observation: TurnObservation,
    decision: Decision | None,
    result: TickResult,
    *,
    tenant_id: TenantId,
    recorded_at_ns: int,
    decider_state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Serialize one rich tick snapshot: input state + plan + outcome.

    Pairs with the thin ``tick`` record on (tenantId, tick, recordedAtNs) so
    offline analysis can join decision metadata, full world projection
    aggregates, and the submitted plan without live probes. ``decider_state``
    (the ComposedDecider ``state_summary`` digest) records why hooks did or
    did not fire; it is read-only and never affects decisions.
    """
    projection = observation.projection
    core = projection.core
    core_pos = _coordinate_pair(getattr(core, "position", None)) if core is not None else None
    core_record: dict[str, object] | None = None
    if core is not None:
        core_record = {
            "pos": core_pos,
            "hp": getattr(core, "health", None),
            "shield": getattr(core, "shield", None),
            "state": getattr(getattr(core, "state", None), "value", getattr(core, "state", None)),
        }

    units_by_role = _unit_counts_by_role(projection.units)
    units_total = sum(units_by_role.values())

    resource_positions = list(projection.resources)
    resource_cells = len(resource_positions)
    nearest_resource_dist: int | None = None
    if core_pos is not None and resource_positions:
        nearest_resource_dist = min(
            _chebyshev(core_pos[0], core_pos[1], res.position.x, res.position.y)
            for res in resource_positions
        )

    entity_list = list(projection.entities)
    visible_enemies = len(entity_list)
    nearest_enemy_dist: int | None = None
    if core_pos is not None and entity_list:
        nearest_enemy_dist = min(
            _chebyshev(core_pos[0], core_pos[1], e.position.x, e.position.y) for e in entity_list
        )

    beacon_record: dict[str, object] | None = None
    if projection.beacon is not None:
        beacon = projection.beacon
        beacon_record = {
            "pos": _coordinate_pair(getattr(beacon, "position", None)),
            "status": getattr(
                getattr(beacon, "status", None), "value", getattr(beacon, "status", None)
            ),
        }

    events_serialized, event_count = _serialize_events(observation.events)

    plan_record: dict[str, object] | None = None
    if decision is not None:
        core_intent = decision.core_intent
        core_intent_record: dict[str, object] | None = None
        if core_intent is not None:
            direction = getattr(core_intent, "direction", None)
            core_intent_record = {
                "action": getattr(getattr(core_intent, "action", None), "value", None),
                "unitRole": getattr(getattr(core_intent, "unit_role", None), "value", None),
                "direction": getattr(direction, "value", direction),
            }
        plan_record = {
            "coreIntent": core_intent_record,
            "unitIntentsByAction": _intent_counts_by_action(decision.unit_intents),
            "unitIntents": _unit_intent_details(decision.unit_intents),
            "unitIntentsTotal": len(decision.unit_intents),
        }

    return {
        "schemaVersion": RECORD_SCHEMA_VERSION,
        "recordType": RECORD_TYPE_TICK_STATE,
        "tenantId": tenant_id.value,
        "recordedAtNs": recorded_at_ns,
        "tick": result.tick,
        "decisionId": result.decision_id.value,
        "deadlineOutcome": result.deadline_outcome.value,
        "submitResult": result.submit_result.value,
        "submitError": result.submit_error,
        "agentLatencyMs": result.agent_latency_ms,
        "selectionLatencyMs": result.selection_latency_ms,
        "lifecycle": observation.lifecycle.value,
        "resources": observation.resources,
        "population": observation.population,
        "respawnAtTick": observation.respawn_at_tick,
        "core": core_record,
        "unitsByRole": units_by_role,
        "units": _unit_details(projection.units),
        "unitsTotal": units_total,
        "visibleEnemies": visible_enemies,
        "nearestEnemyDist": nearest_enemy_dist,
        "resourceCells": resource_cells,
        "nearestResourceDist": nearest_resource_dist,
        "terrainObstacles": len(projection.terrain),
        "beacon": beacon_record,
        "events": events_serialized,
        "eventCount": event_count,
        "plan": plan_record,
        "deciderState": None if decider_state is None else dict(decider_state),
    }


def parse_tick_state(data: Mapping[str, object], *, expected_tenant: TenantId) -> dict[str, object]:
    """Validate one tick_state record's common envelope; returns the raw dict.

    The full state payload is large and application-owned; this check enforces
    the stable envelope (schemaVersion, recordType, tenantId, tick, decisionId,
    deadline/submit vocabulary) so corrupt records fail loudly without
    re-instantiating every domain value.
    """
    _check_common(data, expected_tenant, RECORD_TYPE_TICK_STATE)
    _required_int(data, "tick")
    decision_id = data.get("decisionId")
    if not isinstance(decision_id, str):
        raise RecorderError(f"decisionId must be a string; actual={decision_id!r}")
    DeadlineOutcome(_require_enum(data, "deadlineOutcome", DeadlineOutcome))
    SubmitResult(_require_enum(data, "submitResult", SubmitResult))
    submit_error = data.get("submitError")
    if submit_error is not None and not isinstance(submit_error, str):
        raise RecorderError(f"submitError must be a string or null; actual={submit_error!r}")
    return dict(data)
