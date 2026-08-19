"""Safety planner configuration: threat tiers, profiles, and immutable config.

The default config mirrors the legacy ``DEFAULT_SAFETY_CONFIG``. Optional
experimental switches from the TypeScript oracle are intentionally not migrated;
they remain registered in the behavior-difference registry as EXPECTED_UNKNOWN.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class AggressionLevel(StrEnum):
    """Battle aggression posture for the deterministic safety planner."""

    __canonical_name__ = "arena-hero.aggression-level.v1"

    DEFENSIVE = "defensive"
    AGGRESSIVE = "aggressive"


class ThreatTier(StrEnum):
    """Leaderboard damage-rank threat classification."""

    __canonical_name__ = "arena-hero.threat-tier.v1"

    STANDARD = "STANDARD"
    AGGRESSOR = "AGGRESSOR"
    ELITE_AGGRESSOR = "ELITE_AGGRESSOR"


@dataclass(frozen=True, slots=True)
class ThreatProfile:
    """One player's leaderboard-derived threat profile."""

    __canonical_name__ = "arena-hero.threat-profile.v1"

    username: str
    damage_score: int
    damage_rank: int
    core_score: int
    core_rank: int
    tier: ThreatTier

    def __post_init__(self) -> None:
        if not isinstance(self.username, str) or not self.username:
            raise ValueError("username must be a non-empty string")
        for name, value in (
            ("damage_score", self.damage_score),
            ("damage_rank", self.damage_rank),
            ("core_score", self.core_score),
            ("core_rank", self.core_rank),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if not isinstance(self.tier, ThreatTier):
            raise TypeError("tier must be a ThreatTier")


def tier_of_damage_rank(rank: int) -> ThreatTier:
    """Classify a damage-output rank: 1-10 elite, 11-30 aggressor, else standard."""

    if isinstance(rank, bool) or not isinstance(rank, int):
        raise TypeError("rank must be an integer")
    if 1 <= rank <= 10:
        return ThreatTier.ELITE_AGGRESSOR
    if rank <= 30:
        return ThreatTier.AGGRESSOR
    return ThreatTier.STANDARD


@dataclass(frozen=True, slots=True)
class SafetyPlannerConfig:
    """Immutable safety planner thresholds; defaults reproduce the oracle."""

    __canonical_name__ = "arena-hero.safety-planner-config.v1"

    reserve_wealthy: int = 3
    reserve_early: int = 1
    wealthy_threshold: int = 10
    # Economy scaling: evolve's champion gene keeps workers at ~55% of a
    # 30-pop fleet (~16) and massarmy's farmer stage runs 12 workers; the
    # previous 8-starved small fleets (production tenants sat at pop 1-2 with
    # worker_target 8, so every harvested resource went to military builds the
    # economy could not sustain).
    worker_target: int = 12
    population_ceiling: int = 20
    explore_radius: int = 8
    threat_enemy_distance: int = 5
    accumulate_target: int = 0
    guard_resources: int = 30
    guard_force: int = 4
    max_focus_distance: int = 32
    # Active Beacon contest: a non-guard military unit within this many tiles
    # of a ground Beacon goes to pick it up when no enemy is visible (the
    # champion gene value is ~15). The carrier then parks next to the Core,
    # gaining the shield cap 10 and double harvest for every worker.
    beacon_contest_range: int = 15
    # A unit carrying our Beacon parks within this radius of the Core.
    beacon_carrier_hold_radius: int = 1
    aggression: AggressionLevel = AggressionLevel.DEFENSIVE
    vanguard_ratio: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("reserve_wealthy", self.reserve_wealthy),
            ("reserve_early", self.reserve_early),
            ("wealthy_threshold", self.wealthy_threshold),
            ("worker_target", self.worker_target),
            ("population_ceiling", self.population_ceiling),
            ("explore_radius", self.explore_radius),
            ("threat_enemy_distance", self.threat_enemy_distance),
            ("accumulate_target", self.accumulate_target),
            ("guard_resources", self.guard_resources),
            ("guard_force", self.guard_force),
            ("max_focus_distance", self.max_focus_distance),
            ("beacon_contest_range", self.beacon_contest_range),
            ("beacon_carrier_hold_radius", self.beacon_carrier_hold_radius),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if not isinstance(self.aggression, AggressionLevel):
            raise TypeError("aggression must be an AggressionLevel")
        if self.vanguard_ratio is not None:
            if isinstance(self.vanguard_ratio, bool) or not isinstance(
                self.vanguard_ratio, (int, float)
            ):
                raise TypeError("vanguard_ratio must be a number or None")
            if not 0.0 <= self.vanguard_ratio <= 1.0:
                raise ValueError("vanguard_ratio must be within [0, 1]")


DEFAULT_SAFETY_CONFIG: Final = SafetyPlannerConfig()
AGGRESSIVE_SAFETY_CONFIG: Final = SafetyPlannerConfig(aggression=AggressionLevel.AGGRESSIVE)
