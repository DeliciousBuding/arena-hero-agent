"""Deterministic safety planner (P4-11 core) and direction helpers.

The legacy TypeScript ``SafetyPlanner`` is stateful and spans thousands of lines.
This Python core is the deterministic, fail-closed composition of the migrated
helper behaviors:

- workers follow the forced-task contract (``forced_task_for``) and the legacy
  task-to-action conversion (DEPOSIT returns home, HARVEST_CURRENT harvests,
  PICKUP_BEACON picks up, RETURN_FOR_HEAL walks home);
- vanguards sweep adjacent hostiles, otherwise guard from a defense post;
- rangers shoot the nearest defensive-priority hostile in line of sight,
  otherwise guard;
- the Core spawns the next unit when affordable.

The composition is intentionally simpler than the stateful oracle and is
registered as an ALLOWED_DIFFERENCE in the behavior-difference registry; every
helper it calls is fixture-compared against the TypeScript oracle. A deterministic
unit-action budget bounds the work and falls back to safe WAIT actions when
exhausted, so the planner is always bounded and reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cmp_to_key
from typing import Final

from arena_hero_agent.domain import (
    CURRENT_RULES_VERSION,
    Coordinate,
    Direction,
    EntityId,
    UnitRole,
    assert_current_rules_version,
    direction_to_adjacent,
    manhattan,
    unit_price,
)

from ..planning.plan import CoreAction, CoreActionType, Plan, UnitAction, UnitActionType
from ..planning.planning_snapshot import PlanningSnapshot, PlanningUnit
from ..planning.task import TaskType, forced_task_for
from .safety_helpers import (
    VisibleEnemy,
    can_shoot,
    defense_post,
    defensive_shot_priority,
    home_cell,
    next_spawn,
    next_spawn_massarmy,
    predicted_enemy_cell,
)
from .safety_planner_config import DEFAULT_SAFETY_CONFIG, SafetyPlannerConfig

EXPLORE_DIRECTION_COUNT = 8

# Official core attributes (rules/core-and-economy.md): HP 5, shield 5.
CORE_MAX_HP: Final = 5
CORE_MAX_SHIELD: Final = 5
# Heal once at or below this HP, keeping enough resources for the next Worker
# (base price 5) plus a small margin after HEAL resolves.
CRITICAL_CORE_HP: Final = 2
CRITICAL_HEAL_MIN_RESOURCES: Final = 7
# One shield point costs exactly one resource; repair only when idle at full HP.
SHIELD_REPAIR_MIN_RESOURCES: Final = 6
# Threat-triggered Vanguard: base price 10 plus a 6-resource reserve so the
# economy keeps rolling after the emergency spawn, and only once the economy
# already fields THREAT_VANGUARD_MIN_WORKERS workers (a dense-map tenant was
# stuck at two workers forever because every 13 resources bought a Vanguard).
THREAT_VANGUARD_MIN_WORKERS: Final = 4
THREAT_VANGUARD_MIN_RESOURCES: Final = 16
# Official Champion Beacon shield cap (rules/champion-beacon.md): the
# carrier's Core shield limit rises 5 -> 10 and clamps back on loss.
BEACON_SHIELD_CAP: Final = 10


def _beacon_held_by_us(snapshot: PlanningSnapshot) -> bool:
    """True when our Core (or one of our units) carries the Champion Beacon."""

    carrier = snapshot.beacon.carrier_id
    if carrier is None:
        return False
    if carrier == snapshot.core_id:
        return True
    return any(unit.id == carrier for unit in snapshot.units)


def worker_dense_direction(index: int) -> int:
    """Map a worker ordinal to a dense 16-direction scan slot."""

    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("index must be an integer")
    if index < 0:
        raise ValueError("index cannot be negative")
    if index < 8:
        return ((index * 3 + 7) % 8) * 2
    return (((index - 8) * 3 + 7) % 8) * 2 + 1


def threat_weighted_direction(index: int, threat_sector: int | None) -> int:
    """Weight the first four patrol directions toward the threat sector."""

    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("index must be an integer")
    if index < 0:
        raise ValueError("index cannot be negative")
    if threat_sector is not None and (
        isinstance(threat_sector, bool) or not isinstance(threat_sector, int)
    ):
        raise TypeError("threat_sector must be an integer or None")
    spread = (index * 3 + 7) % EXPLORE_DIRECTION_COUNT
    if threat_sector is None:
        return spread
    if index < 4:
        offset = 0 if index == 0 else 1 if index == 1 else -1 if index == 2 else 2
        return (threat_sector + offset + EXPLORE_DIRECTION_COUNT) % EXPLORE_DIRECTION_COUNT
    return spread


def step_toward(from_position: Coordinate, target: Coordinate) -> Direction:
    """Deterministic one step toward a target: x axis first, then y."""

    if not isinstance(from_position, Coordinate) or not isinstance(target, Coordinate):
        raise TypeError("from_position and target must be Coordinate values")
    dx = target.x - from_position.x
    dy = target.y - from_position.y
    if dx != 0:
        return Direction.EAST if dx > 0 else Direction.WEST
    return Direction.SOUTH if dy > 0 else Direction.NORTH


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    """One deterministic safety plan plus its budget accounting."""

    __canonical_name__ = "arena-hero.safety-decision.v1"

    plan: Plan
    computed_actions: int
    budget_exhausted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.plan, Plan):
            raise TypeError("plan must be a Plan")
        if isinstance(self.computed_actions, bool) or not isinstance(self.computed_actions, int):
            raise TypeError("computed_actions must be an integer")
        if self.computed_actions < 0:
            raise ValueError("computed_actions cannot be negative")
        if not isinstance(self.budget_exhausted, bool):
            raise TypeError("budget_exhausted must be a boolean")


@dataclass(slots=True)
class _PredictiveFireRecord:
    """Bookkeeping for one Ranger's predictive shots (military S4)."""

    enemy_id: str
    enemy_position: Coordinate
    misses: int = 0
    cooldown_until: int = 0


class SafetyPlanner:
    """Deterministic, budget-bounded safety planner for one observed tick."""

    def __init__(self, config: SafetyPlannerConfig = DEFAULT_SAFETY_CONFIG) -> None:
        if not isinstance(config, SafetyPlannerConfig):
            raise TypeError("config must be a SafetyPlannerConfig")
        self._config = config
        # S5 dedup: at most one unit contests the Beacon per tick. Reset at
        # the top of every ``decide`` call so one planner instance can serve
        # the whole live process without leaking state across ticks.
        self._beacon_contest_claimed = False
        # Military S4 predictive fire bookkeeping, keyed by unit id.
        # Persistent across ticks; refreshed from the snapshot at the top of
        # every ``decide`` call.
        self._predictive_fire_records: dict[str, _PredictiveFireRecord] = {}

    @property
    def config(self) -> SafetyPlannerConfig:
        return self._config

    def decide(
        self,
        snapshot: PlanningSnapshot,
        *,
        budget: int | None = None,
    ) -> SafetyDecision:
        """Produce the safety plan for one snapshot, bounded by an action budget."""

        if not isinstance(snapshot, PlanningSnapshot):
            raise TypeError("snapshot must be a PlanningSnapshot")
        assert_current_rules_version(snapshot.rules_version)
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int)):
            raise TypeError("budget must be an integer or None")
        if budget is not None and budget < 0:
            raise ValueError("budget cannot be negative")

        # S5 dedup: exactly one Beacon contestant per tick.
        self._beacon_contest_claimed = False
        # Military S4: settle last tick's predictive shots before deciding.
        self._refresh_predictive_fire_state(snapshot)

        core_action = self._decide_core(snapshot)
        unit_actions: list[UnitAction] = []
        computed = 0
        budget_exhausted = False
        for unit in snapshot.units:
            if budget is not None and computed >= budget:
                budget_exhausted = True
                unit_actions.append(_wait_action(unit))
                continue
            action = self._decide_unit(snapshot, unit)
            unit_actions.append(action)
            computed += 1

        return SafetyDecision(
            plan=Plan(
                tick=snapshot.tick, unit_actions=tuple(unit_actions), core_action=core_action
            ),
            computed_actions=computed,
            budget_exhausted=budget_exhausted,
        )

    def _decide_core(self, snapshot: PlanningSnapshot) -> CoreAction | None:
        if snapshot.core_state != "normal":
            return None
        workers = sum(1 for unit in snapshot.units if unit.unit_role is UnitRole.WORKER)
        vanguards = sum(1 for unit in snapshot.units if unit.unit_role is UnitRole.VANGUARD)
        rangers = sum(1 for unit in snapshot.units if unit.unit_role is UnitRole.RANGER)

        # Survival first: a critically damaged Core heals before any other
        # spending. HEAL auto-continues until full HP or empty resources, so
        # gate it on a reserve that keeps the next Worker affordable
        # (production: t3 took CORE_DAMAGED 10 times in 400 ticks while never
        # healing; a 1-resource-per-HP repair is the cheapest survival layer).
        core_health = snapshot.core_health
        if (
            core_health is not None
            and core_health <= CRITICAL_CORE_HP
            and snapshot.resources >= CRITICAL_HEAL_MIN_RESOURCES
        ):
            return CoreAction(type=CoreActionType.HEAL)

        # Threat response before the economy gate: one Vanguard is the
        # cheapest active defense (HP 4 absorbs four SWEEPs vs two for a
        # Worker, and it sweeps back for 1). It must never starve the
        # economy, though: in a dense map enemies are near constantly, and
        # buying a 10-resource Vanguard every time res >= 13 kept tenants at
        # two workers forever (observed in the FFA bench after 0.1.42). The
        # floor of four workers plus the 16-resource reserve means early
        # income always goes to Workers first.
        if (
            workers >= THREAT_VANGUARD_MIN_WORKERS
            and snapshot.core_position is not None
            and snapshot.enemy_units
        ):
            nearest_enemy_distance = min(
                manhattan(snapshot.core_position, enemy.position)
                for enemy in snapshot.enemy_units
            )
            if (
                nearest_enemy_distance <= self._config.threat_enemy_distance
                and snapshot.resources >= THREAT_VANGUARD_MIN_RESOURCES
            ):
                return CoreAction(type=CoreActionType.SPAWN, unit_role=UnitRole.VANGUARD)

        role = (
            next_spawn_massarmy(workers, vanguards, rangers, snapshot.population)
            if self._config.massarmy_stages
            else next_spawn(workers, vanguards, rangers, self._config.worker_target, self._config)
        )
        cost = unit_price(role, snapshot.population, CURRENT_RULES_VERSION)
        if snapshot.resources >= cost:
            return CoreAction(type=CoreActionType.SPAWN, unit_role=role)

        # Shield repair only when idle and at full HP: one resource per shield
        # point, keeping the Core at max defense before the next raid window.
        # Holding the Champion Beacon raises the official shield cap to 10.
        core_shield = snapshot.core_shield
        shield_cap = (
            BEACON_SHIELD_CAP if _beacon_held_by_us(snapshot) else CORE_MAX_SHIELD
        )
        if (
            core_health == CORE_MAX_HP
            and core_shield is not None
            and core_shield < shield_cap
            and snapshot.resources >= SHIELD_REPAIR_MIN_RESOURCES
        ):
            return CoreAction(type=CoreActionType.REPAIR_SHIELD)
        return None

    def _decide_unit(self, snapshot: PlanningSnapshot, unit: PlanningUnit) -> UnitAction:
        # A unit carrying our Beacon parks next to the Core (the official
        # shield cap 10 and double harvest then apply). Cargo-carrying
        # workers still deposit first — the forced-task contract wins.
        if snapshot.beacon.carrier_id == unit.id and not (
            unit.unit_role is UnitRole.WORKER and unit.cargo > 0
        ):
            return self._decide_beacon_carrier(snapshot, unit)
        if unit.unit_role is UnitRole.WORKER:
            return self._decide_worker(snapshot, unit)
        if unit.unit_role is UnitRole.VANGUARD:
            return self._decide_vanguard(snapshot, unit)
        if unit.unit_role is UnitRole.RANGER:
            return self._decide_ranger(snapshot, unit)
        return _wait_action(unit)

    def _decide_beacon_carrier(
        self, snapshot: PlanningSnapshot, unit: PlanningUnit
    ) -> UnitAction:
        """Park a unit carrying our Beacon within the Core hold radius."""

        core = snapshot.core_position
        if core is None:
            return _wait_action(unit)
        if manhattan(unit.position, core) <= self._config.beacon_carrier_hold_radius:
            return _wait_action(unit)
        return UnitAction(
            unit_id=unit.id,
            type=UnitActionType.MOVE,
            direction=step_toward(unit.position, core),
        )

    def _decide_worker(self, snapshot: PlanningSnapshot, unit: PlanningUnit) -> UnitAction:
        task = forced_task_for(unit, snapshot)
        if task is None:
            return _wait_action(unit)
        if task.type is TaskType.DEPOSIT:
            assert task.target is not None
            if unit.position == snapshot.core_position:
                return UnitAction(unit_id=unit.id, type=UnitActionType.DEPOSIT)
            return UnitAction(
                unit_id=unit.id,
                type=UnitActionType.MOVE,
                direction=step_toward(unit.position, task.target),
            )
        if task.type is TaskType.HARVEST_CURRENT:
            return UnitAction(unit_id=unit.id, type=UnitActionType.HARVEST)
        if task.type is TaskType.PICKUP_BEACON:
            return UnitAction(unit_id=unit.id, type=UnitActionType.PICKUP_BEACON)
        if task.type is TaskType.RETURN_FOR_HEAL:
            assert task.target is not None
            return UnitAction(
                unit_id=unit.id,
                type=UnitActionType.MOVE,
                direction=step_toward(unit.position, task.target),
            )
        return _wait_action(unit)

    def _decide_vanguard(self, snapshot: PlanningSnapshot, unit: PlanningUnit) -> UnitAction:
        enemies = _visible_enemies(snapshot)
        for enemy in enemies:
            if manhattan(unit.position, enemy.position) == 1:
                return UnitAction(
                    unit_id=unit.id,
                    type=UnitActionType.SWEEP,
                    direction=direction_to_adjacent(unit.position, enemy.position),
                )
        contest = self._decide_beacon_contest(snapshot, unit)
        if contest is not None:
            return contest
        return _guard_action(snapshot, unit, enemies, UnitRole.VANGUARD)

    def _decide_ranger(self, snapshot: PlanningSnapshot, unit: PlanningUnit) -> UnitAction:
        enemies = _visible_enemies(snapshot)
        ordered = sorted(
            enemies,
            key=cmp_to_key(lambda left, right: defensive_shot_priority(unit.position, left, right)),
        )
        for enemy in ordered:
            if can_shoot(unit.position, enemy.position, snapshot.obstacle_cells):
                return UnitAction(
                    unit_id=unit.id,
                    type=UnitActionType.SHOOT,
                    target_id=EntityId(enemy.id),
                    expected_cell=enemy.position,
                )
        predictive = self._decide_predictive_shot(snapshot, unit, ordered)
        if predictive is not None:
            return predictive
        contest = self._decide_beacon_contest(snapshot, unit)
        if contest is not None:
            return contest
        return _guard_action(snapshot, unit, enemies, UnitRole.RANGER)

    def _refresh_predictive_fire_state(self, snapshot: PlanningSnapshot) -> None:
        """Settle the previous tick's predictive shots (military S4).

        A predictive shot is scored against the enemy it led: an enemy that is
        gone counts as a kill (record dropped), one that stayed on the same
        cell counts as a miss, and one that moved resets the streak. Reaching
        ``ranger_predictive_miss_cap`` consecutive misses puts the Ranger on
        ``ranger_predictive_cooldown_ticks`` cooldown.
        """

        for unit_id, record in list(self._predictive_fire_records.items()):
            enemy = next(
                (
                    candidate
                    for candidate in snapshot.enemy_units
                    if candidate.id.value == record.enemy_id
                ),
                None,
            )
            if enemy is None:
                del self._predictive_fire_records[unit_id]
                continue
            if enemy.position == record.enemy_position:
                record.misses += 1
                if record.misses >= self._config.ranger_predictive_miss_cap:
                    record.cooldown_until = (
                        snapshot.tick + self._config.ranger_predictive_cooldown_ticks
                    )
            else:
                record.misses = 0

    def _decide_predictive_shot(
        self,
        snapshot: PlanningSnapshot,
        unit: PlanningUnit,
        ordered: list[VisibleEnemy],
    ) -> UnitAction | None:
        """Lead a moving enemy when no direct shot is available (military S4)."""

        if not self._config.ranger_predictive_fire or not ordered:
            return None
        record = self._predictive_fire_records.get(unit.id.value)
        if record is not None and record.cooldown_until > snapshot.tick:
            return None
        if record is not None and record.misses >= self._config.ranger_predictive_miss_cap:
            return None
        enemy = ordered[0]
        predicted = predicted_enemy_cell(unit.position, enemy.position)
        if predicted is None:
            return None
        if not can_shoot(unit.position, predicted, snapshot.obstacle_cells):
            return None
        self._predictive_fire_records[unit.id.value] = _PredictiveFireRecord(
            enemy_id=enemy.id,
            enemy_position=enemy.position,
            misses=record.misses if record is not None else 0,
            cooldown_until=record.cooldown_until if record is not None else 0,
        )
        return UnitAction(
            unit_id=unit.id,
            type=UnitActionType.SHOOT,
            target_id=EntityId(enemy.id),
            expected_cell=predicted,
        )

    def _decide_beacon_contest(
        self, snapshot: PlanningSnapshot, unit: PlanningUnit
    ) -> UnitAction | None:
        """Walk toward and pick up a ground Beacon (evolve beacon_go_range).

        Only outside defense windows (no visible enemies), only from outside
        the Core guard ring (home-guard units never leave), and only when the
        Beacon actually sits on the ground. The carrier then parks at the Core
        via ``_decide_beacon_carrier``.

        S4 gate: a small or resource-tight economy keeps its military coverage
        instead of contesting (``beacon_contest_min_population`` /
        ``beacon_contest_min_resources``). S5 dedup: only one unit contests per
        tick — the first eligible unit claims the trip and every other unit
        keeps guarding/patrolling instead of dog-piling the Beacon.
        """

        beacon = snapshot.beacon
        if beacon.status != "ground" or beacon.carrier_id is not None:
            return None
        if snapshot.enemy_units:
            return None
        if (
            snapshot.population < self._config.beacon_contest_min_population
            or snapshot.resources < self._config.beacon_contest_min_resources
        ):
            return None
        if self._beacon_contest_claimed:
            return None
        core = snapshot.core_position
        if core is not None and manhattan(unit.position, core) <= 1:
            return None
        distance = manhattan(unit.position, beacon.position)
        if distance > self._config.beacon_contest_range:
            return None
        self._beacon_contest_claimed = True
        if unit.position == beacon.position:
            return UnitAction(unit_id=unit.id, type=UnitActionType.PICKUP_BEACON)
        return UnitAction(
            unit_id=unit.id,
            type=UnitActionType.MOVE,
            direction=step_toward(unit.position, beacon.position),
        )


def _visible_enemies(snapshot: PlanningSnapshot) -> tuple[VisibleEnemy, ...]:
    return tuple(
        VisibleEnemy(
            id=enemy.id.value,
            position=enemy.position,
            kind="UNIT",
            unit_role=enemy.unit_role,
        )
        for enemy in snapshot.enemy_units
    )


def _guard_action(
    snapshot: PlanningSnapshot,
    unit: PlanningUnit,
    enemies: tuple[VisibleEnemy, ...],
    role: UnitRole,
) -> UnitAction:
    if snapshot.core_position is None:
        return _wait_action(unit)
    index = _military_index(snapshot, unit)
    post = defense_post(
        snapshot.core_position,
        enemies,
        snapshot.obstacle_cells,
        role,
        index,
    )
    if post is None:
        post = home_cell(snapshot.core_position, snapshot.obstacle_cells, index)
    if post is None or unit.position == post:
        return _wait_action(unit)
    return UnitAction(
        unit_id=unit.id,
        type=UnitActionType.MOVE,
        direction=step_toward(unit.position, post),
    )


def _military_index(snapshot: PlanningSnapshot, unit: PlanningUnit) -> int:
    index = 0
    for candidate in snapshot.units:
        if candidate.id == unit.id:
            return index
        if candidate.unit_role is not UnitRole.WORKER:
            index += 1
    return index


def _wait_action(unit: PlanningUnit) -> UnitAction:
    return UnitAction(unit_id=unit.id, type=UnitActionType.WAIT)
