"""Regression tests for the diagnostic telemetry fields of tick_state records.

These fields exist purely so stalls are diagnosable from logs: per-unit plan
intents (who wanted to move where), the Core intent direction, and the
read-only decider-state digest (why hooks fired or stayed silent). Persisting
them must never change decisions — they are serialization-only.
"""

from __future__ import annotations

from arena_hero_agent.adapters.recorder.records import serialize_tick_state
from arena_hero_agent.application import (
    DeadlineOutcome,
    SubmitResult,
    TickResult,
    TurnObservation,
)
from arena_hero_agent.application.turns import CoreAction as ApplicationCoreAction
from arena_hero_agent.application.turns import (
    CoreIntent,
    Decision,
    PlayerLifecycle,
    UnitIntent,
)
from arena_hero_agent.application.turns import UnitAction as ApplicationUnitAction
from arena_hero_agent.domain import (
    Coordinate,
    DecisionId,
    Direction,
    EntityId,
    RulesVersion,
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


def _result(tick: int) -> TickResult:
    return TickResult(
        tick=tick,
        decision_id=DecisionId.from_deterministic_input((TENANT, tick, "state")),
        deadline_outcome=DeadlineOutcome.CANDIDATE,
        submit_result=SubmitResult.ACCEPTED,
    )


def _decision(tick: int) -> Decision:
    return Decision(
        tick=tick,
        unit_intents=(
            UnitIntent(
                unit_id=EntityId("w1"),
                action=ApplicationUnitAction.MOVE,
                direction=Direction.WEST,
            ),
            UnitIntent(
                unit_id=EntityId("r1"),
                action=ApplicationUnitAction.SHOOT,
                expected_cell=Coordinate(3, 3),
            ),
        ),
        core_intent=CoreIntent(
            action=ApplicationCoreAction.START_MOVE,
            direction=Direction.WEST,
        ),
    )


def test_plan_records_per_unit_intent_details() -> None:
    record = serialize_tick_state(
        _observation(1),
        _decision(1),
        _result(1),
        tenant_id=TENANT,
        recorded_at_ns=1,
    )

    plan = record["plan"]
    assert isinstance(plan, dict)
    intents = plan["unitIntents"]
    # Decision normalizes intents sorted by unit id, so r1 comes before w1.
    assert intents == [
        {
            "unitId": "r1",
            "action": "shoot",
            "direction": None,
            "targetId": None,
            "expectedCell": [3, 3],
        },
        {
            "unitId": "w1",
            "action": "move",
            "direction": "west",
            "targetId": None,
            "expectedCell": None,
        },
    ]
    # Aggregated counts stay for backward compatibility.
    assert plan["unitIntentsByAction"] == {"move": 1, "shoot": 1}


def test_core_intent_records_direction() -> None:
    record = serialize_tick_state(
        _observation(1),
        _decision(1),
        _result(1),
        tenant_id=TENANT,
        recorded_at_ns=1,
    )

    core_intent = record["plan"]["coreIntent"]
    assert core_intent == {"action": "start_move", "unitRole": None, "direction": "west"}


def test_decider_state_is_persisted_when_provided() -> None:
    digest = {
        "barrenMigration": {"active": True, "barrenSinceTick": 17},
        "noWorkerDeadlockTicks": 3,
    }
    record = serialize_tick_state(
        _observation(1),
        _decision(1),
        _result(1),
        tenant_id=TENANT,
        recorded_at_ns=1,
        decider_state=digest,
    )
    assert record["deciderState"] == digest


def test_decider_state_is_none_when_omitted() -> None:
    record = serialize_tick_state(
        _observation(1),
        _decision(1),
        _result(1),
        tenant_id=TENANT,
        recorded_at_ns=1,
    )
    assert record["deciderState"] is None
