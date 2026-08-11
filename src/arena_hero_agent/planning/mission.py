"""Mission value layer: collection target scoring and surveyor budgeting.

This is the deterministic value/confidence half of the legacy ``mission-planner``:
target confidence (visible bonus plus seeded age decay), collection-pool filters,
refill predictions, and surveyor id selection. Worker assignment itself (the cost
matrix and sticky routing) belongs to P4-12; this module only exposes the pure
functions that assignment consumes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from arena_hero_agent.domain import Coordinate, manhattan

from .planning_snapshot import PlanningUnit


@dataclass(frozen=True, slots=True)
class MissionConfig:
    """Immutable mission-layer thresholds; defaults reproduce the oracle."""

    __canonical_name__ = "arena-hero.mission-config.v1"

    collection_value_floor: float = float("-inf")
    max_collection_distance: float = float("inf")
    survey_worker_cap: int = 0
    survey_burst_ticks: int = 0
    survey_worker_floor: int = 0
    visible_bonus: float = 0.0
    seed_age_decay: float = 0.0
    refill_lookahead: int = 0
    refill_bonus: float = 0.0
    dead_mine_overdue_ticks: int = 0
    migration_scout: bool = False
    always_survey: bool = False
    switch_threshold: float = 0.0
    survey_on_supply_gap: bool = False


DEFAULT_MISSION_CONFIG: Final = MissionConfig()


def target_confidence(
    cell: Mapping[str, object],
    tick: int,
    config: MissionConfig = DEFAULT_MISSION_CONFIG,
) -> float:
    """Score a target cell's confidence: visible bonus minus seeded age decay."""

    if not isinstance(cell, Mapping):
        raise TypeError("cell must be a Mapping")
    if not isinstance(config, MissionConfig):
        raise TypeError("config must be a MissionConfig")
    confidence = config.visible_bonus if cell.get("visible") is True else 0.0
    if cell.get("seeded") is True:
        last_seen = cell.get("last_seen_tick")
        if isinstance(last_seen, int) and not isinstance(last_seen, bool):
            age = max(0, tick - last_seen)
            confidence -= config.seed_age_decay * age
    return confidence


def is_collectable(
    score: float,
    worker: PlanningUnit,
    cell_x: int,
    cell_y: int,
    config: MissionConfig = DEFAULT_MISSION_CONFIG,
    refill_predictions: Mapping[str, int] | None = None,
    *,
    visible: bool = False,
) -> bool:
    """Return whether a scored target enters the collection pool.

    Currently visible cells are real-time facts and only obey hard constraints
    (distance); historical/memory cells must also pass the value floor and the
    dead-mine prediction gate.
    """

    if not isinstance(worker, PlanningUnit):
        raise TypeError("worker must be a PlanningUnit")
    if not isinstance(config, MissionConfig):
        raise TypeError("config must be a MissionConfig")
    if not visible and score < config.collection_value_floor:
        return False
    key = f"{cell_x},{cell_y}"
    due_in_ticks = None if refill_predictions is None else refill_predictions.get(key)
    if not visible and due_in_ticks is not None and due_in_ticks < -config.dead_mine_overdue_ticks:
        return False
    distance = manhattan(worker.position, Coordinate(cell_x, cell_y))
    return distance <= config.max_collection_distance


def refill_bonus_of(
    key: str,
    refill_predictions: Mapping[str, int] | None,
    config: MissionConfig = DEFAULT_MISSION_CONFIG,
) -> float:
    """Return the bonus for a mine predicted to refill within the lookahead."""

    if not isinstance(config, MissionConfig):
        raise TypeError("config must be a MissionConfig")
    if refill_predictions is None:
        return 0.0
    due_in_ticks = refill_predictions.get(key)
    if due_in_ticks is None:
        return 0.0
    if due_in_ticks > config.refill_lookahead:
        return 0.0
    if due_in_ticks < -config.dead_mine_overdue_ticks:
        return 0.0
    return config.refill_bonus


def surveyor_ids(
    unassigned: tuple[PlanningUnit, ...],
    config: MissionConfig = DEFAULT_MISSION_CONFIG,
    *,
    survey_burst_active: bool = False,
) -> frozenset[str]:
    """Select deterministic surveyor ids from the first cap sorted by id."""

    if not isinstance(config, MissionConfig):
        raise TypeError("config must be a MissionConfig")
    ordered = tuple(sorted(unassigned, key=lambda unit: unit.id.value))
    required = (
        max(config.survey_worker_floor, config.survey_worker_cap)
        if survey_burst_active
        else config.survey_worker_cap
    )
    if required <= 0:
        return frozenset()
    return frozenset(unit.id.value for unit in ordered[:required])
