"""Pure conductor migration state machine (migration-system-v1 §2).

The state machine is deliberately free of I/O and side effects. Every legal
transition is enumerated by :func:`transition`; anything unknown is rejected
(``ValueError``, fail-closed) and any legal-but-out-of-order event is a no-op
that returns the input state unchanged, so stale or replayed events can never
advance a migration.

中止分级（评审定稿）:
- CORE_DAMAGED = 暂停（DEFENSIVE_HOLD，可恢复，滞回退出）;
- 活跃敌核贴脸/取消 = ABORT（路线/目标失效重审）;
- CORE_DESTROYED / core 代际变化 = RECOVERY_ABORT（禁止旧 legProgress 续迁）.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MigrationState(StrEnum):
    """Conductor-visible migration phases from the §2 state table."""

    __canonical_name__ = "arena-hero.migration-state.v1"

    IDLE = "IDLE"
    PLAN = "PLAN"
    LEG_MOVE = "LEG_MOVE"
    LEG_SETTLE = "LEG_SETTLE"
    DEFENSIVE_HOLD = "DEFENSIVE_HOLD"
    RECOVERY_ABORT = "RECOVERY_ABORT"
    ARRIVED = "ARRIVED"
    ABORT = "ABORT"


class MigrationEventType(StrEnum):
    """Events accepted by :func:`transition` (§2 event list)."""

    __canonical_name__ = "arena-hero.migration-event-type.v1"

    INTENT_ACCEPTED = "INTENT_ACCEPTED"
    PLAN_AUDITED = "PLAN_AUDITED"
    PLAN_REJECTED = "PLAN_REJECTED"
    LEG_BURST_DONE = "LEG_BURST_DONE"
    LEG_SETTLE_DONE = "LEG_SETTLE_DONE"
    CORE_DAMAGED = "CORE_DAMAGED"
    THREAT_CLEARED = "THREAT_CLEARED"
    THREAT_ESCALATED = "THREAT_ESCALATED"
    REPLAN_REQUESTED = "REPLAN_REQUESTED"
    CORE_DESTROYED = "CORE_DESTROYED"
    CORE_GENERATION_CHANGED = "CORE_GENERATION_CHANGED"
    CANCEL = "CANCEL"
    ARRIVED_SETTLE_DONE = "ARRIVED_SETTLE_DONE"
    CLEANED = "CLEANED"
    RECOVERY_DONE = "RECOVERY_DONE"


@dataclass(frozen=True, slots=True)
class MigrationEvent:
    """Immutable event payload; ``last_leg`` only drives LEG_SETTLE_DONE.

    StrEnum eagerly creates ad-hoc members for unknown string values, so the
    known-member check below is required to keep unknown events fail-closed.
    """

    __canonical_name__ = "arena-hero.migration-event.v1"

    type: MigrationEventType
    last_leg: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.type, MigrationEventType) or self.type not in MigrationEventType:
            raise ValueError(f"unknown migration event type: {self.type!r}")


#: States that can be interrupted by core destruction / generation change.
_ACTIVE_PHASES = frozenset(
    {
        MigrationState.PLAN,
        MigrationState.LEG_MOVE,
        MigrationState.LEG_SETTLE,
        MigrationState.DEFENSIVE_HOLD,
    }
)

#: States where CANCEL is a no-op (already terminal or not yet started).
_CANCEL_NOOP = frozenset(
    {
        MigrationState.IDLE,
        MigrationState.ABORT,
        MigrationState.RECOVERY_ABORT,
        MigrationState.ARRIVED,
    }
)


def transition(state: MigrationState, event: MigrationEvent) -> MigrationState:
    """Return the next state for one pure state-machine step.

    Fail-closed rules:
    - Unknown state or event values raise ``ValueError`` (rejected).
    - Known states/events that do not form a legal transition return the input
      state unchanged (no-op), so a migration never advances on stale input.
    - Core destruction / generation change takes precedence over every other
      event while the migration is in an active phase.
    """
    if not isinstance(state, MigrationState):
        raise ValueError(f"unknown migration state: {state!r}")
    if not isinstance(event, MigrationEvent):
        raise ValueError(f"unknown migration event: {event!r}")

    if event.type in (
        MigrationEventType.CORE_DESTROYED,
        MigrationEventType.CORE_GENERATION_CHANGED,
    ):
        if state in _ACTIVE_PHASES:
            return MigrationState.RECOVERY_ABORT
        return state

    if event.type == MigrationEventType.CANCEL:
        if state not in _CANCEL_NOOP:
            return MigrationState.ABORT
        return state

    if state == MigrationState.IDLE:
        if event.type == MigrationEventType.INTENT_ACCEPTED:
            return MigrationState.PLAN
        return state

    if state == MigrationState.PLAN:
        if event.type == MigrationEventType.PLAN_AUDITED:
            return MigrationState.LEG_MOVE
        if event.type == MigrationEventType.PLAN_REJECTED:
            return MigrationState.ABORT
        return state

    if state == MigrationState.LEG_MOVE:
        if event.type == MigrationEventType.LEG_BURST_DONE:
            return MigrationState.LEG_SETTLE
        if event.type == MigrationEventType.CORE_DAMAGED:
            return MigrationState.DEFENSIVE_HOLD
        return state

    if state == MigrationState.LEG_SETTLE:
        if event.type == MigrationEventType.LEG_SETTLE_DONE:
            return MigrationState.ARRIVED if event.last_leg else MigrationState.LEG_MOVE
        if event.type == MigrationEventType.CORE_DAMAGED:
            return MigrationState.DEFENSIVE_HOLD
        if event.type == MigrationEventType.REPLAN_REQUESTED:
            return MigrationState.PLAN
        if event.type == MigrationEventType.THREAT_ESCALATED:
            return MigrationState.ABORT
        return state

    if state == MigrationState.DEFENSIVE_HOLD:
        if event.type == MigrationEventType.THREAT_CLEARED:
            return MigrationState.LEG_SETTLE
        if event.type == MigrationEventType.THREAT_ESCALATED:
            return MigrationState.ABORT
        if event.type == MigrationEventType.REPLAN_REQUESTED:
            return MigrationState.PLAN
        return state

    if state == MigrationState.ARRIVED:
        if event.type == MigrationEventType.ARRIVED_SETTLE_DONE:
            return MigrationState.IDLE
        return state

    if state == MigrationState.ABORT:
        if event.type == MigrationEventType.CLEANED:
            return MigrationState.IDLE
        return state

    if state == MigrationState.RECOVERY_ABORT:
        if event.type == MigrationEventType.RECOVERY_DONE:
            return MigrationState.IDLE
        return state

    return state


__all__ = [
    "MigrationEvent",
    "MigrationEventType",
    "MigrationState",
    "transition",
]
