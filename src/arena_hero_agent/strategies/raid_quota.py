"""Third-party raid/assassination quota accounting plus formation wiring.

Absorbs the offensive strategies from the referenced third-party
implementations as self-contained pure functions:

- independent ``CORE_ASSAULT`` squad quota (1V+2R) that never borrows from the
  2V+1R home-defense reserve, plus raid recall/lifecycle state;
- decapitation strike-group damage ledger that commits rangers first and backs
  out vanguards from the enemy's remaining HP to avoid overkill;
- per-role replacement queue that enqueues lost-unit roles and drains them as
  replacements are produced;
- stationary enemy-core raid confirmation (consecutive observations >= N,
  distance <= M, fighters >= 3) that keeps the home-defense squad behind.

Everything is deterministic with no randomness and no I/O. This layer is not
wired into the composed decider yet; the main session may import the public
functions below when integrating into ``ComposedDecider``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from arena_hero_agent.domain import Coordinate, manhattan

from .tactical_squads import TacticalSquad

CORE_ASSAULT_VANGUARDS: Final = 1
CORE_ASSAULT_RANGERS: Final = 2
HOME_DEFENSE_VANGUARDS: Final = 2
HOME_DEFENSE_RANGERS: Final = 1

STRIKE_VANGUARD_RESERVE: Final = 2
STRIKE_RANGER_RESERVE: Final = 2
STRIKE_RANGER_CAP: Final = 2

RAID_MIN_OBSERVATIONS: Final = 3
RAID_MAX_DISTANCE: Final = 40
RAID_MIN_FIGHTERS: Final = 3


@dataclass(frozen=True, slots=True)
class UnitQuota:
    """Committed vanguard and ranger counts for one operation."""

    vanguard_count: int = 0
    ranger_count: int = 0

    @property
    def total(self) -> int:
        return self.vanguard_count + self.ranger_count


@dataclass(frozen=True, slots=True)
class StrikeGroup:
    """Selected unit ids for a strike or assault group."""

    vanguard_ids: tuple[str, ...] = ()
    ranger_ids: tuple[str, ...] = ()

    @property
    def member_ids(self) -> tuple[str, ...]:
        return self.vanguard_ids + self.ranger_ids


@dataclass(frozen=True, slots=True)
class RaidState:
    """Lifecycle snapshot of one raid against a stationary enemy core."""

    enabled: bool = False
    recall: bool = False
    vanguard_ids: frozenset[str] = frozenset()
    ranger_ids: frozenset[str] = frozenset()
    core_id: str | None = None
    core_position: Coordinate | None = None
    acquired_tick: int | None = None


@dataclass(frozen=True, slots=True)
class ReplacementQueue:
    """Per-role replacement backlog; only positive counts are retained."""

    counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = {role: count for role, count in self.counts.items() if count > 0}
        object.__setattr__(self, "counts", normalized)

    def missing(self, role: str) -> int:
        return self.counts.get(role, 0)

    def to_mapping(self) -> dict[str, int]:
        return dict(self.counts)


@dataclass(frozen=True, slots=True)
class StationaryCore:
    """A repeatedly observed stationary enemy core."""

    key: str
    position: Coordinate
    observations: int = 1


def core_assault_quota(
    available_vanguards: int,
    available_rangers: int,
    *,
    home_vanguards: int = HOME_DEFENSE_VANGUARDS,
    home_rangers: int = HOME_DEFENSE_RANGERS,
    assault_vanguards: int = CORE_ASSAULT_VANGUARDS,
    assault_rangers: int = CORE_ASSAULT_RANGERS,
) -> UnitQuota:
    """Count the independent CORE_ASSAULT squad (1V+2R) above the home reserve.

    The home-defense reserve (2V+1R by default) is withheld first; CORE_ASSAULT
    only receives units beyond that reserve, so the home squad is never touched.
    """

    home_v = min(home_vanguards, available_vanguards)
    home_r = min(home_rangers, available_rangers)
    return UnitQuota(
        vanguard_count=min(assault_vanguards, available_vanguards - home_v),
        ranger_count=min(assault_rangers, available_rangers - home_r),
    )


def strike_group_quota(
    target_health: int,
    vanguard_count: int,
    ranger_count: int,
    *,
    vanguard_reserve: int = STRIKE_VANGUARD_RESERVE,
    ranger_reserve: int = STRIKE_RANGER_RESERVE,
    ranger_cap: int = STRIKE_RANGER_CAP,
) -> UnitQuota:
    """Back out strike counts so rangers shoot first and vanguards avoid overkill.

    Rangers deal one damage each and commit first, capped at ``ranger_cap`` while
    reserving ``ranger_reserve``. Remaining target health is covered one damage
    per vanguard above ``vanguard_reserve``.
    """

    ranger_strike = min(ranger_count, ranger_cap, max(1, ranger_count - ranger_reserve))
    remaining_damage = max(0, target_health - ranger_strike)
    vanguard_strike = min(
        vanguard_count,
        remaining_damage,
        max(1, vanguard_count - vanguard_reserve),
    )
    return UnitQuota(vanguard_count=vanguard_strike, ranger_count=ranger_strike)


def select_strike_group(
    vanguard_ids: Sequence[str],
    ranger_ids: Sequence[str],
    quota: UnitQuota,
) -> StrikeGroup:
    """Select the trailing ``quota`` ids from each sorted id sequence."""

    ordered_v = tuple(sorted(vanguard_ids))
    ordered_r = tuple(sorted(ranger_ids))
    return StrikeGroup(
        vanguard_ids=ordered_v[-quota.vanguard_count:] if quota.vanguard_count else (),
        ranger_ids=ordered_r[-quota.ranger_count:] if quota.ranger_count else (),
    )


def raid_guard_ids(home_defense: TacticalSquad | None) -> frozenset[str]:
    """Return home-defense member ids that must stay behind during a raid."""

    if home_defense is None:
        return frozenset()
    return frozenset(home_defense.member_ids)


def raid_active(state: RaidState) -> bool:
    """Return whether a raid is enabled, not recalled, and has members."""

    return state.enabled and not state.recall and bool(state.vanguard_ids or state.ranger_ids)


def recall_raid(state: RaidState) -> RaidState:
    """Set recall and drop the current raid target, keeping members frozen."""

    return replace(state, recall=True, core_id=None, core_position=None, acquired_tick=None)


def clear_raid_target(state: RaidState) -> RaidState:
    """Drop the current raid target while preserving enabled/recall and members."""

    return replace(state, core_id=None, core_position=None, acquired_tick=None)


def acquire_raid_target(
    state: RaidState,
    core_id: str,
    position: Coordinate,
    tick: int,
) -> RaidState:
    """Record a newly acquired raid target on the state."""

    return replace(state, core_id=core_id, core_position=position, acquired_tick=tick)


def pick_raid_target(
    stationary: Mapping[str, StationaryCore],
    core_position: Coordinate,
    *,
    min_observations: int = RAID_MIN_OBSERVATIONS,
    max_distance: int = RAID_MAX_DISTANCE,
) -> Coordinate | None:
    """Pick the nearest confirmed-stationary enemy core within raid distance.

    A core qualifies when it has at least ``min_observations`` consecutive
    stationary observations and its Manhattan distance to ``core_position`` is
    no greater than ``max_distance``.
    """

    best: Coordinate | None = None
    best_distance: int | None = None
    for observation in stationary.values():
        if observation.observations < min_observations:
            continue
        distance = manhattan(observation.position, core_position)
        if distance > max_distance:
            continue
        if best_distance is None or distance < best_distance:
            best = observation.position
            best_distance = distance
    return best


def raid_fighters_ready(
    fighter_count: int,
    *,
    min_fighters: int = RAID_MIN_FIGHTERS,
) -> bool:
    """Return whether enough combat units are available to leave guards behind."""

    return fighter_count >= min_fighters


def reconcile_replacement_queue(
    previous_by_unit: Mapping[str, str],
    current_by_unit: Mapping[str, str],
    queue: ReplacementQueue,
) -> ReplacementQueue:
    """Enqueue lost-unit roles and drain produced-unit roles from the queue."""

    previous = dict(previous_by_unit)
    current = dict(current_by_unit)
    counts = dict(queue.counts)
    for unit_id, role in previous.items():
        if unit_id not in current:
            counts[role] = counts.get(role, 0) + 1
    for unit_id, role in current.items():
        if unit_id not in previous:
            counts[role] = counts.get(role, 0) - 1
    return ReplacementQueue(counts)


def replacement_gap_order(
    queue: ReplacementQueue,
    role_order: Sequence[str],
) -> tuple[str, ...]:
    """Order remaining replacement gaps by descending count, then role order."""

    ranked = {role: index for index, role in enumerate(role_order)}
    gaps = [(role, count) for role, count in queue.counts.items() if count > 0]
    gaps.sort(key=lambda item: (-item[1], ranked.get(item[0], len(ranked)), item[0]))
    return tuple(role for role, _ in gaps)


__all__ = [
    "CORE_ASSAULT_RANGERS",
    "CORE_ASSAULT_VANGUARDS",
    "HOME_DEFENSE_RANGERS",
    "HOME_DEFENSE_VANGUARDS",
    "RAID_MAX_DISTANCE",
    "RAID_MIN_FIGHTERS",
    "RAID_MIN_OBSERVATIONS",
    "STRIKE_RANGER_CAP",
    "STRIKE_RANGER_RESERVE",
    "STRIKE_VANGUARD_RESERVE",
    "RaidState",
    "ReplacementQueue",
    "StationaryCore",
    "StrikeGroup",
    "UnitQuota",
    "acquire_raid_target",
    "clear_raid_target",
    "core_assault_quota",
    "pick_raid_target",
    "raid_active",
    "raid_fighters_ready",
    "raid_guard_ids",
    "recall_raid",
    "reconcile_replacement_queue",
    "replacement_gap_order",
    "select_strike_group",
    "strike_group_quota",
]
