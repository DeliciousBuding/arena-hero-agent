"""Respawn detection and economy-first recovery for the composed decider.

Production lesson (W44 续36): after a Core is destroyed it respawns at a random
far coordinate — often in a resource-depleted area — and the correct response is
to go all-in on Workers until the economy is re-established, instead of stopping
at the default worker target and buying military the fresh Core cannot afford.

The teleport signal is the primary detector: Core migration moves at most one
cell per tick, so a single-tick Manhattan jump of at least
``DEFAULT_DETECTION_DISTANCE`` can only be a destroy-then-respawn teleport.

Official rules alignment (2026-08-18, `destruction-and-respawn.md` +
`map-and-vision.md`): respawn placement only considers terrain passability and
20-30 Manhattan distance from the nearest living Core — it does NOT consider
resource proximity. Resource density is per-chunk (32x32) and falls with ring
distance from origin: ``quota = max(2, floor(128 / (8 + ring)))``. A respawn at
ring 46 (e.g. [-663, -784]) has only 2 resource points per 1024-cell chunk —
workers may explore for hundreds of ticks without finding any. The
``BarrenMigrationState`` tracks this condition and, after a configurable tick
threshold with zero visible resource cells, signals the Core to migrate toward
the origin where resource density is higher (ring 0 = 16 per chunk).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from arena_hero_agent.domain import Coordinate, cell_key, manhattan

DEFAULT_DETECTION_DISTANCE: Final = 32
DEFAULT_RECOVERY_WORKERS: Final = 16
DEFAULT_BARREN_MIGRATION_TICKS: Final = 30
DEFAULT_BARREN_MIGRATION_COOLDOWN: Final = 30
DEFAULT_STUCK_RESOURCES_TICKS: Final = 20
DEFAULT_BARREN_RESET_LIMIT: Final = 3


def detect_respawn(
    previous_core: Coordinate | None,
    current_core: Coordinate,
    *,
    detection_distance: int = DEFAULT_DETECTION_DISTANCE,
) -> bool:
    """Return whether the core teleported far enough to count as a respawn.

    ``previous_core is None`` means there is no prior observation (first tick or
    a fresh process), which is not evidence of a respawn and returns ``False``.
    """

    if previous_core is None:
        return False
    if detection_distance < 1:
        raise ValueError("detection_distance must be at least 1")
    return manhattan(previous_core, current_core) >= detection_distance


@dataclass(slots=True)
class RespawnRecoveryState:
    """Cross-tick recovery latch. ``active`` means force Worker production."""

    active: bool = False
    detected_tick: int | None = None

    def note_respawn(self, tick: int) -> None:
        if not self.active:
            self.active = True
            self.detected_tick = tick

    def note_recovered(self) -> None:
        self.active = False
        self.detected_tick = None


@dataclass(slots=True)
class BarrenMigrationState:
    """Track resource-barren condition to trigger Core migration toward origin.

    After respawn the Core may land in a resource-sparse area (high ring). If
    ``resource_cells`` stays empty for ``barren_threshold`` consecutive ticks,
    the hook signals the Core to START_MOVE toward [0, 0] — where resource
    density is highest. The migration is slow (4 ticks per cell) but automatic;
    workers continue exploring during migration. A cooldown prevents jitter:
    after each migration attempt, ``cooldown`` ticks must pass before the next.

    Phantom-resource protection: if the agent briefly sees resource cells but
    can't reach them (population doesn't grow), the barren counter would
    oscillate indefinitely. After ``max_resets`` such resets, the resource
    cells are treated as phantom and the counter stops resetting — allowing
    migration to proceed.
    """

    barren_since_tick: int | None = None
    migration_started_tick: int | None = None
    reset_count: int = 0

    def observe(
        self,
        *,
        has_resource_cells: bool,
        tick: int,
        core_migrating: bool,
        barren_threshold: int = DEFAULT_BARREN_MIGRATION_TICKS,
        cooldown: int = DEFAULT_BARREN_MIGRATION_COOLDOWN,
        max_resets: int = DEFAULT_BARREN_RESET_LIMIT,
    ) -> bool:
        """Track barren condition; return True when migration should start.

        Call once per tick before the core_action decision. Returns ``True``
        only on the tick the migration should be triggered. After firing, the
        cooldown prevents re-triggering until ``cooldown`` ticks have passed.
        """

        if core_migrating:
            self.barren_since_tick = None
            return False

        if has_resource_cells:
            self.reset_count += 1
            if self.reset_count <= max_resets:
                self.barren_since_tick = None
                self.migration_started_tick = None
                return False
            # Phantom resources: briefly visible but unreachable.
            # Don't reset barren_since_tick — let the counter continue.

        if self.barren_since_tick is None:
            self.barren_since_tick = tick
            return False

        elapsed = tick - self.barren_since_tick
        if elapsed < barren_threshold:
            return False

        if (
            self.migration_started_tick is not None
            and tick - self.migration_started_tick < cooldown
        ):
            return False

        self.migration_started_tick = tick
        self.barren_since_tick = tick
        self.reset_count = 0
        return True

    def reset(self) -> None:
        self.barren_since_tick = None
        self.migration_started_tick = None
        self.reset_count = 0


@dataclass(slots=True)
class StuckWithResourcesState:
    """Track terrain-trap deadlock: Core has resources but population won't grow.

    When the Core has resources (> 0) but the population hasn't increased for
    ``threshold`` consecutive ticks, the most likely cause is a terrain trap:
    the worker is stuck on the Core's cell (MOVE_BLOCKED_TERRAIN) and the Core
    can't spawn (CELL_UNIT_LIMIT).  Issuing SELF_DESTRUCT breaks this deadlock
    by respawning the Core at a new terrain-passable location.
    """

    last_population: int | None = None
    stuck_since_tick: int | None = None

    def observe(
        self,
        *,
        resources: int,
        population: int,
        tick: int,
        threshold: int = DEFAULT_STUCK_RESOURCES_TICKS,
    ) -> bool:
        """Return True when SELF_DESTRUCT should fire to escape the trap."""

        if resources <= 0:
            self.last_population = population
            self.stuck_since_tick = None
            return False

        if self.last_population is None or population > self.last_population:
            self.last_population = population
            self.stuck_since_tick = tick
            return False

        if population < self.last_population:
            self.last_population = population
            self.stuck_since_tick = tick
            return False

        if self.stuck_since_tick is None:
            self.stuck_since_tick = tick
            return False

        return tick - self.stuck_since_tick >= threshold

    def reset(self) -> None:
        self.last_population = None
        self.stuck_since_tick = None


def migration_direction_toward_origin(
    core: Coordinate,
    obstacles: frozenset[str],
) -> str | None:
    """Return the cardinal direction from ``core`` toward [0, 0], or None.

    Chooses the axis with the larger absolute offset (so the Core heads toward
    the origin on its dominant axis first). If the adjacent cell in that
    direction is blocked by terrain, falls back to the secondary axis. If both
    are blocked, returns None — the terrain trap hook should handle it.
    """

    dx = -core.x
    dy = -core.y
    if dx == 0 and dy == 0:
        return None

    if abs(dx) >= abs(dy):
        primary = "E" if dx > 0 else "W"
        secondary = "S" if dy > 0 else "N" if dy < 0 else None
    else:
        primary = "S" if dy > 0 else "N"
        secondary = "E" if dx > 0 else "W" if dx < 0 else None

    for direction in (primary, secondary):
        if direction is None:
            continue
        adjacent = _adjacent_coordinate(core, direction)
        if cell_key(adjacent) not in obstacles:
            return direction

    return None


def _adjacent_coordinate(core: Coordinate, direction: str) -> Coordinate:
    """Return the Coordinate one step in the given cardinal direction."""

    if direction == "E":
        return Coordinate(core.x + 1, core.y)
    if direction == "W":
        return Coordinate(core.x - 1, core.y)
    if direction == "S":
        return Coordinate(core.x, core.y + 1)
    return Coordinate(core.x, core.y - 1)


__all__ = [
    "DEFAULT_BARREN_MIGRATION_TICKS",
    "DEFAULT_BARREN_RESET_LIMIT",
    "DEFAULT_DETECTION_DISTANCE",
    "DEFAULT_RECOVERY_WORKERS",
    "DEFAULT_STUCK_RESOURCES_TICKS",
    "BarrenMigrationState",
    "RespawnRecoveryState",
    "StuckWithResourcesState",
    "detect_respawn",
    "migration_direction_toward_origin",
]
