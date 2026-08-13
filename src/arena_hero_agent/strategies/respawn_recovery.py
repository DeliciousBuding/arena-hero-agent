"""Respawn detection and economy-first recovery for the composed decider.

Production lesson (W44 续36): after a Core is destroyed it respawns at a random
far coordinate — often in a resource-depleted area — and the correct response is
to go all-in on Workers until the economy is re-established, instead of stopping
at the default worker target and buying military the fresh Core cannot afford.

The teleport signal is the primary detector: Core migration moves at most one
cell per tick, so a single-tick Manhattan jump of at least
``DEFAULT_DETECTION_DISTANCE`` can only be a destroy-then-respawn teleport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from arena_hero_agent.domain import Coordinate, manhattan

DEFAULT_DETECTION_DISTANCE: Final = 32
DEFAULT_RECOVERY_WORKERS: Final = 16


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


__all__ = [
    "DEFAULT_DETECTION_DISTANCE",
    "DEFAULT_RECOVERY_WORKERS",
    "RespawnRecoveryState",
    "detect_respawn",
]
