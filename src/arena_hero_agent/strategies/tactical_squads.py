"""Tactical squad formation and rally points (legacy tactical-squads).

Forms stable squads (HOME_DEFENSE 2V+1R, STRIKE 2V+1R, MOBILE remainder) with
sticky previous membership, then derives deterministic rally slots and points.
Pure and deterministic; the planner config gates whether this layer is used.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from arena_hero_agent.domain import Coordinate, UnitRole, chebyshev, manhattan

from ..planning.planning_snapshot import PlanningUnit

RALLY_SLOT_DISTANCE = 5
RALLY_SLOT_COUNT = 8
RALLY_SQUAD_MEMBER_COUNT = 3
RALLY_MEMBER_SLOT_COUNT = 24
RALLY_MEMBER_RING_COUNT = 3

_RALLY_OFFSETS = (
    (1, 0),
    (-1, 0),
    (0, 1),
    (0, -1),
    (1, 1),
    (1, -1),
    (-1, 1),
    (-1, -1),
)


@dataclass(frozen=True, slots=True)
class TacticalSquad:
    """One formed squad with stable identity and typed member lists."""

    __canonical_name__ = "arena-hero.tactical-squad.v1"

    id: str
    role: str
    index: int
    vanguard_ids: tuple[str, ...]
    ranger_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("squad id must be a non-empty string")
        if self.role not in ("HOME_DEFENSE", "STRIKE", "MOBILE"):
            raise ValueError(f"unsupported squad role {self.role!r}")
        if isinstance(self.index, bool) or not isinstance(self.index, int):
            raise TypeError("squad index must be an integer")
        if self.index < 0:
            raise ValueError("squad index cannot be negative")

    @property
    def member_ids(self) -> tuple[str, ...]:
        return self.vanguard_ids + self.ranger_ids


@dataclass(frozen=True, slots=True)
class SquadMembership:
    """Formed squads plus the sticky unit-to-squad mapping."""

    __canonical_name__ = "arena-hero.squad-membership.v1"

    squads: tuple[TacticalSquad, ...] = ()
    squad_by_unit: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.squads, tuple) or any(
            not isinstance(squad, TacticalSquad) for squad in self.squads
        ):
            raise TypeError("squads must be a tuple of TacticalSquad")
        if not isinstance(self.squad_by_unit, Mapping):
            raise TypeError("squad_by_unit must be a Mapping")


EMPTY_SQUAD_MEMBERSHIP = SquadMembership()


@dataclass(frozen=True, slots=True)
class _FleetPlan:
    id: str
    role: str
    index: int


def _partition_local_fleets(
    vanguards: tuple[PlanningUnit, ...],
    rangers: tuple[PlanningUnit, ...],
    tenant_id: str,
) -> tuple[_FleetPlan, ...]:
    ordered_v = tuple(sorted(vanguards, key=lambda unit: unit.id.value))
    ordered_r = tuple(sorted(rangers, key=lambda unit: unit.id.value))
    if not ordered_v and not ordered_r:
        return ()

    fleets: list[_FleetPlan] = []
    v = list(ordered_v)
    r = list(ordered_r)

    def take(units: list[PlanningUnit], count: int) -> list[PlanningUnit]:
        taken = units[:count]
        del units[:count]
        return taken

    home_v = take(v, 2)
    home_r = take(r, 1)
    global_index = 0
    if home_v or home_r:
        fleets.append(_FleetPlan(f"{tenant_id}:home:0", "HOME_DEFENSE", global_index))
        global_index += 1

    strike_index = 0
    while len(v) + len(r) >= 2:
        strike_v = take(v, min(2, len(v)))
        strike_r = take(r, min(2 - len(strike_v), len(r)) if len(strike_v) < 2 else 1)
        while len(strike_v) + len(strike_r) < 3 and v:
            strike_v.extend(take(v, 1))
        while len(strike_v) + len(strike_r) < 3 and r:
            strike_r.extend(take(r, 1))
        if strike_v or strike_r:
            fleets.append(
                _FleetPlan(
                    f"{tenant_id}:strike:{strike_index}",
                    "STRIKE",
                    global_index,
                )
            )
            global_index += 1
            strike_index += 1

    if v or r:
        fleets.append(_FleetPlan(f"{tenant_id}:mobile:0", "MOBILE", 0))
    return tuple(fleets)


def _role_caps(role: str, home_vanguards: int, home_rangers: int) -> tuple[int, int] | None:
    if role == "HOME_DEFENSE":
        return home_vanguards, home_rangers
    if role == "STRIKE":
        return 2, 1
    return None


def _count_type(
    members: tuple[str, ...],
    unit_by_id: Mapping[str, PlanningUnit],
    role: UnitRole,
) -> int:
    return sum(1 for member in members if unit_by_id[member].unit_role is role)


def _can_add(
    role: str,
    caps: tuple[int, int] | None,
    members: tuple[str, ...],
    unit_id: str,
    unit_by_id: Mapping[str, PlanningUnit],
) -> bool:
    unit_role = unit_by_id[unit_id].unit_role
    if caps is not None and role == "HOME_DEFENSE":
        if unit_role is UnitRole.VANGUARD:
            return _count_type(members, unit_by_id, UnitRole.VANGUARD) < caps[0]
        if unit_role is UnitRole.RANGER:
            return _count_type(members, unit_by_id, UnitRole.RANGER) < caps[1]
        return False
    if role == "STRIKE":
        if len(members) >= 3:
            return False
        if unit_role is UnitRole.VANGUARD:
            return _count_type(members, unit_by_id, UnitRole.VANGUARD) < 2
        if unit_role is UnitRole.RANGER:
            return _count_type(members, unit_by_id, UnitRole.RANGER) < 2
        return False
    return True


def _sort_by_anchor(
    units: tuple[PlanningUnit, ...],
    anchor: Coordinate | None,
) -> list[PlanningUnit]:
    if anchor is None:
        return sorted(units, key=lambda unit: unit.id.value)
    return sorted(
        units,
        key=lambda unit: (
            chebyshev(unit.position, anchor),
            unit.id.value,
        ),
    )


def reconcile_tactical_squads(
    units: tuple[PlanningUnit, ...],
    previous: Mapping[str, str] | None,
    tenant_id: str,
    *,
    home_vanguards: int = 2,
    home_rangers: int = 1,
    home_anchor: Coordinate | None = None,
) -> SquadMembership:
    """Form tactical squads from the controlled units, preserving sticky members."""

    if not isinstance(units, tuple):
        raise TypeError("units must be a tuple of PlanningUnit")
    if previous is not None and not isinstance(previous, Mapping):
        raise TypeError("previous must be a Mapping or None")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("tenant_id must be a non-empty string")
    if not units:
        return EMPTY_SQUAD_MEMBERSHIP

    vanguards = tuple(unit for unit in units if unit.unit_role is UnitRole.VANGUARD)
    rangers = tuple(unit for unit in units if unit.unit_role is UnitRole.RANGER)
    desired = _partition_local_fleets(vanguards, rangers, tenant_id)
    if not desired:
        return EMPTY_SQUAD_MEMBERSHIP

    unit_by_id = {unit.id.value: unit for unit in units}
    alive = frozenset(unit.id.value for unit in units)
    prev_members: dict[str, list[str]] = {}
    if previous is not None:
        for unit_id, squad_id in previous.items():
            if unit_id not in alive:
                continue
            members = prev_members.setdefault(squad_id, [])
            if unit_id not in members:
                members.append(unit_id)

    assigned: set[str] = set()
    squads: list[TacticalSquad] = []

    for fleet in desired:
        if fleet.role == "MOBILE":
            continue
        caps = _role_caps(fleet.role, home_vanguards, home_rangers)
        members: list[str] = []
        for unit_id in prev_members.get(fleet.id, []):
            if unit_id in assigned:
                continue
            if not _can_add(fleet.role, caps, tuple(members), unit_id, unit_by_id):
                continue
            members.append(unit_id)
            assigned.add(unit_id)
        unassigned_units = tuple(unit for unit in units if unit.id.value not in assigned)
        if fleet.role == "HOME_DEFENSE":
            pool = _sort_by_anchor(unassigned_units, home_anchor)
        else:
            pool = [
                *sorted(
                    (unit for unit in unassigned_units if unit.unit_role is UnitRole.VANGUARD),
                    key=lambda unit: unit.id.value,
                ),
                *sorted(
                    (unit for unit in unassigned_units if unit.unit_role is UnitRole.RANGER),
                    key=lambda unit: unit.id.value,
                ),
            ]
        for unit in pool:
            if unit.id.value in assigned:
                continue
            if not _can_add(fleet.role, caps, tuple(members), unit.id.value, unit_by_id):
                continue
            members.append(unit.id.value)
            assigned.add(unit.id.value)
        if not members:
            continue
        squads.append(_build_squad(fleet.id, fleet.role, fleet.index, tuple(members), unit_by_id))

    leftover = sorted(
        (unit for unit in units if unit.id.value not in assigned),
        key=lambda unit: unit.id.value,
    )
    if leftover:
        mobile = next((fleet for fleet in desired if fleet.role == "MOBILE"), None)
        mobile_id = mobile.id if mobile is not None else f"{tenant_id}:mobile:0"
        mobile_index = desired.index(mobile) if mobile is not None else len(squads)
        squads.append(
            _build_squad(
                mobile_id,
                "MOBILE",
                mobile_index,
                tuple(unit.id.value for unit in leftover),
                unit_by_id,
            )
        )

    squad_by_unit: dict[str, str] = {}
    for squad in squads:
        for member in squad.member_ids:
            squad_by_unit[member] = squad.id
    return SquadMembership(squads=tuple(squads), squad_by_unit=squad_by_unit)


def _build_squad(
    squad_id: str,
    role: str,
    index: int,
    members: tuple[str, ...],
    unit_by_id: Mapping[str, PlanningUnit],
) -> TacticalSquad:
    vanguard_ids = tuple(
        sorted(
            (member for member in members if unit_by_id[member].unit_role is UnitRole.VANGUARD),
            key=lambda value: value,
        )
    )
    ranger_ids = tuple(
        sorted(
            (member for member in members if unit_by_id[member].unit_role is UnitRole.RANGER),
            key=lambda value: value,
        )
    )
    return TacticalSquad(
        id=squad_id,
        role=role,
        index=index,
        vanguard_ids=vanguard_ids,
        ranger_ids=ranger_ids,
    )


def rally_slot_for_squad(squad_index: int) -> int:
    """Map a squad order index to a rally slot in 0..7."""

    if isinstance(squad_index, bool) or not isinstance(squad_index, int):
        raise TypeError("squad_index must be an integer")
    return squad_index % RALLY_SLOT_COUNT


def rally_point_at_slot(
    target: Coordinate,
    home: Coordinate,
    obstacles: frozenset[str],
    resource_cells: frozenset[str],
    slot: int,
) -> Coordinate:
    """Pick a rally point near the target, rotating the 8-direction candidates."""

    if not isinstance(target, Coordinate) or not isinstance(home, Coordinate):
        raise TypeError("target and home must be Coordinate values")
    if not isinstance(obstacles, frozenset) or not isinstance(resource_cells, frozenset):
        raise TypeError("obstacles and resource_cells must be frozensets of cell keys")
    candidates = _sorted_rally_candidates(target, home, RALLY_SLOT_DISTANCE)
    start = rally_slot_for_squad(slot)
    for offset in range(len(candidates)):
        candidate = candidates[(start + offset) % len(candidates)]
        if candidate.cell_key in obstacles:
            continue
        if candidate.cell_key in resource_cells:
            continue
        return candidate
    return target


def rally_member_slot(squad_index: int, member_index: int) -> int:
    """Map squad index plus member ordinal to a unique rally slot in 0..23."""

    if isinstance(squad_index, bool) or not isinstance(squad_index, int):
        raise TypeError("squad_index must be an integer")
    if isinstance(member_index, bool) or not isinstance(member_index, int):
        raise TypeError("member_index must be an integer")
    return (squad_index * RALLY_SQUAD_MEMBER_COUNT + member_index) % RALLY_MEMBER_SLOT_COUNT


def rally_point_at_member_slot(
    target: Coordinate,
    home: Coordinate,
    obstacles: frozenset[str],
    resource_cells: frozenset[str],
    slot: int,
) -> Coordinate:
    """Pick a per-member rally point on ring slots 5, 6, and 7 around the target."""

    if not isinstance(target, Coordinate) or not isinstance(home, Coordinate):
        raise TypeError("target and home must be Coordinate values")
    if not isinstance(obstacles, frozenset) or not isinstance(resource_cells, frozenset):
        raise TypeError("obstacles and resource_cells must be frozensets of cell keys")
    ring = slot // RALLY_SLOT_COUNT
    direction = slot % RALLY_SLOT_COUNT
    distance = RALLY_SLOT_DISTANCE + ring
    candidates = _sorted_rally_candidates(target, home, distance)
    for offset in range(len(candidates)):
        candidate = candidates[(direction + offset) % len(candidates)]
        if candidate.cell_key in obstacles:
            continue
        if candidate.cell_key in resource_cells:
            continue
        return candidate
    return target


def _sorted_rally_candidates(
    target: Coordinate,
    home: Coordinate,
    distance: int,
) -> list[Coordinate]:
    candidates = [
        Coordinate(target.x + dx * distance, target.y + dy * distance) for dx, dy in _RALLY_OFFSETS
    ]
    candidates.sort(key=lambda cell: manhattan(cell, home))
    return candidates
