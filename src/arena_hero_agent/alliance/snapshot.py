"""Cross-tenant read-only alliance snapshot with stale-data detection.

P4-17 builds the deterministic alliance read model that P4-16's command bus
lacked: merge multi-tenant observations into one immutable snapshot, identify
stale observations by a tick freshness window, and aggregate force counts
without the "83 enemy units" double-count artifact (TS lib/alliance counts.ts
semantics). Pure functions only; no I/O.

Stale-data strategy (chosen, recorded in PROGRESS.md): each sighting is
classified LIVE / RECENT / HISTORICAL using the shared-intel freshness windows
(liveWindowTicks=1, freshnessWindowTicks=8 by default). HISTORICAL sightings
are fail-closed: they are marked in ``stale_sighting_keys``, never counted as
currently-visible, and never amplify force or threat estimates. Counts keep
the TS counts.ts four-way semantics so the snapshot can be diffed against the
TS oracle.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from arena_hero_agent.domain import Coordinate, TenantId

if TYPE_CHECKING:
    from .threat import ThreatField

# --- TS lib/alliance constants (sightings.ts / counts.ts / shared-intel.ts) ---

UNIT_SAME_TICK_GATE = 0
CORE_SPATIAL_GATE = 8
CONFIDENCE_FLOOR = 0.05
UNIT_TAU = 6
CORE_TAU = 96
OBSTACLE_TAU = math.inf
RESOURCE_TAU = 24
RECENT_UNIQUE_WINDOW = 300
DEFAULT_LIVE_WINDOW_TICKS = 1
DEFAULT_FRESHNESS_WINDOW_TICKS = 8


class SightingKind(StrEnum):
    """Observed entity category (TS ``SightingKind``)."""

    __canonical_name__ = "arena-hero.sighting-kind.v1"

    CORE = "CORE"
    UNIT = "UNIT"
    RESOURCE = "RESOURCE"


class UnitType(StrEnum):
    """Combat unit role (TS ``UnitType``)."""

    __canonical_name__ = "arena-hero.unit-type.v1"

    WORKER = "WORKER"
    VANGUARD = "VANGUARD"
    RANGER = "RANGER"


class EvidenceKind(StrEnum):
    """Sighting evidence provenance (TS ``EvidenceKind``)."""

    __canonical_name__ = "arena-hero.evidence-kind.v1"

    LIVE = "LIVE"
    CALIBRATION = "CALIBRATION"
    HISTORY = "HISTORY"
    LEADERBOARD = "LEADERBOARD"


class MemberStatus(StrEnum):
    """Compressed alliance member status (TS ``AllianceMemberState.status``)."""

    __canonical_name__ = "arena-hero.member-status.v1"

    READY = "READY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    RESPAWNING = "RESPAWNING"


class IntelFreshness(StrEnum):
    """Tick-window freshness of a sighting (TS shared-intel ``IntelFreshness``)."""

    __canonical_name__ = "arena-hero.intel-freshness.v1"

    LIVE = "LIVE"
    RECENT = "RECENT"
    HISTORICAL = "HISTORICAL"


def _require_int(name: str, value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _require_float01(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be within [0, 1]")
    return number


@dataclass(frozen=True, slots=True)
class CoreRef:
    """Compressed enemy/ally core reference inside a member state."""

    __canonical_name__ = "arena-hero.core-ref.v1"

    id: str
    position: Coordinate
    hp: int
    shield: int
    moving: bool

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("core id must be a non-empty string")
        if not isinstance(self.position, Coordinate):
            raise TypeError("core position must be a Coordinate")
        if isinstance(self.hp, bool) or not isinstance(self.hp, int):
            raise TypeError("core hp must be an integer")
        if isinstance(self.shield, bool) or not isinstance(self.shield, int):
            raise TypeError("core shield must be an integer")
        if self.hp < 0 or self.shield < 0:
            raise ValueError("core hp/shield cannot be negative")
        if not isinstance(self.moving, bool):
            raise TypeError("core moving must be a boolean")


@dataclass(frozen=True, slots=True)
class EntitySighting:
    """One tenant's observation of one entity (TS ``EntitySighting``)."""

    __canonical_name__ = "arena-hero.entity-sighting.v1"

    key: str
    kind: SightingKind
    unit_type: UnitType | None
    entity_id: str | None
    owner_username: str | None
    position: Coordinate
    source_tenant: TenantId
    first_seen_tick: int
    last_seen_tick: int
    currently_visible: bool
    confidence: float
    evidence: EvidenceKind

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("sighting key must be a non-empty string")
        if not isinstance(self.kind, SightingKind):
            raise TypeError("sighting kind must be a SightingKind")
        if self.unit_type is not None and not isinstance(self.unit_type, UnitType):
            raise TypeError("sighting unit_type must be a UnitType or None")
        if self.entity_id is not None and not isinstance(self.entity_id, str):
            raise TypeError("sighting entity_id must be a string or None")
        if self.owner_username is not None and not isinstance(self.owner_username, str):
            raise TypeError("sighting owner_username must be a string or None")
        if not isinstance(self.position, Coordinate):
            raise TypeError("sighting position must be a Coordinate")
        if not isinstance(self.source_tenant, TenantId):
            raise TypeError("sighting source_tenant must be a TenantId")
        _require_int("first_seen_tick", self.first_seen_tick)
        _require_int("last_seen_tick", self.last_seen_tick)
        if self.last_seen_tick < self.first_seen_tick:
            raise ValueError("last_seen_tick cannot precede first_seen_tick")
        if not isinstance(self.currently_visible, bool):
            raise TypeError("sighting currently_visible must be a boolean")
        object.__setattr__(
            self, "confidence", _require_float01("sighting confidence", self.confidence)
        )
        if not isinstance(self.evidence, EvidenceKind):
            raise TypeError("sighting evidence must be an EvidenceKind")


@dataclass(frozen=True, slots=True)
class AllianceMemberState:
    """Compressed member state (TS ``AllianceMemberState``); not a TickState copy."""

    __canonical_name__ = "arena-hero.alliance-member-state.v1"

    tenant_id: TenantId
    tick: int
    observed_at_ms: int
    core: CoreRef | None
    resources: int
    resource_capacity: int
    population: int
    workers: int
    vanguards: int
    rangers: int
    carried_resources: int
    active_fleet_ids: tuple[str, ...]
    local_threat: float
    local_harvest_rate: float
    status: MemberStatus

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("member tenant_id must be a TenantId")
        _require_int("member tick", self.tick)
        _require_int("member observed_at_ms", self.observed_at_ms)
        if self.core is not None and not isinstance(self.core, CoreRef):
            raise TypeError("member core must be a CoreRef or None")
        for name in (
            "resources",
            "resource_capacity",
            "population",
            "workers",
            "vanguards",
            "rangers",
            "carried_resources",
        ):
            _require_int(f"member {name}", getattr(self, name))
        if not isinstance(self.active_fleet_ids, Sequence) or isinstance(
            self.active_fleet_ids, (str, bytes)
        ):
            raise TypeError("member active_fleet_ids must be a sequence of strings")
        object.__setattr__(self, "active_fleet_ids", tuple(self.active_fleet_ids))
        if not all(isinstance(item, str) for item in self.active_fleet_ids):
            raise TypeError("member active_fleet_ids must contain only strings")
        object.__setattr__(
            self, "local_threat", _require_float01("member local_threat", self.local_threat)
        )
        if isinstance(self.local_harvest_rate, bool) or not isinstance(
            self.local_harvest_rate, (int, float)
        ):
            raise TypeError("member local_harvest_rate must be a number")
        if not math.isfinite(float(self.local_harvest_rate)):
            raise ValueError("member local_harvest_rate must be finite")
        object.__setattr__(self, "local_harvest_rate", float(self.local_harvest_rate))
        if not isinstance(self.status, MemberStatus):
            raise TypeError("member status must be a MemberStatus")


@dataclass(frozen=True, slots=True)
class AllianceObservation:
    """Raw per-tenant observation (TS ``AllianceObservation``)."""

    __canonical_name__ = "arena-hero.alliance-observation.v1"

    tenant_id: TenantId
    tick: int
    kind: SightingKind
    entity_id: str | None = None
    owner_username: str | None = None
    unit_type: UnitType | None = None
    controlled: bool = False
    position: Coordinate = Coordinate(0, 0)
    evidence: EvidenceKind = EvidenceKind.CALIBRATION

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("observation tenant_id must be a TenantId")
        _require_int("observation tick", self.tick)
        if not isinstance(self.kind, SightingKind):
            raise TypeError("observation kind must be a SightingKind")
        if self.entity_id is not None and not isinstance(self.entity_id, str):
            raise TypeError("observation entity_id must be a string or None")
        if self.owner_username is not None and not isinstance(self.owner_username, str):
            raise TypeError("observation owner_username must be a string or None")
        if self.unit_type is not None and not isinstance(self.unit_type, UnitType):
            raise TypeError("observation unit_type must be a UnitType or None")
        if not isinstance(self.controlled, bool):
            raise TypeError("observation controlled must be a boolean")
        if not isinstance(self.position, Coordinate):
            raise TypeError("observation position must be a Coordinate")
        if not isinstance(self.evidence, EvidenceKind):
            raise TypeError("observation evidence must be an EvidenceKind")


@dataclass(frozen=True, slots=True)
class AllianceForceCounts:
    """Four-way force semantics (TS ``AllianceForceCounts``)."""

    __canonical_name__ = "arena-hero.alliance-force-counts.v1"

    current_visible_combat: int
    recent_unique_combat: int
    historical_sighting_count: int
    estimated_force: float

    def __post_init__(self) -> None:
        for name in ("current_visible_combat", "recent_unique_combat", "historical_sighting_count"):
            _require_int(name, getattr(self, name))
        if isinstance(self.estimated_force, bool) or not isinstance(
            self.estimated_force, (int, float)
        ):
            raise TypeError("estimated_force must be a number")
        if not math.isfinite(float(self.estimated_force)):
            raise ValueError("estimated_force must be finite")
        object.__setattr__(self, "estimated_force", float(self.estimated_force))


# --- sightings.ts semantics: dedupe, merge, confidence decay ---


def is_combat_unit(unit_type: UnitType | None) -> bool:
    """VANGUARD/RANGER project direct combat; WORKER does not (TS isCombatUnit)."""

    return unit_type is UnitType.VANGUARD or unit_type is UnitType.RANGER


def tau_for(kind: SightingKind, unit_type: UnitType | None = None) -> float:
    """Confidence decay time constant by kind/unit role (TS tauFor)."""

    if kind is SightingKind.CORE:
        return CORE_TAU
    if kind is SightingKind.RESOURCE:
        return RESOURCE_TAU
    if kind is SightingKind.UNIT:
        return UNIT_TAU if is_combat_unit(unit_type) else UNIT_TAU * 2
    return UNIT_TAU


def confidence_at(age_ticks: int, tau: float) -> float:
    """confidence(age) = max(floor, exp(-age / tau)) (TS confidenceAt)."""

    _require_int("age_ticks", age_ticks)
    if not math.isfinite(tau) or tau <= 0:
        raise ValueError("tau must be a positive finite number")
    if tau == math.inf:
        return 1.0 if age_ticks <= 0 else CONFIDENCE_FLOOR
    return max(CONFIDENCE_FLOOR, math.exp(-age_ticks / tau))


def current_confidence(sighting: EntitySighting, now_tick: int) -> float:
    """Decayed current confidence; currently visible forces 1 (TS currentConfidence)."""

    if sighting.currently_visible:
        return 1.0
    age = max(0, _require_int("now_tick", now_tick) - sighting.last_seen_tick)
    return confidence_at(age, tau_for(sighting.kind, sighting.unit_type))


def merge_key(
    *,
    kind: SightingKind,
    entity_id: str | None = None,
    owner_username: str | None = None,
    source_tenant: TenantId,
    tick: int | None = None,
    position: Coordinate,
) -> str:
    """Deterministic dedupe key (TS mergeKey rules 1-4)."""

    if not isinstance(kind, SightingKind):
        raise TypeError("merge_key kind must be a SightingKind")
    if entity_id is not None and not isinstance(entity_id, str):
        raise TypeError("merge_key entity_id must be a string or None")
    if owner_username is not None and not isinstance(owner_username, str):
        raise TypeError("merge_key owner_username must be a string or None")
    if not isinstance(source_tenant, TenantId):
        raise TypeError("merge_key source_tenant must be a TenantId")
    if not isinstance(position, Coordinate):
        raise TypeError("merge_key position must be a Coordinate")
    if entity_id:
        return f"{kind.value}:{entity_id}"
    if kind is SightingKind.CORE and owner_username:
        return f"CORE:{owner_username}"
    if kind is SightingKind.UNIT:
        observed_tick = _require_int("merge_key tick", 0 if tick is None else tick)
        return f"UNIT:{source_tenant.value}:{observed_tick}:{position.x},{position.y}"
    return f"{kind.value}:{source_tenant.value}:{position.x},{position.y}"


def _sighting_key_for_raw(raw: AllianceObservation) -> str:
    return merge_key(
        kind=raw.kind,
        entity_id=raw.entity_id,
        owner_username=raw.owner_username,
        source_tenant=raw.tenant_id,
        tick=raw.tick,
        position=raw.position,
    )


def _merged_confidence(
    *, kind: SightingKind, unit_type: UnitType | None, last_seen_tick: int, now_tick: int
) -> float:
    """Confidence after a same-key merge (TS currentConfidence over merged fields)."""

    if last_seen_tick == now_tick:
        return 1.0
    age = max(0, now_tick - last_seen_tick)
    return confidence_at(age, tau_for(kind, unit_type))


def normalize_sighting(
    raw: AllianceObservation,
    existing: EntitySighting | None,
    now_tick: int,
) -> EntitySighting:
    """Normalize one raw observation into an EntitySighting (TS normalizeSighting)."""

    if not isinstance(raw, AllianceObservation):
        raise TypeError("raw must be an AllianceObservation")
    _require_int("now_tick", now_tick)
    key = _sighting_key_for_raw(raw)
    visible = raw.tick == now_tick

    # Spatial gate: an id-less enemy CORE with the same owner is a new entity
    # when it drifts beyond CORE_SPATIAL_GATE (avoids permanently merging
    # same-name multi-base owners); otherwise it merges under one key.
    if (
        existing is not None
        and existing.kind is SightingKind.CORE
        and raw.kind is SightingKind.CORE
        and existing.owner_username is not None
        and existing.owner_username == raw.owner_username
        and existing.entity_id is None
        and raw.entity_id is None
        and existing.key == key
    ):
        drift = abs(existing.position.x - raw.position.x) + abs(
            existing.position.y - raw.position.y
        )
        if drift > CORE_SPATIAL_GATE:
            return _fresh_sighting(
                key=f"CORE:{raw.owner_username}:{raw.position.x},{raw.position.y}",
                raw=raw,
                now_tick=now_tick,
            )

    if existing is not None and existing.key == key:
        evidence = (
            existing.evidence
            if raw.evidence is EvidenceKind.LEADERBOARD
            and existing.evidence is not EvidenceKind.LEADERBOARD
            else raw.evidence
        )
        return EntitySighting(
            key=existing.key,
            kind=existing.kind,
            unit_type=existing.unit_type,
            entity_id=existing.entity_id,
            owner_username=existing.owner_username,
            position=raw.position,
            source_tenant=existing.source_tenant,
            first_seen_tick=existing.first_seen_tick,
            last_seen_tick=raw.tick,
            currently_visible=visible,
            confidence=_merged_confidence(
                kind=existing.kind,
                unit_type=existing.unit_type,
                last_seen_tick=raw.tick,
                now_tick=now_tick,
            ),
            evidence=evidence,
        )

    # Spatial-gate merge: same owner, drift within the gate, but different keys
    # (e.g. a previously split history entry returns near its origin).
    if (
        raw.kind is SightingKind.CORE
        and existing is not None
        and existing.kind is SightingKind.CORE
        and existing.owner_username is not None
        and existing.owner_username == raw.owner_username
        and existing.entity_id is None
        and raw.entity_id is None
    ):
        drift = abs(existing.position.x - raw.position.x) + abs(
            existing.position.y - raw.position.y
        )
        if drift <= CORE_SPATIAL_GATE:
            return EntitySighting(
                key=key,
                kind=existing.kind,
                unit_type=existing.unit_type,
                entity_id=existing.entity_id,
                owner_username=existing.owner_username,
                position=raw.position,
                source_tenant=existing.source_tenant,
                first_seen_tick=existing.first_seen_tick,
                last_seen_tick=raw.tick,
                currently_visible=visible,
                confidence=_merged_confidence(
                    kind=existing.kind,
                    unit_type=existing.unit_type,
                    last_seen_tick=raw.tick,
                    now_tick=now_tick,
                ),
                evidence=existing.evidence,
            )

    return _fresh_sighting(key=key, raw=raw, now_tick=now_tick)


def _fresh_sighting(*, key: str, raw: AllianceObservation, now_tick: int) -> EntitySighting:
    return EntitySighting(
        key=key,
        kind=raw.kind,
        unit_type=raw.unit_type,
        entity_id=raw.entity_id,
        owner_username=raw.owner_username,
        position=raw.position,
        source_tenant=raw.tenant_id,
        first_seen_tick=raw.tick,
        last_seen_tick=raw.tick,
        currently_visible=raw.tick == now_tick,
        confidence=1.0,
        evidence=raw.evidence,
    )


def merge_sightings(
    existing: Sequence[EntitySighting],
    raws: Sequence[AllianceObservation],
    now_tick: int,
) -> tuple[EntitySighting, ...]:
    """Merge raw observations into existing sightings by key; idempotent (TS mergeSightings)."""

    if not isinstance(existing, Sequence) or isinstance(existing, (str, bytes)):
        raise TypeError("existing must be a sequence of EntitySighting")
    by_key: dict[str, EntitySighting] = {}
    for sighting in existing:
        if not isinstance(sighting, EntitySighting):
            raise TypeError("existing must contain only EntitySighting")
        by_key[sighting.key] = sighting
    if not isinstance(raws, Sequence) or isinstance(raws, (str, bytes)):
        raise TypeError("raws must be a sequence of AllianceObservation")
    for raw in raws:
        if not isinstance(raw, AllianceObservation):
            raise TypeError("raws must contain only AllianceObservation")
        previous = by_key.get(_sighting_key_for_raw(raw))
        merged = normalize_sighting(raw, previous, now_tick)
        by_key[merged.key] = merged
    return tuple(by_key.values())


# --- counts.ts semantics ---


def is_currently_visible(sighting: EntitySighting, now_tick: int) -> bool:
    """Visible this tick (TS isCurrentlyVisible)."""

    return sighting.currently_visible or sighting.last_seen_tick == now_tick


def _is_combat_sighting(sighting: EntitySighting) -> bool:
    return sighting.kind is SightingKind.UNIT and is_combat_unit(sighting.unit_type)


def current_visible_combat(sightings: Sequence[EntitySighting], now_tick: int) -> int:
    return sum(1 for s in sightings if _is_combat_sighting(s) and is_currently_visible(s, now_tick))


def recent_unique_combat(
    sightings: Sequence[EntitySighting],
    now_tick: int,
    window: int = RECENT_UNIQUE_WINDOW,
) -> int:
    _require_int("window", window, minimum=0)
    keys = {
        s.key for s in sightings if _is_combat_sighting(s) and now_tick - s.last_seen_tick <= window
    }
    return len(keys)


def historical_sighting_count(sightings: Sequence[EntitySighting]) -> int:
    return sum(1 for s in sightings if _is_combat_sighting(s))


def estimated_force(sightings: Sequence[EntitySighting], now_tick: int) -> float:
    """Unique combat entities weighted by decayed confidence (TS estimatedForce)."""

    by_key: dict[str, EntitySighting] = {}
    for sighting in sightings:
        if not _is_combat_sighting(sighting):
            continue
        previous = by_key.get(sighting.key)
        if previous is None or sighting.last_seen_tick > previous.last_seen_tick:
            by_key[sighting.key] = sighting
    return sum(current_confidence(sighting, now_tick) for sighting in by_key.values())


def compute_force_counts(
    sightings: Sequence[EntitySighting],
    now_tick: int,
    *,
    historical_count_override: int | None = None,
) -> AllianceForceCounts:
    """Single entry for the four-way force counts (TS computeForceCounts)."""

    _require_int("now_tick", now_tick)
    if historical_count_override is not None:
        _require_int("historical_count_override", historical_count_override)
    return AllianceForceCounts(
        current_visible_combat=current_visible_combat(sightings, now_tick),
        recent_unique_combat=recent_unique_combat(sightings, now_tick),
        historical_sighting_count=(
            historical_count_override
            if historical_count_override is not None
            else historical_sighting_count(sightings)
        ),
        estimated_force=estimated_force(sightings, now_tick),
    )


# --- shared-intel freshness semantics (stale-data detection) ---


@dataclass(frozen=True, slots=True)
class FreshnessWindow:
    """Tick freshness windows used for stale-data classification."""

    __canonical_name__ = "arena-hero.freshness-window.v1"

    live_window_ticks: int = DEFAULT_LIVE_WINDOW_TICKS
    freshness_window_ticks: int = DEFAULT_FRESHNESS_WINDOW_TICKS

    def __post_init__(self) -> None:
        _require_int("live_window_ticks", self.live_window_ticks)
        _require_int("freshness_window_ticks", self.freshness_window_ticks)
        if self.freshness_window_ticks < self.live_window_ticks:
            raise ValueError("freshness_window_ticks cannot be smaller than live_window_ticks")


def classify_sighting_freshness(
    sighting: EntitySighting,
    now_tick: int,
    window: FreshnessWindow | None = None,
) -> IntelFreshness:
    """Classify a sighting LIVE/RECENT/HISTORICAL (TS shared-intel classifyFreshness)."""

    if not isinstance(sighting, EntitySighting):
        raise TypeError("sighting must be an EntitySighting")
    window = window or FreshnessWindow()
    age = max(0, _require_int("now_tick", now_tick) - sighting.last_seen_tick)
    if sighting.currently_visible and age <= window.live_window_ticks:
        return IntelFreshness.LIVE
    if age <= window.freshness_window_ticks:
        return IntelFreshness.RECENT
    return IntelFreshness.HISTORICAL


# --- snapshot.ts semantics ---


def _stable_compare(left: str, right: str) -> int:
    if left < right:
        return -1
    if left > right:
        return 1
    return 0


def observations_to_sightings(
    observations: Sequence[AllianceObservation],
    now_tick: int,
    default_evidence: EvidenceKind = EvidenceKind.CALIBRATION,
) -> tuple[EntitySighting, ...]:
    """Enemy sightings from observations: drop controlled allies, merge, dedupe."""

    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
        raise TypeError("observations must be a sequence of AllianceObservation")
    if not isinstance(default_evidence, EvidenceKind):
        raise TypeError("default_evidence must be an EvidenceKind")
    raw_enemy: list[AllianceObservation] = []
    for observation in observations:
        if not isinstance(observation, AllianceObservation):
            raise TypeError("observations must contain only AllianceObservation")
        if observation.controlled:
            continue
        raw_enemy.append(observation)
    return merge_sightings((), raw_enemy, now_tick)


@dataclass(frozen=True, slots=True)
class AllianceSnapshot:
    """Immutable cross-tenant snapshot (TS ``AllianceSnapshot`` + stale markers)."""

    __canonical_name__ = "arena-hero.alliance-snapshot.v1"

    revision: int
    tick_window: tuple[int, int]
    generated_at_ms: int
    members: Mapping[TenantId, AllianceMemberState]
    sightings: tuple[EntitySighting, ...]
    ally_entity_ids: frozenset[str]
    threat: ThreatField  # threat.py; TYPE_CHECKING-only to avoid an import cycle
    counts: AllianceForceCounts
    treasury_tenant: TenantId | None
    freshness: Mapping[str, IntelFreshness]
    stale_sighting_keys: frozenset[str]

    def __post_init__(self) -> None:
        _require_int("revision", self.revision, minimum=0)
        if (
            not isinstance(self.tick_window, tuple)
            or len(self.tick_window) != 2
            or any(isinstance(t, bool) or not isinstance(t, int) for t in self.tick_window)
        ):
            raise TypeError("tick_window must be a (start, end) integer pair")
        if self.tick_window[0] < 0 or self.tick_window[1] < self.tick_window[0]:
            raise ValueError("tick_window must be non-negative and end after start")
        _require_int("generated_at_ms", self.generated_at_ms, minimum=0)
        if not isinstance(self.members, Mapping):
            raise TypeError("members must be a mapping of TenantId -> AllianceMemberState")
        object.__setattr__(self, "members", MappingProxyType(dict(self.members)))
        if not isinstance(self.sightings, tuple):
            raise TypeError("sightings must be a tuple of EntitySighting")
        if not isinstance(self.ally_entity_ids, frozenset):
            raise TypeError("ally_entity_ids must be a frozenset of strings")
        if not isinstance(self.counts, AllianceForceCounts):
            raise TypeError("counts must be an AllianceForceCounts")
        if not all(
            hasattr(self.threat, name)
            for name in ("cells", "max_direct", "estimated_combat_force", "tick_window")
        ):
            raise TypeError("threat must be a ThreatField-shaped object")
        if self.treasury_tenant is not None and not isinstance(self.treasury_tenant, TenantId):
            raise TypeError("treasury_tenant must be a TenantId or None")
        if not isinstance(self.freshness, Mapping):
            raise TypeError("freshness must be a mapping of key -> IntelFreshness")
        object.__setattr__(self, "freshness", MappingProxyType(dict(self.freshness)))
        if not isinstance(self.stale_sighting_keys, frozenset):
            raise TypeError("stale_sighting_keys must be a frozenset of strings")


def _sorted_sightings(sightings: Sequence[EntitySighting]) -> tuple[EntitySighting, ...]:
    """Deterministic sort matching TS snapshot.ts: key asc, lastSeen desc, tenant asc."""

    return tuple(
        sorted(
            sightings,
            key=lambda s: (s.key, -s.last_seen_tick, s.source_tenant.value),
        )
    )


def build_alliance_snapshot_from_sightings(
    *,
    revision: int,
    members: Sequence[AllianceMemberState],
    sightings: Sequence[EntitySighting],
    ally_entity_ids: frozenset[str] | Sequence[str],
    now_tick: int,
    generated_at_ms: int = 0,
    leaderboard_aggression: Mapping[str, float] | None = None,
    historical_sighting_count: int | None = None,
    treasury_tenant: TenantId | None = None,
    freshness_window: FreshnessWindow | None = None,
) -> AllianceSnapshot:
    """Canonical snapshot entry (TS buildAllianceSnapshotFromSightings + staleness).

    ``generated_at_ms`` defaults to 0 (not wall clock) so the pure function is
    deterministic; callers pass wall-clock ms when display time matters.
    """

    from .threat import (  # lazy: avoid import cycle
        adjust_with_leaderboard_prior,
        project_threat_field,
    )

    _require_int("revision", revision, minimum=0)
    _require_int("now_tick", now_tick)
    if generated_at_ms is not None:
        _require_int("generated_at_ms", generated_at_ms, minimum=0)
    generated_ms = 0 if generated_at_ms is None else generated_at_ms
    if not isinstance(sightings, Sequence) or isinstance(sightings, (str, bytes)):
        raise TypeError("sightings must be a sequence of EntitySighting")
    if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
        raise TypeError("members must be a sequence of AllianceMemberState")

    if (
        isinstance(ally_entity_ids, (frozenset, set))
        or isinstance(ally_entity_ids, Sequence)
        and not isinstance(ally_entity_ids, (str, bytes))
    ):
        ally = frozenset(ally_entity_ids)
    else:
        raise TypeError("ally_entity_ids must be a set or sequence of strings")
    if not all(isinstance(item, str) for item in ally):
        raise TypeError("ally_entity_ids must contain only strings")

    for sighting in sightings:
        if not isinstance(sighting, EntitySighting):
            raise TypeError("sightings must contain only EntitySighting")
    filtered = [
        s
        for s in sightings
        if s.key not in ally and (s.entity_id is None or s.entity_id not in ally)
    ]

    ordered = _sorted_sightings(filtered)
    counts = compute_force_counts(
        ordered,
        now_tick,
        historical_count_override=historical_sighting_count,
    )
    threat = project_threat_field(ordered, now_tick, generated_at_ms=generated_ms)
    if leaderboard_aggression is not None and len(leaderboard_aggression) > 0:
        threat = adjust_with_leaderboard_prior(threat, ordered, leaderboard_aggression)

    member_map: dict[TenantId, AllianceMemberState] = {}
    for member in members:
        if not isinstance(member, AllianceMemberState):
            raise TypeError("members must contain only AllianceMemberState")
        member_map[member.tenant_id] = member

    window = freshness_window or FreshnessWindow()
    freshness = {s.key: classify_sighting_freshness(s, now_tick, window) for s in ordered}
    stale_keys = frozenset(
        key for key, level in freshness.items() if level is IntelFreshness.HISTORICAL
    )

    if ordered:
        tick_window = (min(s.first_seen_tick for s in ordered), now_tick)
    else:
        tick_window = (now_tick, now_tick)

    return AllianceSnapshot(
        revision=revision,
        tick_window=tick_window,
        generated_at_ms=generated_ms,
        members=member_map,
        sightings=ordered,
        ally_entity_ids=ally,
        threat=threat,
        counts=counts,
        treasury_tenant=treasury_tenant,
        freshness=freshness,
        stale_sighting_keys=stale_keys,
    )


def build_alliance_snapshot(
    *,
    revision: int,
    members: Sequence[AllianceMemberState],
    observations: Sequence[AllianceObservation],
    roster_ally_entity_ids: frozenset[str] | Sequence[str],
    now_tick: int,
    generated_at_ms: int = 0,
    leaderboard_aggression: Mapping[str, float] | None = None,
    default_evidence: EvidenceKind = EvidenceKind.CALIBRATION,
    treasury_tenant: TenantId | None = None,
    freshness_window: FreshnessWindow | None = None,
) -> AllianceSnapshot:
    """Build a snapshot from raw observations (TS buildAllianceSnapshot)."""

    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
        raise TypeError("observations must be a sequence of AllianceObservation")
    sightings = observations_to_sightings(observations, now_tick, default_evidence)
    raw_combat_count = sum(
        1
        for o in observations
        if not o.controlled and o.kind is SightingKind.UNIT and is_combat_unit(o.unit_type)
    )
    return build_alliance_snapshot_from_sightings(
        revision=revision,
        members=members,
        sightings=sightings,
        ally_entity_ids=roster_ally_entity_ids,
        now_tick=now_tick,
        generated_at_ms=generated_at_ms,
        leaderboard_aggression=leaderboard_aggression,
        historical_sighting_count=raw_combat_count,
        treasury_tenant=treasury_tenant,
        freshness_window=freshness_window,
    )


__all__ = [
    "AllianceForceCounts",
    "AllianceMemberState",
    "AllianceObservation",
    "AllianceSnapshot",
    "CONFIDENCE_FLOOR",
    "CORE_SPATIAL_GATE",
    "CORE_TAU",
    "CoreRef",
    "DEFAULT_FRESHNESS_WINDOW_TICKS",
    "DEFAULT_LIVE_WINDOW_TICKS",
    "EntitySighting",
    "EvidenceKind",
    "FreshnessWindow",
    "IntelFreshness",
    "MemberStatus",
    "OBSTACLE_TAU",
    "RECENT_UNIQUE_WINDOW",
    "RESOURCE_TAU",
    "SightingKind",
    "UNIT_SAME_TICK_GATE",
    "UNIT_TAU",
    "UnitType",
    "build_alliance_snapshot",
    "build_alliance_snapshot_from_sightings",
    "classify_sighting_freshness",
    "compute_force_counts",
    "confidence_at",
    "current_confidence",
    "current_visible_combat",
    "estimated_force",
    "historical_sighting_count",
    "is_combat_unit",
    "is_currently_visible",
    "merge_key",
    "merge_sightings",
    "normalize_sighting",
    "observations_to_sightings",
    "recent_unique_combat",
    "tau_for",
]
