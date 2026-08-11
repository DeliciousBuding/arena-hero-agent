"""Deterministic alliance threat field and tenant-relative threat summaries.

P4-17 implements the TS lib/alliance threat-field.ts and threat-summary.ts
semantics: project enemy combat/Core sightings into sparse threat cells, then
derive eight-sector directional summaries relative to each member core. Pure
functions only; no I/O.

Stale-data guard (recorded in PROGRESS.md): by default HISTORICAL sightings
(older than the freshness window) are excluded from the field and summaries
entirely, so stale threat data can never amplify a threat level. TS parity is
kept for fresh data; ``include_historical=True`` reproduces the exact TS
threat-field projection including decayed historical contributions.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from arena_hero_agent.domain import Coordinate, TenantId

from .snapshot import (
    AllianceSnapshot,
    EntitySighting,
    FreshnessWindow,
    IntelFreshness,
    SightingKind,
    classify_sighting_freshness,
    current_confidence,
    estimated_force,
    is_combat_unit,
)

# --- TS threat-field.ts constants ---

THREAT_FIELD_RADIUS = 12
CORE_RAID_RADIUS = 24


class ThreatDirection(StrEnum):
    """Stable eight-direction sectors (TS ``ThreatDirection``)."""

    __canonical_name__ = "arena-hero.threat-direction.v1"

    N = "N"
    NE = "NE"
    E = "E"
    SE = "SE"
    S = "S"
    SW = "SW"
    W = "W"
    NW = "NW"


DIRECTIONS: tuple[ThreatDirection, ...] = (
    ThreatDirection.N,
    ThreatDirection.NE,
    ThreatDirection.E,
    ThreatDirection.SE,
    ThreatDirection.S,
    ThreatDirection.SW,
    ThreatDirection.W,
    ThreatDirection.NW,
)


def proximity_weight(distance: int) -> float:
    """Projection weight decay: 1 / (1 + d); within the cell it is 1 (TS)."""

    _require_int("distance", distance)
    return 1.0 / (1 + distance)


@dataclass(frozen=True, slots=True)
class ThreatCell:
    """Sparse threat cell (TS ``ThreatCell``)."""

    __canonical_name__ = "arena-hero.threat-cell.v1"

    position: Coordinate
    direct_combat: float
    projected_combat: float
    core_raid: float
    uncertainty: float

    def __post_init__(self) -> None:
        if not isinstance(self.position, Coordinate):
            raise TypeError("threat cell position must be a Coordinate")
        for name in ("direct_combat", "projected_combat", "core_raid", "uncertainty"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"threat cell {name} must be a number")
            if not math.isfinite(float(value)):
                raise ValueError(f"threat cell {name} must be finite")
            object.__setattr__(self, name, float(value))


@dataclass(frozen=True, slots=True)
class ThreatField:
    """Sparse threat field (TS ``ThreatField``) with deterministic cell order."""

    __canonical_name__ = "arena-hero.threat-field.v1"

    cells: Mapping[str, ThreatCell]
    max_direct: ThreatCell | None
    estimated_combat_force: float
    tick_window: tuple[int, int]
    generated_at_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.cells, Mapping):
            raise TypeError("threat field cells must be a mapping of key -> ThreatCell")
        object.__setattr__(self, "cells", MappingProxyType(dict(self.cells)))
        if self.max_direct is not None and not isinstance(self.max_direct, ThreatCell):
            raise TypeError("threat field max_direct must be a ThreatCell or None")
        if isinstance(self.estimated_combat_force, bool) or not isinstance(
            self.estimated_combat_force, (int, float)
        ):
            raise TypeError("threat field estimated_combat_force must be a number")
        if not math.isfinite(float(self.estimated_combat_force)):
            raise ValueError("threat field estimated_combat_force must be finite")
        object.__setattr__(self, "estimated_combat_force", float(self.estimated_combat_force))
        if (
            not isinstance(self.tick_window, tuple)
            or len(self.tick_window) != 2
            or any(isinstance(t, bool) or not isinstance(t, int) for t in self.tick_window)
        ):
            raise TypeError("threat field tick_window must be a (start, end) integer pair")
        if self.tick_window[0] < 0 or self.tick_window[1] < self.tick_window[0]:
            raise ValueError("threat field tick_window must be non-negative and end after start")
        _require_int("threat field generated_at_ms", self.generated_at_ms, minimum=0)


def _require_int(name: str, value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _round_ts(value: float) -> float:
    """Match JS Math.round(x*1e6)/1e6: half away from zero, 6 decimals."""

    if not math.isfinite(value):
        return 0.0
    return math.floor(value * 1_000_000 + 0.5) / 1_000_000


def _cell_key(position: Coordinate) -> str:
    return f"{position.x},{position.y}"


@dataclass
class _SectorAccumulator:
    # Mutable per-sector accumulation during a tenant threat summary.
    score: float = 0.0
    distances: list[int] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)


def _project_around(
    cells: dict[str, ThreatCell],
    source: Coordinate,
    radius: int,
    weight: float,
    uncertainty: float,
    field_name: str,
) -> None:
    """Contribute one sighting to surrounding cells (TS projectAround)."""

    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            distance = abs(dx) + abs(dy)
            if distance > radius:
                continue
            position = Coordinate(source.x + dx, source.y + dy)
            key = _cell_key(position)
            entry = cells.get(key)
            add = weight * proximity_weight(distance)
            direct = add if field_name == "direct_combat" else 0.0
            projected = add if field_name == "projected_combat" else 0.0
            core_raid = add if field_name == "core_raid" else 0.0
            if entry is None:
                cells[key] = ThreatCell(
                    position=position,
                    direct_combat=direct,
                    projected_combat=projected,
                    core_raid=core_raid,
                    uncertainty=uncertainty,
                )
            else:
                cells[key] = ThreatCell(
                    position=entry.position,
                    direct_combat=entry.direct_combat + direct,
                    projected_combat=entry.projected_combat + projected,
                    core_raid=entry.core_raid + core_raid,
                    uncertainty=max(entry.uncertainty, uncertainty),
                )


def project_threat_field(
    sightings: Sequence[EntitySighting],
    now_tick: int,
    *,
    radius: int = THREAT_FIELD_RADIUS,
    core_raid_radius: int = CORE_RAID_RADIUS,
    generated_at_ms: int = 0,
    freshness_window: FreshnessWindow | None = None,
    include_historical: bool = False,
) -> ThreatField:
    """Project a threat field from sightings (TS projectThreatField + stale guard).

    - Visible combat units project directCombat (weight 1, distance decayed);
    - remembered (non-visible) combat units project projectedCombat (weight =
      decayed confidence);
    - enemy Cores project coreRaid (weight = decayed confidence);
    - per-cell uncertainty is the max of (1 - confidence) across contributors.
    HISTORICAL sightings are excluded by default so stale data never amplifies
    threat; ``include_historical=True`` reproduces exact TS output.
    """

    _require_int("now_tick", now_tick)
    _require_int("radius", radius, minimum=1)
    _require_int("core_raid_radius", core_raid_radius, minimum=1)
    if generated_at_ms is not None:
        _require_int("generated_at_ms", generated_at_ms, minimum=0)
    generated_ms = 0 if generated_at_ms is None else generated_at_ms
    if not isinstance(sightings, Sequence) or isinstance(sightings, (str, bytes)):
        raise TypeError("sightings must be a sequence of EntitySighting")
    for sighting in sightings:
        if not isinstance(sighting, EntitySighting):
            raise TypeError("sightings must contain only EntitySighting")

    window = freshness_window or FreshnessWindow()
    active = list(sightings)
    if not include_historical:
        active = [
            s
            for s in active
            if classify_sighting_freshness(s, now_tick, window) is not IntelFreshness.HISTORICAL
        ]

    cells: dict[str, ThreatCell] = {}
    for sighting in active:
        if sighting.kind is not SightingKind.UNIT and sighting.kind is not SightingKind.CORE:
            continue
        if sighting.kind is SightingKind.UNIT and not is_combat_unit(sighting.unit_type):
            continue  # WORKER never projects threat
        confidence = current_confidence(sighting, now_tick)
        uncertainty = 1.0 - confidence
        if sighting.kind is SightingKind.UNIT:
            if sighting.currently_visible or sighting.last_seen_tick == now_tick:
                _project_around(cells, sighting.position, radius, 1.0, uncertainty, "direct_combat")
            else:
                _project_around(
                    cells, sighting.position, radius, confidence, uncertainty, "projected_combat"
                )
        else:
            _project_around(
                cells, sighting.position, core_raid_radius, confidence, uncertainty, "core_raid"
            )

    # Deterministic max: highest directCombat; ties break to the smallest cell
    # key (TS picks the first inserted; we make the tie explicit and stable).
    max_direct: ThreatCell | None = None
    for key in sorted(cells):
        cell = cells[key]
        if max_direct is None or cell.direct_combat > max_direct.direct_combat:
            max_direct = cell

    if active:
        tick_window = (
            min(s.first_seen_tick for s in active),
            max(s.last_seen_tick for s in active),
        )
    else:
        tick_window = (now_tick, now_tick)

    return ThreatField(
        cells=cells,
        max_direct=max_direct,
        estimated_combat_force=estimated_force(active, now_tick),
        tick_window=tick_window,
        generated_at_ms=generated_ms,
    )


def adjust_with_leaderboard_prior(
    field: ThreatField,
    sightings: Sequence[EntitySighting],
    owner_aggression: Mapping[str, float],
    strength: float = 0.3,
) -> ThreatField:
    """Add a weak leaderboard prior near known enemy cores (TS, idempotent).

    The leaderboard never creates map entities; it only boosts coreRaid around
    a CORE whose owner has a positive aggression prior.
    """

    if not isinstance(field, ThreatField):
        raise TypeError("field must be a ThreatField")
    if not isinstance(sightings, Sequence) or isinstance(sightings, (str, bytes)):
        raise TypeError("sightings must be a sequence of EntitySighting")
    if not isinstance(owner_aggression, Mapping):
        raise TypeError("owner_aggression must be a mapping of username -> 0..1")
    if isinstance(strength, bool) or not isinstance(strength, (int, float)):
        raise TypeError("strength must be a number")
    strength_float = float(strength)
    if not math.isfinite(strength_float) or strength_float < 0:
        raise ValueError("strength must be a finite non-negative number")
    if len(owner_aggression) == 0:
        return field

    cells: dict[str, ThreatCell] = {key: cell for key, cell in field.cells.items()}
    for sighting in sightings:
        if sighting.kind is not SightingKind.CORE or sighting.owner_username is None:
            continue
        prior = owner_aggression.get(sighting.owner_username)
        if prior is None or prior <= 0:
            continue
        _project_around(
            cells,
            sighting.position,
            CORE_RAID_RADIUS,
            prior * strength_float,
            1.0 - prior,
            "core_raid",
        )

    return ThreatField(
        cells=cells,
        max_direct=field.max_direct,
        estimated_combat_force=field.estimated_combat_force,
        tick_window=field.tick_window,
        generated_at_ms=field.generated_at_ms,
    )


# --- threat-summary.ts semantics ---


@dataclass(frozen=True, slots=True)
class ThreatSummaryConfig:
    """Sector summary weights (TS ``ThreatSummaryConfig``)."""

    __canonical_name__ = "arena-hero.threat-summary-config.v1"

    core_weight: float = 4.0
    unit_weight: float = 1.0
    distance_scale: float = 16.0
    max_distance: float = 96.0
    high_score_threshold: float = 0.55
    max_sector_score: float = 16.0


def _finite_non_negative(value: object, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("config values must be numbers")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return fallback
    return number


def _finite_positive(value: object, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("config values must be numbers")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return fallback
    return number


def resolve_threat_summary_config(
    config: ThreatSummaryConfig | Mapping[str, object] | None = None,
) -> ThreatSummaryConfig:
    """Resolve partial config to full defaults (TS resolveThreatSummaryConfig)."""

    if config is None:
        return ThreatSummaryConfig()
    if not isinstance(config, Mapping) and not isinstance(config, ThreatSummaryConfig):
        raise TypeError("config must be a mapping or ThreatSummaryConfig")
    defaults = ThreatSummaryConfig()
    if isinstance(config, ThreatSummaryConfig):
        values = {
            "core_weight": config.core_weight,
            "unit_weight": config.unit_weight,
            "distance_scale": config.distance_scale,
            "max_distance": config.max_distance,
            "high_score_threshold": config.high_score_threshold,
            "max_sector_score": config.max_sector_score,
        }
    else:
        values = dict(config)
    return ThreatSummaryConfig(
        core_weight=_finite_non_negative(
            values.get("core_weight", defaults.core_weight), defaults.core_weight
        ),
        unit_weight=_finite_non_negative(
            values.get("unit_weight", defaults.unit_weight), defaults.unit_weight
        ),
        distance_scale=_finite_positive(
            values.get("distance_scale", defaults.distance_scale), defaults.distance_scale
        ),
        max_distance=_finite_positive(
            values.get("max_distance", defaults.max_distance), defaults.max_distance
        ),
        high_score_threshold=_finite_non_negative(
            values.get("high_score_threshold", defaults.high_score_threshold),
            defaults.high_score_threshold,
        ),
        max_sector_score=_finite_positive(
            values.get("max_sector_score", defaults.max_sector_score), defaults.max_sector_score
        ),
    )


@dataclass(frozen=True, slots=True)
class ThreatSector:
    """One directional sector of a tenant threat summary (TS ``ThreatSector``)."""

    __canonical_name__ = "arena-hero.threat-sector.v1"

    direction: ThreatDirection
    score: float
    entity_count: int
    nearest_distance: int | None
    entity_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.direction, ThreatDirection):
            raise TypeError("threat sector direction must be a ThreatDirection")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise TypeError("threat sector score must be a number")
        object.__setattr__(self, "score", float(self.score))
        _require_int("threat sector entity_count", self.entity_count)
        if self.nearest_distance is not None:
            _require_int("threat sector nearest_distance", self.nearest_distance, minimum=1)
        if not isinstance(self.entity_keys, tuple) or not all(
            isinstance(k, str) for k in self.entity_keys
        ):
            raise TypeError("threat sector entity_keys must be a tuple of strings")


@dataclass(frozen=True, slots=True)
class TenantThreatSummary:
    """Tenant-relative threat summary (TS ``TenantThreatSummary``)."""

    __canonical_name__ = "arena-hero.tenant-threat-summary.v1"

    tenant_id: TenantId
    core_position: Coordinate | None
    sectors: tuple[ThreatSector, ...]
    high_directions: tuple[ThreatDirection, ...]
    multi_direction_pressure: bool
    total_score: float

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("threat summary tenant_id must be a TenantId")
        if self.core_position is not None and not isinstance(self.core_position, Coordinate):
            raise TypeError("threat summary core_position must be a Coordinate or None")
        if not isinstance(self.sectors, tuple) or len(self.sectors) != len(DIRECTIONS):
            raise TypeError("threat summary sectors must cover every direction")
        if not isinstance(self.high_directions, tuple) or not all(
            isinstance(d, ThreatDirection) for d in self.high_directions
        ):
            raise TypeError("threat summary high_directions must be a tuple of ThreatDirection")
        if not isinstance(self.multi_direction_pressure, bool):
            raise TypeError("threat summary multi_direction_pressure must be a boolean")
        if isinstance(self.total_score, bool) or not isinstance(self.total_score, (int, float)):
            raise TypeError("threat summary total_score must be a number")
        object.__setattr__(self, "total_score", float(self.total_score))


def _manhattan(a: Coordinate, b: Coordinate) -> int:
    return abs(a.x - b.x) + abs(a.y - b.y)


def threat_direction(core: Coordinate, target: Coordinate) -> ThreatDirection:
    """Eight-direction sector of target relative to core (TS threatDirection)."""

    dx = target.x - core.x
    dy = target.y - core.y
    if dx == 0 and dy == 0:
        return ThreatDirection.N
    if dx == 0:
        return ThreatDirection.N if dy > 0 else ThreatDirection.S
    if dy == 0:
        return ThreatDirection.E if dx > 0 else ThreatDirection.W
    if dx > 0 and dy > 0:
        return ThreatDirection.NE
    if dx > 0 and dy < 0:
        return ThreatDirection.SE
    if dx < 0 and dy < 0:
        return ThreatDirection.SW
    return ThreatDirection.NW


def _contribution(
    *,
    kind: SightingKind,
    confidence: float,
    distance: int,
    config: ThreatSummaryConfig,
) -> float:
    """Weighted, distance-decayed score contribution (TS contribution)."""

    if distance > config.max_distance:
        return 0.0
    weight = config.core_weight if kind is SightingKind.CORE else config.unit_weight
    score = weight * confidence / (1 + distance / config.distance_scale)
    if not math.isfinite(score):
        return 0.0
    return max(0.0, score)


def _non_adjacent_high_pressure(high: Sequence[ThreatDirection]) -> bool:
    """True when high-pressure directions are not all adjacent (TS)."""

    if len(high) < 2:
        return False
    indices = [DIRECTIONS.index(direction) for direction in high]
    for i, left in enumerate(indices):
        for right in indices[i + 1 :]:
            raw = abs(left - right)
            circular = min(raw, len(DIRECTIONS) - raw)
            if circular >= 2:
                return True
    return False


def build_tenant_threat(
    *,
    tenant_id: TenantId,
    core_position: Coordinate | None,
    sightings: Sequence[tuple[str, SightingKind, Coordinate, float]],
    config: ThreatSummaryConfig,
) -> TenantThreatSummary:
    """Derive one tenant's directional threat summary (TS buildTenantThreat)."""

    if core_position is None:
        return TenantThreatSummary(
            tenant_id=tenant_id,
            core_position=None,
            sectors=tuple(
                ThreatSector(
                    direction=direction,
                    score=0.0,
                    entity_count=0,
                    nearest_distance=None,
                    entity_keys=(),
                )
                for direction in DIRECTIONS
            ),
            high_directions=(),
            multi_direction_pressure=False,
            total_score=0.0,
        )

    buckets: dict[ThreatDirection, _SectorAccumulator] = {
        direction: _SectorAccumulator() for direction in DIRECTIONS
    }
    for key, kind, position, confidence in sightings:
        if kind is not SightingKind.CORE and kind is not SightingKind.UNIT:
            continue
        distance = _manhattan(core_position, position)
        score = _contribution(kind=kind, confidence=confidence, distance=distance, config=config)
        if score <= 0:
            continue
        bucket = buckets[threat_direction(core_position, position)]
        bucket.score = min(config.max_sector_score, bucket.score + score)
        bucket.distances.append(distance)
        bucket.keys.append(key)

    sectors = tuple(
        ThreatSector(
            direction=direction,
            score=_round_ts(bucket.score),
            entity_count=len(bucket.keys),
            nearest_distance=None if not bucket.distances else min(bucket.distances),
            entity_keys=tuple(sorted(bucket.keys)),
        )
        for direction, bucket in ((d, buckets[d]) for d in DIRECTIONS)
    )
    high_directions = tuple(
        sector.direction for sector in sectors if sector.score >= config.high_score_threshold
    )
    total = _round_ts(sum(sector.score for sector in sectors))
    return TenantThreatSummary(
        tenant_id=tenant_id,
        core_position=core_position,
        sectors=sectors,
        high_directions=high_directions,
        multi_direction_pressure=_non_adjacent_high_pressure(high_directions),
        total_score=total,
    )


def build_threat_summaries_from_snapshot(
    snapshot: AllianceSnapshot,
    config_input: ThreatSummaryConfig | Mapping[str, object] | None = None,
) -> tuple[TenantThreatSummary, ...]:
    """Tenant-relative directional summaries from a canonical snapshot.

    Matches TS buildAllianceThreatSummariesFromSnapshot, except HISTORICAL
    sightings are excluded (TS's intel path deliberately excludes historical-
    only sightings; the snapshot variant does not). This is the fail-closed
    stale-data guard required by P4-17.
    """

    if not isinstance(snapshot, AllianceSnapshot):
        raise TypeError("snapshot must be an AllianceSnapshot")
    config = resolve_threat_summary_config(config_input)
    reports = sorted(snapshot.members.values(), key=lambda m: m.tenant_id.value)
    directional: list[tuple[str, SightingKind, Coordinate, float]] = []
    for sighting in snapshot.sightings:
        if sighting.kind is not SightingKind.CORE and sighting.kind is not SightingKind.UNIT:
            continue
        if snapshot.freshness.get(sighting.key) is IntelFreshness.HISTORICAL:
            continue
        confidence = sighting.confidence if math.isfinite(sighting.confidence) else 0.0
        directional.append(
            (
                sighting.key,
                sighting.kind,
                sighting.position,
                max(0.0, min(1.0, confidence)),
            )
        )
    return tuple(
        build_tenant_threat(
            tenant_id=member.tenant_id,
            core_position=member.core.position if member.core is not None else None,
            sightings=directional,
            config=config,
        )
        for member in reports
    )


__all__ = [
    "CORE_RAID_RADIUS",
    "DIRECTIONS",
    "THREAT_FIELD_RADIUS",
    "TenantThreatSummary",
    "ThreatCell",
    "ThreatDirection",
    "ThreatField",
    "ThreatSector",
    "ThreatSummaryConfig",
    "adjust_with_leaderboard_prior",
    "build_tenant_threat",
    "build_threat_summaries_from_snapshot",
    "project_threat_field",
    "proximity_weight",
    "resolve_threat_summary_config",
    "threat_direction",
]
