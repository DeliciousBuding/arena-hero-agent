"""Decision trace factories, outcome counters, and deterministic hashing tests."""

from __future__ import annotations

from arena_hero_agent.telemetry import (
    OUTCOME_COUNT_EVENT_TYPES,
    OutcomeCountEvent,
    OutcomeCountOwnershipContext,
    count_outcome_events,
    plan_hash_of,
)

# ---------------------------------------------------------------------------
# count_outcome_events
# ---------------------------------------------------------------------------


def deposit(actor: str | None, amount: object) -> dict[str, object]:
    return {
        "eventType": OUTCOME_COUNT_EVENT_TYPES.DEPOSIT_SUCCEEDED,
        "actorId": actor,
        "values": {"amount": amount},
    }


def test_deposit_sum_without_ownership() -> None:
    counts = count_outcome_events([deposit("w1", 5), deposit("w2", 7)])
    assert counts.grossDeposit == 12
    assert counts.spawnCount == 0
    assert counts.healCount == 0
    assert counts.unitLossCount == 0


def test_deposit_ownership_filters_actors() -> None:
    ownership = OutcomeCountOwnershipContext(priorUnitIds=frozenset({"w1", "w2"}), priorCoreId="c1")
    counts = count_outcome_events(
        [
            deposit("w1", 5),
            deposit("w3", 100),  # not owned
            deposit(None, 9),  # no actor
            {
                "eventType": OUTCOME_COUNT_EVENT_TYPES.DEPOSIT_SUCCEEDED,
                "actorId": "c1",
                "values": {"amount": 3},
            },
        ],
        ownership,
    )
    assert counts.grossDeposit == 8


def test_negative_amount_without_ownership_counts() -> None:
    counts = count_outcome_events([deposit("w1", -3)])
    assert counts.grossDeposit == -3


def test_negative_amount_with_ownership_is_excluded() -> None:
    ownership = OutcomeCountOwnershipContext(priorUnitIds=frozenset({"w1"}))
    counts = count_outcome_events([deposit("w1", -3)], ownership)
    assert counts.grossDeposit == 0


def test_non_numeric_or_missing_amount_contributes_zero() -> None:
    counts = count_outcome_events(
        [
            {"eventType": OUTCOME_COUNT_EVENT_TYPES.DEPOSIT_SUCCEEDED, "actorId": "w1"},
            {
                "eventType": OUTCOME_COUNT_EVENT_TYPES.DEPOSIT_SUCCEEDED,
                "actorId": "w1",
                "values": {"amount": "lots"},
            },
            {
                "eventType": OUTCOME_COUNT_EVENT_TYPES.DEPOSIT_SUCCEEDED,
                "actorId": "w1",
                "values": None,
            },
        ]
    )
    assert counts.grossDeposit == 0


def test_spawn_and_heal_counts() -> None:
    counts = count_outcome_events(
        [
            {"eventType": OUTCOME_COUNT_EVENT_TYPES.CORE_SPAWN_SUCCEEDED, "actorId": "c1"},
            {"eventType": OUTCOME_COUNT_EVENT_TYPES.UNIT_HEAL_SUCCEEDED, "actorId": "w1"},
            {"eventType": OUTCOME_COUNT_EVENT_TYPES.CORE_HEAL_SUCCEEDED, "actorId": "c1"},
            {"eventType": "REPAIR", "actorId": "w2"},
        ]
    )
    assert counts.spawnCount == 1
    assert counts.healCount == 2  # REPAIR is not a heal


def test_unit_loss_explicit_list() -> None:
    counts = count_outcome_events(
        [
            {"eventType": OUTCOME_COUNT_EVENT_TYPES.UNIT_DESTROYED, "actorId": "w1"},
            {"eventType": OUTCOME_COUNT_EVENT_TYPES.UNIT_SELF_DESTRUCTED, "actorId": "w2"},
            {"eventType": "CORE_RESOURCE_OVERFLOW_DESTROYED", "actorId": "w3"},
            {"eventType": "CORE_DESTROYED", "actorId": "c1"},
        ]
    )
    assert counts.unitLossCount == 2


def test_unit_loss_requires_prior_ownership() -> None:
    ownership = OutcomeCountOwnershipContext(priorUnitIds=frozenset({"w1"}))
    counts = count_outcome_events(
        [
            {"eventType": OUTCOME_COUNT_EVENT_TYPES.UNIT_DESTROYED, "actorId": "w1"},
            {"eventType": OUTCOME_COUNT_EVENT_TYPES.UNIT_DESTROYED, "actorId": "w9"},
            {"eventType": OUTCOME_COUNT_EVENT_TYPES.UNIT_DESTROYED, "actorId": None},
        ],
        ownership,
    )
    assert counts.unitLossCount == 1


def test_outcome_count_event_dataclass_forms() -> None:
    events = [
        OutcomeCountEvent(
            eventType=OUTCOME_COUNT_EVENT_TYPES.DEPOSIT_SUCCEEDED,
            actorId="w1",
            values={"amount": 4},
        ),
        OutcomeCountEvent(eventType=OUTCOME_COUNT_EVENT_TYPES.CORE_SPAWN_SUCCEEDED, actorId="c1"),
    ]
    counts = count_outcome_events(events)
    assert counts.grossDeposit == 4
    assert counts.spawnCount == 1


# ---------------------------------------------------------------------------
# plan_hash_of (known answers computed with Node.js against the TS oracle)
# ---------------------------------------------------------------------------


def test_plan_hash_known_answers() -> None:
    assert (
        plan_hash_of({"type": "MOVE", "direction": "RIGHT", "tick": 1000, "units": ["w1", "w2"]})
        == "f87f5bd1"
    )
    assert (
        plan_hash_of(
            {"plan": {"type": "MOVE", "direction": "RIGHT", "tick": 1000, "units": ["w1", "w2"]}}
        )
        == "63dec4d6"
    )
    assert plan_hash_of({"a": {"b": [1, {"c": None}]}, "d": "x"}) == "bb8bfce1"
    assert (
        plan_hash_of({"beacon": {"position": [2, 3], "status": "GROUND"}, "count": 0}) == "8f36dfa7"
    )
    assert plan_hash_of({"events": ["DEPOSIT 2"], "delta": 2.5}) == "4d759dff"
    assert plan_hash_of({"x": -5, "y": 0}) == "a000186c"
    assert plan_hash_of({"a": "1", "b": 1}) == "cd2f26aa"
    assert plan_hash_of([1, 2, 3]) == "e0f965d9"
    assert plan_hash_of(True) == "4db211e5"
    assert plan_hash_of("") == "ffcaaa85"
    assert plan_hash_of(None) == "77074ba4"
    assert plan_hash_of({}) == "5465b825"


def test_plan_hash_key_order_independent() -> None:
    assert (
        plan_hash_of({"a": 1, "b": "x", "tick": 1000})
        == plan_hash_of({"tick": 1000, "b": "x", "a": 1})
        == "3ce369a3"
    )


def test_plan_hash_non_ascii_and_surrogate_pairs() -> None:
    assert plan_hash_of("中文") == "0a27a3bd"
    assert plan_hash_of("😀") == "accb0e0e"


def test_plan_hash_js_number_formatting() -> None:
    # JS JSON.stringify serializes 1.0 as "1", -0.0 as "0", 1e21 as "1e+21",
    # and 1e-7 as "1e-7"; the stable string must match for hash parity.
    assert (
        plan_hash_of({"n": -0.0, "z": 0, "f": 1.0, "half": 4.5, "big": 1e21, "tiny": 1e-7})
        == "905289e2"
    )


def test_plan_hash_is_deterministic_and_time_free() -> None:
    first = plan_hash_of({"type": "DEPOSIT", "tick": 42})
    second = plan_hash_of({"type": "DEPOSIT", "tick": 42})
    assert first == second
    assert len(first) == 8
