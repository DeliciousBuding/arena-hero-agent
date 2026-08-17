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

from arena_hero_agent.domain import Coordinate, manhattan

DEFAULT_DETECTION_DISTANCE: Final = 32
DEFAULT_RECOVERY_WORKERS: Final = 16
DEFAULT_BARREN_MIGRATION_TICKS: Final = 100
DEFAULT_BARREN_MIGRATION_COOLDOWN: Final = 200


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
    """

    barren_since_tick: int | None = None
    migration_started_tick: int | None = None

    def observe(
        self,
        *,
        has_resource_cells: bool,
        tick: int,
        core_migrating: bool,
        barren_threshold: int = DEFAULT_BARREN_MIGRATION_TICKS,
        cooldown: int = DEFAULT_BARREN_MIGRATION_COOLDOWN,
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
            self.barren_since_tick = None
            self.migration_started_tick = None
            return False

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
        return True

    def reset(self) -> None:
        self.barren_since_tick = None
        self.migration_started_tick = None


def migration_direction_toward_origin(
    core: Coordinate,
    obstacles: frozenset[str],
) -> str | None:
    """Return the cardinal direction from ``core`` toward [0, 0], or None.

    Chooses the axis with the larger absolute offset (so the Core heads toward
    the origin on its dominant axis first). Returns a plain string ("E", "W",
    "N", "S") for the composition layer to map to its Direction enum.
    """

    dx = -core.x
    dy = -core.y
    if dx == 0 and dy == 0:
        return None
    if abs(dx) >= abs(dy):
        return "E" if dx > 0 else "W"
    return "S" if dy > 0 else "N"


__all__ = [
    "DEFAULT_BARREN_MIGRATION_TICKS",
    "DEFAULT_DETECTION_DISTANCE",
    "DEFAULT_RECOVERY_WORKERS",
    "BarrenMigrationState",
    "RespawnRecoveryState",
    "detect_respawn",
    "migration_direction_toward_origin",
]
