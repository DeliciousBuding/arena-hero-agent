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
- falls back to a BFS nearest-unvisited frontier when the directional ring
  and refill anchors all land inside walls (maze): the scout still gets a
  reachable frontier target instead of stalling (port of evolve
  ``_nearest_unvisited``).

All cross-tick knowledge lives in :class:`ExplorationState`, which is advanced
by :func:`observe_exploration` from the current snapshot plus the previous
tick's assignments.  The target builder is otherwise pure.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from arena_hero_agent.domain import Coordinate, UnitRole, manhattan

from .planning_snapshot import PlanningSnapshot, ResourceCellInfo
from .task import TaskType
from .worker_assignment import Assignment

CHUNK_SIZE: Final = 32
NORMAL_RING_STEP: Final = 10
NORMAL_RING_COUNT: Final = 3
HUNGER_RING_STEP: Final = 8
HUNGER_RING_COUNT: Final = 5
HUNGER_TICKS: Final = 200
REFILL_RECHECK_TICKS: Final = 4
EXPLORATION_SURVEY_CAP: Final = 2
SWEEP_RING_STEP: Final = 8
SWEEP_RING_COUNT: Final = 6
HUNGER_SWEEP_RING_COUNT: Final = 8

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

_CARDINAL_DELTAS: Final[tuple[tuple[int, int], ...]] = (
    (1, 0),
    (0, 1),
    (-1, 0),
    (0, -1),
)
_FRONTIER_RADIUS: Final = 64
_FRONTIER_NODE_BUDGET: Final = 16384

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
    # Resource-cell memory: known (unconsumed) natural resource positions plus
    # per-worker cargo from the previous tick so harvests are inferred by the
    # cargo 0 -> >0 transition instead of by assignment type.
    cell_positions: dict[str, Coordinate] = field(default_factory=dict)
    cell_last_seen: dict[str, int] = field(default_factory=dict)
    prev_cargo: dict[str, int] = field(default_factory=dict)
    known_obstacles: set[str] = field(default_factory=set)


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

    # Visible resources mark their chunk as seen and anchor it, and enter the
    # resource-cell memory so other workers can still collect them once they
    # leave the fog-of-war vision range.
    for cell in snapshot.resource_cells.values():
        if cell.visible:
            chunk = chunk_of(cell.position)
            state.chunk_seen_tick[chunk] = snapshot.tick
            state.chunk_anchor[chunk] = cell.position
            state.cell_positions[cell.position.cell_key] = cell.position
            state.cell_last_seen[cell.position.cell_key] = snapshot.tick
    state.known_obstacles |= snapshot.obstacle_cells

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

    # Cargo 0 -> >0 means this worker just harvested the natural resource it is
    # standing on (GO_RESOURCE that converted to HARVEST is not visible as a
    # HARVEST_CURRENT assignment, so infer it from cargo instead). Remove the
    # cell from memory and schedule the chunk refill recheck.
    for unit in snapshot.units:
        if unit.unit_role is not UnitRole.WORKER:
            continue
        key = unit.id.value
        prev_cargo = state.prev_cargo.get(key, 0)
        if prev_cargo == 0 and unit.cargo > 0 and unit.position.cell_key in state.cell_positions:
            state.cell_positions.pop(unit.position.cell_key, None)
            state.cell_last_seen.pop(unit.position.cell_key, None)
            chunk = chunk_of(unit.position)
            state.chunk_last_harvest_tick[chunk] = snapshot.tick
            state.chunk_next_refill_tick[chunk] = refill_tick_at_or_after(snapshot.tick)
            state.chunk_harvest_count[chunk] = state.chunk_harvest_count.get(chunk, 0) + 1
            state.chunk_anchor[chunk] = unit.position
            state.hungry_since = snapshot.tick
        state.prev_cargo[key] = unit.cargo


def with_memory_resource_cells(
    snapshot: PlanningSnapshot,
    state: ExplorationState,
) -> PlanningSnapshot:
    """Return ``snapshot`` with remembered resource cells merged as ``visible=False``.

    Known-but-not-currently-visible resource cells enter the worker assignment
    matrix as historical candidates so every worker can route to a mine that a
    single scout discovered, instead of only the workers whose vision currently
    covers that cell. Cells already visible keep their ``visible=True`` form.
    """

    if not isinstance(snapshot, PlanningSnapshot):
        raise TypeError("snapshot must be a PlanningSnapshot")
    if not isinstance(state, ExplorationState):
        raise TypeError("state must be an ExplorationState")

    merged = dict(snapshot.resource_cells)
    for key, position in state.cell_positions.items():
        if key in merged:
            continue
        merged[key] = ResourceCellInfo(
            position=position,
            visible=False,
            last_seen_tick=state.cell_last_seen.get(key),
        )
    return replace(snapshot, resource_cells=merged)


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


def sweep_radii(hungry: bool) -> tuple[int, ...]:
    """Return the all-direction sweep ring radii (8-step outward)."""

    if not isinstance(hungry, bool):
        raise TypeError("hungry must be a boolean")
    count = HUNGER_SWEEP_RING_COUNT if hungry else SWEEP_RING_COUNT
    return tuple(SWEEP_RING_STEP * ring for ring in range(1, count + 1))


def _ring_band_candidates(core: Coordinate, radii: tuple[int, ...]):
    """Yield one diamond-ring point per direction for each radius (8-way sweep)."""

    for radius in radii:
        for vx, vy in _DELTAS:
            scale = radius // (abs(vx) + abs(vy))
            yield Coordinate(core.x + vx * scale, core.y + vy * scale)


def _bfs_frontier_flood(
    start: Coordinate,
    blocked: frozenset[str],
    visited: Mapping[str, int],
    taken: set[str],
    *,
    search_radius: int = _FRONTIER_RADIUS,
    node_budget: int = _FRONTIER_NODE_BUDGET,
) -> tuple[Coordinate | None, frozenset[str]]:
    """Flood-fill from ``start``; return (nearest frontier, reachable keys).

    Four-way BFS from ``start`` in the oracle's fixed (E, S, W, N) order.
    ``blocked`` cells are impassable; ``visited`` (recorded scout arrivals)
    and ``taken`` (already claimed this tick) cells are traversable but not
    valid targets.  The search is Chebyshev-bounded and node-budget-bounded.

    Returns the nearest reachable, unvisited, unclaimed cell in BFS order plus
    the full set of reachable non-blocked keys.  Callers use the reachable set
    as a path-awareness gate for directional-ring candidates: a ring point that
    sits inside a wall pocket (unreachable) is rejected instead of producing a
    stall, and the nearest frontier becomes the guaranteed fallback.
    """

    if not isinstance(start, Coordinate):
        raise TypeError("start must be a Coordinate")
    if not isinstance(blocked, frozenset):
        raise TypeError("blocked must be a frozenset of cell keys")
    if not isinstance(visited, Mapping):
        raise TypeError("visited must be a Mapping of cell key -> tick")
    if not isinstance(taken, set):
        raise TypeError("taken must be a set of cell keys")
    if isinstance(search_radius, bool) or not isinstance(search_radius, int) or search_radius < 1:
        raise ValueError("search_radius must be a positive integer")
    if isinstance(node_budget, bool) or not isinstance(node_budget, int) or node_budget < 1:
        raise ValueError("node_budget must be a positive integer")

    start_key = start.cell_key
    queue: deque[Coordinate] = deque([start])
    seen: set[str] = {start_key}
    nearest: Coordinate | None = None
    head = 0
    while head < len(queue) and head < node_budget:
        current = queue[head]
        head += 1
        for dx, dy in _CARDINAL_DELTAS:
            neighbor = Coordinate(current.x + dx, current.y + dy)
            if max(abs(neighbor.x - start.x), abs(neighbor.y - start.y)) > search_radius:
                continue
            key = neighbor.cell_key
            if key in seen or key in blocked:
                continue
            seen.add(key)
            if nearest is None and key not in visited and key not in taken:
                nearest = neighbor
            queue.append(neighbor)
    return nearest, frozenset(seen)


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
    blocked = frozenset(obstacles | state.known_obstacles | enemies)
    taken: set[str] = set()
    targets: dict[str, Coordinate] = {}

    # Depleted chunks whose refill boundary has passed (revisit first).
    due_chunks = tuple(
        chunk for chunk in state.chunk_next_refill_tick if _refill_due(chunk, state, tick)
    )

    # Refill-revisit anchors are the highest-priority scout targets, then a
    # deterministic all-direction ring band (8 directions x every radius) so a
    # scout sweeps the whole compass instead of a single fixed direction.
    due_anchors = [state.chunk_anchor.get(chunk) or chunk_center(chunk) for chunk in due_chunks]
    radii = sweep_radii(hungry)

    for unit in workers:
        nearest, reachable = _bfs_frontier_flood(
            unit.position,
            blocked,
            state.point_visited,
            taken,
        )
        best: Coordinate | None = None
        for candidate in (*due_anchors, *_ring_band_candidates(core, radii)):
            cell = candidate.cell_key
            if (
                cell in obstacles
                or cell in state.known_obstacles
                or cell in enemies
                or cell in taken
                or cell in state.point_visited
            ):
                continue
            # Path-awareness gate: only reject a candidate the flood could
            # actually reach.  Targets beyond the flood radius (e.g. a distant
            # refill anchor) are not gated here; the route-aware merge handles
            # their pathing and a far anchor is trustworthy history.
            within_flood = (
                max(abs(candidate.x - unit.position.x), abs(candidate.y - unit.position.y))
                <= _FRONTIER_RADIUS
            )
            if within_flood and cell not in reachable:
                continue
            best = candidate
            break
        if best is None:
            # Every refill anchor and directional-ring candidate is blocked,
            # visited, already claimed, or unreachable from this worker.  Use
            # the BFS nearest reachable frontier so a scout always gets a
            # path-aware target instead of stalling against a wall pocket.
            best = nearest
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
    "sweep_radii",
    "with_memory_resource_cells",
]
