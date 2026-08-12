"""Alliance shared-intelligence fusion read model (W20).

Port of the legacy TypeScript ``lib/alliance/shared-intel.ts`` (P5-2 snapshot
commit ``8cf5cbb``): pure, deterministic, I/O-free fusion of per-tenant entity
sightings into one alliance shared-intel view. Historical knowledge is kept
queryable but never inflates current force estimates; every sighting is
classified LIVE / RECENT / HISTORICAL with decayed confidence, and ally entity
IDs are removed before any enemy aggregation.

This is the missing projection contract the Command Center ``/api/alliance/
snapshot`` payload needs: the canonical snapshot domain model
(``snapshot.py`` + ``threat.py``) builds members/counts/threat, and this module
adds the fused intel view (``SharedIntelView``). No I/O, no API imports; the
Command Center API layer stays a thin wrapper.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arena_hero_agent.domain import Coordinate, TenantId

from .snapshot import (
    DEFAULT_FRESHNESS_WINDOW_TICKS,
    DEFAULT_LIVE_WINDOW_TICKS,
    EntitySighting,
    IntelFreshness,
)

DEFAULT_CONFIDENCE_TAU_TICKS = 8
DEFAULT_CONFIDENCE_FLOOR = 0.05

_EVIDENCE_RANK = {
    "LIVE": 4,
    "CALIBRATION": 3,
    "LEADERBOARD": 2,
    "HISTORY": 1,
}


@dataclass(frozen=True, slots=True)
class SharedIntelConfig:
    """Shared-intel freshness/confidence windows (TS ``SharedIntelConfig``)."""

    live_window_ticks: int = DEFAULT_LIVE_WINDOW_TICKS
    freshness_window_ticks: int = DEFAULT_FRESHNESS_WINDOW_TICKS
    confidence_tau_ticks: float = DEFAULT_CONFIDENCE_TAU_TICKS
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR


def resolve_shared_intel_config(
    config: Mapping[str, Any] | SharedIntelConfig | None = None,
) -> SharedIntelConfig:
    """Resolve a partial config to a validated defaulted config (TS resolve)."""
    values: dict[str, Any] = {}
    if config is not None:
        if isinstance(config, SharedIntelConfig):
            values = {
                "live_window_ticks": config.live_window_ticks,
                "freshness_window_ticks": config.freshness_window_ticks,
                "confidence_tau_ticks": config.confidence_tau_ticks,
                "confidence_floor": config.confidence_floor,
            }
        elif isinstance(config, Mapping):
            values = dict(config)
        else:
            raise TypeError("config must be a SharedIntelConfig or a mapping")
    live = max(0, _sanitize_int(values.get("live_window_ticks"), DEFAULT_LIVE_WINDOW_TICKS))
    fresh = max(
        live,
        _sanitize_int(values.get("freshness_window_ticks"), DEFAULT_FRESHNESS_WINDOW_TICKS),
    )
    tau = max(1e-9, _finite_or(values.get("confidence_tau_ticks"), DEFAULT_CONFIDENCE_TAU_TICKS))
    floor = _clamp01(_finite_or(values.get("confidence_floor"), DEFAULT_CONFIDENCE_FLOOR))
    return SharedIntelConfig(
        live_window_ticks=live,
        freshness_window_ticks=fresh,
        confidence_tau_ticks=tau,
        confidence_floor=floor,
    )


@dataclass(frozen=True, slots=True)
class FusedEntitySighting:
    """A deduplicated sighting with fusion metadata (TS ``FusedEntitySighting``)."""

    __canonical_name__ = "arena-hero.fused-entity-sighting.v1"

    key: str
    kind: Any
    unit_type: Any
    entity_id: str | None
    owner_username: str | None
    position: Coordinate
    source_tenant: TenantId
    first_seen_tick: int
    last_seen_tick: int
    currently_visible: bool
    confidence: float
    evidence: Any
    source_tenants: tuple[str, ...]
    age_ticks: int
    decayed_confidence: float
    freshness: IntelFreshness

    def __post_init__(self) -> None:
        if not isinstance(self.source_tenants, tuple) or not all(
            isinstance(item, str) for item in self.source_tenants
        ):
            raise TypeError("source_tenants must be a tuple of strings")
        if isinstance(self.age_ticks, bool) or not isinstance(self.age_ticks, int):
            raise TypeError("age_ticks must be an integer")
        if self.age_ticks < 0:
            raise ValueError("age_ticks cannot be negative")
        if isinstance(self.decayed_confidence, bool) or not isinstance(
            self.decayed_confidence, (int, float)
        ):
            raise TypeError("decayed_confidence must be a number")
        if not math.isfinite(float(self.decayed_confidence)):
            raise ValueError("decayed_confidence must be finite")
        if not isinstance(self.freshness, IntelFreshness):
            raise TypeError("freshness must be an IntelFreshness")


@dataclass(frozen=True, slots=True)
class SharedIntelCounts:
    """Enemy counts per freshness tier (TS ``SharedIntelCounts``)."""

    __canonical_name__ = "arena-hero.shared-intel-counts.v1"

    current_enemy_units: int
    current_enemy_cores: int
    recent_enemy_units: int
    recent_enemy_cores: int
    historical_enemy_units: int
    historical_enemy_cores: int


@dataclass(frozen=True, slots=True)
class SharedIntelView:
    """The fused alliance shared-intel view (TS ``SharedIntelView``)."""

    __canonical_name__ = "arena-hero.shared-intel-view.v1"

    current_tick: int
    member_reports: tuple[dict[str, Any], ...]
    currently_visible: tuple[FusedEntitySighting, ...]
    recent_fused: tuple[FusedEntitySighting, ...]
    historical_known: tuple[FusedEntitySighting, ...]
    counts: SharedIntelCounts


def _sanitize_int(value: object, fallback: int) -> int:
    number = _finite_or(value, fallback)
    return max(0, math.trunc(number))


def _finite_or(value: object, fallback: float) -> float:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else fallback
    return fallback


def _clamp01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, value))


def _stable_compare(left: str, right: str) -> int:
    if left < right:
        return -1
    if left > right:
        return 1
    return 0


def _sighting_age(last_seen_tick: int, current_tick: int) -> int:
    last = _finite_or(last_seen_tick, current_tick)
    now = _finite_or(current_tick, last)
    return max(0, math.trunc(now - last))


def _decayed_confidence(
    sighting: EntitySighting, age_ticks: int, config: SharedIntelConfig
) -> float:
    """Confidence decayed by age; malformed confidence contributes nothing."""
    if not math.isfinite(float(sighting.confidence)):
        return 0.0
    base = _clamp01(float(sighting.confidence))
    if base == 0.0:
        return 0.0
    decayed = base * math.exp(-age_ticks / config.confidence_tau_ticks)
    return _clamp01(max(config.confidence_floor, decayed))


def _winner_compare(
    left: EntitySighting | FusedEntitySighting,
    right: EntitySighting | FusedEntitySighting,
) -> int:
    """Deterministic winner precedence: newest > visible > evidence > confidence > tenant."""
    if left.last_seen_tick != right.last_seen_tick:
        return right.last_seen_tick - left.last_seen_tick
    if left.currently_visible != right.currently_visible:
        return -1 if left.currently_visible else 1
    evidence = _EVIDENCE_RANK.get(str(right.evidence), 0) - _EVIDENCE_RANK.get(
        str(left.evidence), 0
    )
    if evidence != 0:
        return evidence
    confidence = _clamp01(float(right.confidence)) - _clamp01(float(left.confidence))
    if confidence != 0:
        return 1 if confidence > 0 else -1
    tenant = _stable_compare(str(left.source_tenant), str(right.source_tenant))
    if tenant != 0:
        return tenant
    owner = _stable_compare(left.owner_username or "", right.owner_username or "")
    if owner != 0:
        return owner
    kind = _stable_compare(str(left.kind), str(right.kind))
    if kind != 0:
        return kind
    if left.position.x != right.position.x:
        return left.position.x - right.position.x
    return left.position.y - right.position.y


def _classify_freshness(
    winner: EntitySighting,
    age_ticks: int,
    config: SharedIntelConfig,
) -> IntelFreshness:
    if winner.currently_visible and age_ticks <= config.live_window_ticks:
        return IntelFreshness.LIVE
    if age_ticks <= config.freshness_window_ticks:
        return IntelFreshness.RECENT
    return IntelFreshness.HISTORICAL


def _fused_compare(left: FusedEntitySighting, right: FusedEntitySighting) -> int:
    key = _stable_compare(left.key, right.key)
    if key != 0:
        return key
    return _winner_compare(left, right)


def fuse_entity_sightings(
    sightings: Sequence[EntitySighting],
    current_tick: int,
    config: Mapping[str, Any] | SharedIntelConfig | None = None,
) -> tuple[FusedEntitySighting, ...]:
    """Deterministically deduplicate sightings by entity key (TS ``fuseEntitySightings``)."""
    resolved = resolve_shared_intel_config(config)
    by_key: dict[str, list[EntitySighting]] = {}
    for sighting in sightings:
        if not isinstance(sighting, EntitySighting):
            raise TypeError("sightings must contain only EntitySighting")
        if not isinstance(sighting.key, str) or not sighting.key:
            continue
        by_key.setdefault(sighting.key, []).append(sighting)
    out: list[FusedEntitySighting] = []
    for key in sorted(by_key):
        bucket = by_key[key]
        winner = sorted(bucket, key=_cmp_to_key(_winner_compare))[0]
        sources = tuple(sorted({str(s.source_tenant) for s in bucket}))
        age_ticks = _sighting_age(winner.last_seen_tick, current_tick)
        out.append(
            FusedEntitySighting(
                key=winner.key,
                kind=winner.kind,
                unit_type=winner.unit_type,
                entity_id=winner.entity_id,
                owner_username=winner.owner_username,
                position=winner.position,
                source_tenant=winner.source_tenant,
                first_seen_tick=winner.first_seen_tick,
                last_seen_tick=winner.last_seen_tick,
                currently_visible=winner.currently_visible,
                confidence=_clamp01(float(winner.confidence)),
                evidence=winner.evidence,
                source_tenants=sources,
                age_ticks=age_ticks,
                decayed_confidence=_decayed_confidence(winner, age_ticks, resolved),
                freshness=_classify_freshness(winner, age_ticks, resolved),
            )
        )
    return tuple(sorted(out, key=_cmp_to_key(_fused_compare)))


def _report_compare(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    """Deterministic report ordering: tenantId asc, then tick asc (TS)."""
    tenant = _stable_compare(str(left.get("tenantId", "")), str(right.get("tenantId", "")))
    if tenant != 0:
        return tenant
    return int(left.get("tick", 0) or 0) - int(right.get("tick", 0) or 0)


def _cmp_to_key(compare: Any) -> Any:  # noqa: ANN001 - stdlib cmp_to_key shim for small sorts
    import functools

    return functools.cmp_to_key(compare)


def aggregate_alliance_intel(
    *,
    sightings: Sequence[EntitySighting],
    ally_entity_ids: frozenset[str] | Sequence[str] | None = None,
    current_tick: int,
    config: Mapping[str, Any] | SharedIntelConfig | None = None,
    member_reports: Sequence[Mapping[str, Any]] | None = None,
) -> SharedIntelView:
    """Build the alliance shared-intel view (TS ``aggregateAllianceIntel``)."""
    resolved = resolve_shared_intel_config(config)
    ally: frozenset[str] = frozenset(ally_entity_ids or ())
    for sighting in sightings:
        if not isinstance(sighting, EntitySighting):
            raise TypeError("sightings must contain only EntitySighting")
    filtered = [
        sighting
        for sighting in sightings
        if sighting.key not in ally
        and (sighting.entity_id is None or sighting.entity_id not in ally)
    ]
    historical_known = fuse_entity_sightings(filtered, current_tick, resolved)
    currently_visible = tuple(s for s in historical_known if s.freshness is IntelFreshness.LIVE)
    recent_fused = tuple(
        s for s in historical_known if s.freshness is not IntelFreshness.HISTORICAL
    )
    reports = tuple(
        sorted(
            (dict(report) for report in (member_reports or ())),
            key=_cmp_to_key(_report_compare),
        )
    )

    def count_kind(items: Sequence[FusedEntitySighting], kind: str) -> int:
        return sum(1 for item in items if str(item.kind) == kind)

    current_tick_value = _sanitize_int(current_tick, 0)
    return SharedIntelView(
        current_tick=current_tick_value,
        member_reports=reports,
        currently_visible=currently_visible,
        recent_fused=recent_fused,
        historical_known=historical_known,
        counts=SharedIntelCounts(
            current_enemy_units=count_kind(currently_visible, "UNIT"),
            current_enemy_cores=count_kind(currently_visible, "CORE"),
            recent_enemy_units=count_kind(recent_fused, "UNIT"),
            recent_enemy_cores=count_kind(recent_fused, "CORE"),
            historical_enemy_units=count_kind(historical_known, "UNIT"),
            historical_enemy_cores=count_kind(historical_known, "CORE"),
        ),
    )


__all__ = [
    "DEFAULT_CONFIDENCE_FLOOR",
    "DEFAULT_CONFIDENCE_TAU_TICKS",
    "FusedEntitySighting",
    "SharedIntelConfig",
    "SharedIntelCounts",
    "SharedIntelView",
    "aggregate_alliance_intel",
    "fuse_entity_sightings",
    "resolve_shared_intel_config",
]
