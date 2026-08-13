"""Event-driven ring-quota exploration (research, default off).

Deterministic port of the third-party collection/exploration core:

- ``arena-evolve/strategies/heuristic.py`` ring patrol (8 direction slots x 2
  ring groups, spiral outward as points are visited) and
- ``arena-hero-clone-waaiging/arena_hero_strategy.py`` chunk ledger (harvest
  accounting, 4-tick refill boundary, per-chunk probe recency).

The default worker-assignment path is byte-for-byte unchanged; this module only
supplies *where* an idle worker explores once the ``exploration_v2`` research
switch is enabled.  Unlike the legacy fixed 24-radius single-explorer path, the
enabled path:

- derives one of 16 stable direction slots per worker (FNV-1a over unit id);
- walks ring radii 10/20/30 (slot 0-7) or 20/30/40 (slot 8-15), or
  8/16/24/32/40 in hunger mode (strictly more than ``HUNGER_TICKS`` ticks with
  no confirmed harvest);
- revisits depleted chunks whose 4-tick refill boundary has passed, by their
  recorded anchor, before falling back to the directional ring;
- ranks candidates by: due refill recheck -> most recently harvested chunk ->
  never-seen chunk -> least recently visited point -> richer chunk quota ->
  distance to Core -> coordinates.

All cross-tick knowledge lives in :class:`ExplorationState`, which is advanced
by :func:`observe_exploration` from the current snapshot plus the previous
tick's assignments.  The target builder is otherwise pure.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from arena_hero_agent.domain import Coordinate, UnitRole, manhattan

from .planning_snapshot import PlanningSnapshot
from .task import TaskType
from .worker_assignment import Assignment

CHUNK_SIZE: Final = 32
NORMAL_RING_STEP: Final = 10
NORMAL_RING_COUNT: Final = 3
HUNGER_RING_STEP: Final = 8
HUNGER_RING_COUNT: Final = 5
HUNGER_TICKS: Final = 200
REFILL_RECHECK_TICKS: Final = 4
EXPLORATION_SURVEY_CAP: Final = 1

_DELTAS: Final[tuple[tuple[int, int], ...]] = (
    (1, 0),
    (1, 1),
    (0, 1),
    (-1, 1),
    (-1, 0),
    (-1, -1),
    (0, -1),
    (1, -1),
)

_FNV_OFFSET_BASIS: Final = 0x811C9DC5
_FNV_PRIME: Final = 0x01000193

_NEVER: Final = 10**9


@dataclass(slots=True)
class ExplorationState:
    """Cross-tick exploration ledger (mutable, owned by the decider)."""

    chunk_seen_tick: dict[tuple[int, int], int] = field(default_factory=dict)
    chunk_last_harvest_tick: dict[tuple[int, int], int] = field(default_factory=dict)
    chunk_next_refill_tick: dict[tuple[int, int], int] = field(default_factory=dict)
    chunk_last_probe_tick: dict[tuple[int, int], int] = field(default_factory=dict)
    chunk_harvest_count: dict[tuple[int, int], int] = field(default_factory=dict)
    chunk_anchor: dict[tuple[int, int], Coordinate] = field(default_factory=dict)
    point_visited: dict[str, int] = field(default_factory=dict)
    hungry_since: int | None = None


def explorer_slot(unit_id: str) -> int:
    """Return a stable 0-15 direction/ring slot for one worker id (FNV-1a)."""

    if not isinstance(unit_id, str) or not unit_id:
        raise TypeError("unit_id must be a non-empty string")
    digest = _FNV_OFFSET_BASIS
    for byte in unit_id.encode("utf-8"):
        digest = ((digest ^ byte) * _FNV_PRIME) & 0xFFFFFFFF
    return digest % 16


def chunk_of(position: Coordinate) -> tuple[int, int]:
    """Return the 32x32 chunk coordinates containing ``position``."""

    if not isinstance(position, Coordinate):
        raise TypeError("position must be a Coordinate")
    return position.x // CHUNK_SIZE, position.y // CHUNK_SIZE


def chunk_center(chunk: tuple[int, int]) -> Coordinate:
    """Return the center cell of a chunk (fallback anchor)."""

    if not isinstance(chunk, tuple) or len(chunk) != 2:
        raise TypeError("chunk must be a (cx, cy) tuple")
    cx, cy = chunk
    if not isinstance(cx, int) or not isinstance(cy, int):
        raise TypeError("chunk coordinates must be integers")
    return Coordinate(cx * CHUNK_SIZE + CHUNK_SIZE // 2, cy * CHUNK_SIZE + CHUNK_SIZE // 2)


def _axis(value: int) -> int:
    """Map a signed chunk axis to its non-negative ring distance."""

    return value if value >= 0 else -value - 1


def chunk_ring(chunk: tuple[int, int]) -> int:
    """Return the official ring index of a chunk (axis distance from center)."""

    if not isinstance(chunk, tuple) or len(chunk) != 2:
        raise TypeError("chunk must be a (cx, cy) tuple")
    cx, cy = chunk
    if isinstance(cx, bool) or not isinstance(cx, int):
        raise TypeError("chunk x must be an integer")
    if isinstance(cy, bool) or not isinstance(cy, int):
        raise TypeError("chunk y must be an integer")
    return _axis(cx) + _axis(cy)


def chunk_quota(chunk: tuple[int, int]) -> int:
    """Return the official resource quota ``max(2, 128 // (8 + ring))``."""

    return max(2, 128 // (8 + chunk_ring(chunk)))


def refill_tick_at_or_after(tick: int) -> int:
    """Return the first world refill boundary at or after ``tick``."""

    if isinstance(tick, bool) or not isinstance(tick, int):
        raise TypeError("tick must be an integer")
    if tick < 0:
        raise ValueError("tick must be non-negative")
    return tick + ((REFILL_RECHECK_TICKS - tick % REFILL_RECHECK_TICKS) % REFILL_RECHECK_TICKS)


def ring_radii(slot: int, hungry: bool) -> tuple[int, ...]:
    """Return the candidate ring radii along one worker's direction slot.

    slot 0-7 -> 10/20/30 (normal) or 8/16/24/32/40 (hunger);
    slot 8-15 -> 20/30/40 (normal) or 16/24/32/40/48 (hunger).
    """

    if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < 16:
        raise ValueError("slot must be an integer in [0, 16)")
    if not isinstance(hungry, bool):
        raise TypeError("hungry must be a boolean")
    step = HUNGER_RING_STEP if hungry else NORMAL_RING_STEP
    count = HUNGER_RING_COUNT if hungry else NORMAL_RING_COUNT
    start_ring = 1 + slot // 8
    return tuple(step * ring for ring in range(start_ring, start_ring + count))


def observe_exploration(
    snapshot: PlanningSnapshot,
    previous_assignments: Sequence[Assignment],
    state: ExplorationState,
) -> None:
    """Advance ``state`` from one tick's snapshot and the prior assignments."""

    if not isinstance(snapshot, PlanningSnapshot):
        raise TypeError("snapshot must be a PlanningSnapshot")
    if not isinstance(state, ExplorationState):
        raise TypeError("state must be an ExplorationState")

    # Visible resources mark their chunk as seen and anchor it.
    for cell in snapshot.resource_cells.values():
        if cell.visible:
            chunk = chunk_of(cell.position)
            state.chunk_seen_tick[chunk] = snapshot.tick
            state.chunk_anchor[chunk] = cell.position

    # A worker that was told to harvest and now carries cargo confirms a
    # successful harvest: record it and reschedule the chunk's refill recheck.
    workers = {unit.id.value: unit for unit in snapshot.units if unit.unit_role is UnitRole.WORKER}
    for assignment in previous_assignments:
        if assignment.task.type is not TaskType.HARVEST_CURRENT:
            continue
        unit = workers.get(assignment.unit_id)
        if unit is None or unit.cargo <= 0:
            continue
        cell = assignment.task.target if assignment.task.target is not None else unit.position
        chunk = chunk_of(cell)
        state.chunk_last_harvest_tick[chunk] = snapshot.tick
        state.chunk_next_refill_tick[chunk] = refill_tick_at_or_after(snapshot.tick)
        state.chunk_harvest_count[chunk] = state.chunk_harvest_count.get(chunk, 0) + 1
        state.chunk_anchor[chunk] = cell
        state.hungry_since = snapshot.tick


def is_hungry(state: ExplorationState, tick: int) -> bool:
    """True once ``tick`` exceeds the last confirmed harvest by more than HUNGER_TICKS."""

    if not isinstance(state, ExplorationState):
        raise TypeError("state must be an ExplorationState")
    since = state.hungry_since
    return since is not None and tick - since > HUNGER_TICKS


def _refill_due(chunk: tuple[int, int], state: ExplorationState, tick: int) -> bool:
    refill = state.chunk_next_refill_tick.get(chunk)
    if refill is None or refill > tick:
        return False
    last_probe = state.chunk_last_probe_tick.get(chunk)
    if last_probe is None:
        return True
    return tick - last_probe >= REFILL_RECHECK_TICKS


def build_exploration_targets(
    snapshot: PlanningSnapshot,
    state: ExplorationState,
    *,
    hungry: bool = False,
) -> dict[str, Coordinate]:
    """Return one ring-quota explore target per controlled worker (pure read)."""

    if not isinstance(snapshot, PlanningSnapshot):
        raise TypeError("snapshot must be a PlanningSnapshot")
    if not isinstance(state, ExplorationState):
        raise TypeError("state must be an ExplorationState")
    if not isinstance(hungry, bool):
        raise TypeError("hungry must be a boolean")

    core = snapshot.core_position
    if core is None:
        return {}
    workers = sorted(
        (unit for unit in snapshot.units if unit.unit_role is UnitRole.WORKER),
        key=lambda unit: unit.id.value,
    )
    if not workers:
        return {}

    tick = snapshot.tick
    obstacles = snapshot.obstacle_cells
    enemies = snapshot.enemy_cells
    taken: set[str] = set()
    targets: dict[str, Coordinate] = {}

    # Depleted chunks whose refill boundary has passed (revisit first).
    due_chunks = tuple(
        chunk for chunk in state.chunk_next_refill_tick if _refill_due(chunk, state, tick)
    )

    for unit in workers:
        slot = explorer_slot(unit.id.value)
        vx, vy = _DELTAS[slot % 8]
        candidates: list[Coordinate] = []
        for chunk in due_chunks:
            anchor = state.chunk_anchor.get(chunk) or chunk_center(chunk)
            candidates.append(anchor)
        for radius in ring_radii(slot, hungry):
            scale = radius // (abs(vx) + abs(vy))
            candidates.append(Coordinate(core.x + vx * scale, core.y + vy * scale))

        best: Coordinate | None = None
        best_key: tuple[int, ...] | None = None
        for candidate in candidates:
            cell = candidate.cell_key
            if cell in obstacles or cell in enemies or cell in taken:
                continue
            chunk = chunk_of(candidate)
            harvest = state.chunk_last_harvest_tick.get(chunk)
            recency = (tick - harvest) if harvest is not None else _NEVER
            seen = state.chunk_seen_tick.get(chunk, 0)
            visited = state.point_visited.get(cell, 0)
            key = (
                0 if _refill_due(chunk, state, tick) else 1,
                recency,
                seen,
                visited,
                -chunk_quota(chunk),
                manhattan(candidate, core),
                candidate.x,
                candidate.y,
            )
            if best_key is None or key < best_key:
                best_key = key
                best = candidate
        if best is not None:
            targets[unit.id.value] = best
            taken.add(best.cell_key)

    return targets


def mark_reached(
    snapshot: PlanningSnapshot,
    assignments: Sequence[Assignment],
    state: ExplorationState,
) -> None:
    """Record explore arrivals for workers actually assigned EXPLORE."""

    workers = {unit.id.value: unit for unit in snapshot.units if unit.unit_role is UnitRole.WORKER}
    for assignment in assignments:
        if assignment.task.type is not TaskType.EXPLORE or assignment.task.target is None:
            continue
        unit = workers.get(assignment.unit_id)
        if unit is None or manhattan(unit.position, assignment.task.target) > 1:
            continue
        state.point_visited[assignment.task.target.cell_key] = snapshot.tick
        state.chunk_last_probe_tick[chunk_of(assignment.task.target)] = snapshot.tick


__all__ = [
    "CHUNK_SIZE",
    "EXPLORATION_SURVEY_CAP",
    "HUNGER_TICKS",
    "NORMAL_RING_COUNT",
    "NORMAL_RING_STEP",
    "REFILL_RECHECK_TICKS",
    "ExplorationState",
    "build_exploration_targets",
    "chunk_center",
    "chunk_of",
    "chunk_quota",
    "chunk_ring",
    "explorer_slot",
    "is_hungry",
    "mark_reached",
    "observe_exploration",
    "refill_tick_at_or_after",
    "ring_radii",
]
