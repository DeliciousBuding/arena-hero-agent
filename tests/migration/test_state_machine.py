"""Full migration state machine transition table (migration-system-v1 §2).

Data-driven: every legal transition is a row in LEGAL_TRANSITIONS; every other
(state, event) combination is asserted to be a no-op (fail-closed), and unknown
state/event values are asserted to be rejected with ValueError.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from arena_hero_agent.migration.state_machine import (
    MigrationEvent,
    MigrationEventType,
    MigrationState,
    transition,
)

ALL_STATES = list(MigrationState)
ALL_EVENTS = list(MigrationEventType)

_ACTIVE = frozenset(
    {
        MigrationState.PLAN,
        MigrationState.LEG_MOVE,
        MigrationState.LEG_SETTLE,
        MigrationState.DEFENSIVE_HOLD,
    }
)
_CANCEL_NOOP = frozenset(
    {
        MigrationState.IDLE,
        MigrationState.ABORT,
        MigrationState.RECOVERY_ABORT,
        MigrationState.ARRIVED,
    }
)

# (state, event_type, last_leg, expected) — the authoritative transition table.
LEGAL_TRANSITIONS: list[tuple[MigrationState, MigrationEventType, bool, MigrationState]] = [
    (MigrationState.IDLE, MigrationEventType.INTENT_ACCEPTED, False, MigrationState.PLAN),
    (MigrationState.PLAN, MigrationEventType.PLAN_AUDITED, False, MigrationState.LEG_MOVE),
    (MigrationState.PLAN, MigrationEventType.PLAN_REJECTED, False, MigrationState.ABORT),
    (MigrationState.LEG_MOVE, MigrationEventType.LEG_BURST_DONE, False, MigrationState.LEG_SETTLE),
    (
        MigrationState.LEG_MOVE,
        MigrationEventType.CORE_DAMAGED,
        False,
        MigrationState.DEFENSIVE_HOLD,
    ),
    (MigrationState.LEG_SETTLE, MigrationEventType.LEG_SETTLE_DONE, False, MigrationState.LEG_MOVE),
    (MigrationState.LEG_SETTLE, MigrationEventType.LEG_SETTLE_DONE, True, MigrationState.ARRIVED),
    (
        MigrationState.LEG_SETTLE,
        MigrationEventType.CORE_DAMAGED,
        False,
        MigrationState.DEFENSIVE_HOLD,
    ),
    (MigrationState.LEG_SETTLE, MigrationEventType.REPLAN_REQUESTED, False, MigrationState.PLAN),
    (MigrationState.LEG_SETTLE, MigrationEventType.THREAT_ESCALATED, False, MigrationState.ABORT),
    (
        MigrationState.DEFENSIVE_HOLD,
        MigrationEventType.THREAT_CLEARED,
        False,
        MigrationState.LEG_SETTLE,
    ),
    (
        MigrationState.DEFENSIVE_HOLD,
        MigrationEventType.THREAT_ESCALATED,
        False,
        MigrationState.ABORT,
    ),
    (
        MigrationState.DEFENSIVE_HOLD,
        MigrationEventType.REPLAN_REQUESTED,
        False,
        MigrationState.PLAN,
    ),
    (MigrationState.ARRIVED, MigrationEventType.ARRIVED_SETTLE_DONE, False, MigrationState.IDLE),
    (MigrationState.ABORT, MigrationEventType.CLEANED, False, MigrationState.IDLE),
    (MigrationState.RECOVERY_ABORT, MigrationEventType.RECOVERY_DONE, False, MigrationState.IDLE),
]

_LEGAL_KEYS = {(state, event_type) for state, event_type, _, _ in LEGAL_TRANSITIONS}


def event(event_type: MigrationEventType, *, last_leg: bool = False) -> MigrationEvent:
    return MigrationEvent(type=event_type, last_leg=last_leg)


@pytest.mark.parametrize(("state", "event_type", "last_leg", "expected"), LEGAL_TRANSITIONS)
def test_legal_transition_table(
    state: MigrationState,
    event_type: MigrationEventType,
    last_leg: bool,
    expected: MigrationState,
) -> None:
    assert transition(state, event(event_type, last_leg=last_leg)) == expected


def test_all_states_covered_by_the_transition_table() -> None:
    sources = {state for state, _, _, _ in LEGAL_TRANSITIONS}
    assert sources == set(ALL_STATES)


def _expected(
    state: MigrationState, event_type: MigrationEventType, last_leg: bool
) -> MigrationState:
    """Reference oracle: LEGAL_TRANSITIONS plus the §2 precedence rules."""
    if event_type in (
        MigrationEventType.CORE_DESTROYED,
        MigrationEventType.CORE_GENERATION_CHANGED,
    ):
        return MigrationState.RECOVERY_ABORT if state in _ACTIVE else state
    if event_type == MigrationEventType.CANCEL:
        return MigrationState.ABORT if state not in _CANCEL_NOOP else state
    for source, kind, flag, expected in LEGAL_TRANSITIONS:
        if source == state and kind == event_type:
            if kind == MigrationEventType.LEG_SETTLE_DONE and flag != last_leg:
                continue
            return expected
    return state


def test_full_transition_table_sweep_covers_every_state_event_pair() -> None:
    # Every (state, event, last_leg) combination, including illegal no-ops.
    for state in ALL_STATES:
        for event_type in ALL_EVENTS:
            for last_leg in (False, True):
                expected = _expected(state, event_type, last_leg)
                assert transition(state, event(event_type, last_leg=last_leg)) == expected


def test_main_chain_idle_to_arrived_to_idle() -> None:
    state = MigrationState.IDLE
    state = transition(state, event(MigrationEventType.INTENT_ACCEPTED))
    assert state == MigrationState.PLAN
    state = transition(state, event(MigrationEventType.PLAN_AUDITED))
    assert state == MigrationState.LEG_MOVE
    state = transition(state, event(MigrationEventType.LEG_BURST_DONE))
    assert state == MigrationState.LEG_SETTLE
    state = transition(state, event(MigrationEventType.LEG_SETTLE_DONE, last_leg=False))
    assert state == MigrationState.LEG_MOVE
    state = transition(state, event(MigrationEventType.LEG_BURST_DONE))
    assert state == MigrationState.LEG_SETTLE
    state = transition(state, event(MigrationEventType.LEG_SETTLE_DONE, last_leg=True))
    assert state == MigrationState.ARRIVED
    state = transition(state, event(MigrationEventType.ARRIVED_SETTLE_DONE))
    assert state == MigrationState.IDLE


def test_abort_two_phase_cleanup_returns_to_idle() -> None:
    state = transition(MigrationState.PLAN, event(MigrationEventType.PLAN_REJECTED))
    assert state == MigrationState.ABORT
    state = transition(state, event(MigrationEventType.CLEANED))
    assert state == MigrationState.IDLE

    state = transition(MigrationState.LEG_MOVE, event(MigrationEventType.CANCEL))
    assert state == MigrationState.ABORT
    state = transition(state, event(MigrationEventType.CLEANED))
    assert state == MigrationState.IDLE


def test_recovery_abort_two_phase_cleanup_returns_to_idle() -> None:
    state = transition(MigrationState.LEG_SETTLE, event(MigrationEventType.CORE_DESTROYED))
    assert state == MigrationState.RECOVERY_ABORT
    state = transition(state, event(MigrationEventType.RECOVERY_DONE))
    assert state == MigrationState.IDLE


def test_core_damaged_enters_defensive_hold_from_move_and_settle() -> None:
    for source in (MigrationState.LEG_MOVE, MigrationState.LEG_SETTLE):
        assert (
            transition(source, event(MigrationEventType.CORE_DAMAGED))
            == MigrationState.DEFENSIVE_HOLD
        )


def test_defensive_hold_hysteresis_exits_to_settle() -> None:
    # 滞回退出：威胁清除 → 回 LEG_SETTLE（不直接 LEG_MOVE）。
    assert (
        transition(MigrationState.DEFENSIVE_HOLD, event(MigrationEventType.THREAT_CLEARED))
        == MigrationState.LEG_SETTLE
    )
    # 敌核贴脸持续 → ABORT；走廊偏离/重复进入 → REPLAN。
    assert (
        transition(MigrationState.DEFENSIVE_HOLD, event(MigrationEventType.THREAT_ESCALATED))
        == MigrationState.ABORT
    )
    assert (
        transition(MigrationState.DEFENSIVE_HOLD, event(MigrationEventType.REPLAN_REQUESTED))
        == MigrationState.PLAN
    )


@pytest.mark.parametrize("source", sorted(_ACTIVE))
def test_core_destroyed_forces_recovery_abort_from_any_active_phase(
    source: MigrationState,
) -> None:
    assert (
        transition(source, event(MigrationEventType.CORE_DESTROYED))
        == MigrationState.RECOVERY_ABORT
    )
    assert (
        transition(source, event(MigrationEventType.CORE_GENERATION_CHANGED))
        == MigrationState.RECOVERY_ABORT
    )


@pytest.mark.parametrize("source", sorted(_CANCEL_NOOP))
def test_cancel_is_noop_in_terminal_or_not_started_states(source: MigrationState) -> None:
    assert transition(source, event(MigrationEventType.CANCEL)) == source


@pytest.mark.parametrize("source", sorted(_ACTIVE))
def test_cancel_aborts_from_any_active_phase(source: MigrationState) -> None:
    assert transition(source, event(MigrationEventType.CANCEL)) == MigrationState.ABORT


def test_recovery_takes_precedence_over_cancel() -> None:
    # CORE_GENERATION_CHANGED 与 CANCEL 同属可中止事件时，恢复中止优先。
    state = transition(MigrationState.LEG_MOVE, event(MigrationEventType.CORE_GENERATION_CHANGED))
    assert state == MigrationState.RECOVERY_ABORT


@pytest.mark.parametrize("bad_state", ["NOT_A_STATE", "", "PLANX", 7, None])
def test_unknown_state_is_rejected(bad_state: object) -> None:
    with pytest.raises(ValueError, match="unknown migration state"):
        transition(cast(Any, bad_state), event(MigrationEventType.INTENT_ACCEPTED))


@pytest.mark.parametrize("bad_event", ["NOT_AN_EVENT", "", "CANCELX", 7, None])
def test_unknown_event_is_rejected(bad_event: object) -> None:
    with pytest.raises(ValueError, match="unknown migration event"):
        transition(MigrationState.IDLE, cast(Any, bad_event))


def test_unknown_event_type_value_is_rejected_by_enum() -> None:
    with pytest.raises(ValueError):
        MigrationEvent(type=cast(Any, "BOGUS"))
