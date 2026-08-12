"""W20 shared-intel fusion: deterministic fusion, freshness tiers, fail-closed.

Expected values are transcribed from the TS ``lib/alliance/shared-intel.ts``
semantics (read as the read-only oracle) and anchored field-for-field by the
golden parity suite (``test_golden_parity.py`` alliance_intel_basic).
"""

from __future__ import annotations

import math

import pytest

from arena_hero_agent.alliance.shared_intel import (
    DEFAULT_CONFIDENCE_FLOOR,
    DEFAULT_CONFIDENCE_TAU_TICKS,
    SharedIntelConfig,
    aggregate_alliance_intel,
    fuse_entity_sightings,
    resolve_shared_intel_config,
)
from arena_hero_agent.alliance.snapshot import (
    EntitySighting,
    EvidenceKind,
    IntelFreshness,
    SightingKind,
    UnitType,
)
from arena_hero_agent.domain import Coordinate, TenantId

T1 = TenantId("t1")

DEFAULT_POSITION = Coordinate(10, 10)
T2 = TenantId("t2")
T3 = TenantId("t3")
NOW = 100


def sighting(
    *,
    key: str,
    tenant: TenantId = T1,
    kind: SightingKind = SightingKind.UNIT,
    unit_type: UnitType | None = UnitType.VANGUARD,
    entity_id: str | None = None,
    owner_username: str | None = None,
    position: Coordinate | None = None,
    first_seen_tick: int | None = None,
    last_seen_tick: int = NOW,
    currently_visible: bool = True,
    confidence: float = 1.0,
    evidence: EvidenceKind = EvidenceKind.LIVE,
) -> EntitySighting:
    first_seen = last_seen_tick if first_seen_tick is None else first_seen_tick
    position = position if position is not None else DEFAULT_POSITION
    return EntitySighting(
        key=key,
        kind=kind,
        unit_type=unit_type,
        entity_id=entity_id,
        owner_username=owner_username,
        position=position,
        source_tenant=tenant,
        first_seen_tick=first_seen,
        last_seen_tick=last_seen_tick,
        currently_visible=currently_visible,
        confidence=confidence,
        evidence=evidence,
    )


def test_fusion_winner_is_newest_sighting() -> None:
    fresh = sighting(key="UNIT:e1", last_seen_tick=NOW, confidence=0.9)
    stale = sighting(
        key="UNIT:e1",
        tenant=T2,
        last_seen_tick=NOW - 5,
        currently_visible=False,
        confidence=0.7,
        evidence=EvidenceKind.CALIBRATION,
    )
    fused = fuse_entity_sightings([stale, fresh], NOW)
    assert len(fused) == 1
    winner = fused[0]
    assert winner.last_seen_tick == NOW
    assert winner.confidence == 0.9
    assert winner.source_tenants == ("t1", "t2")
    assert winner.age_ticks == 0
    assert winner.decayed_confidence == 0.9
    assert winner.freshness is IntelFreshness.LIVE


def test_fusion_sources_are_sorted_union() -> None:
    first = sighting(key="UNIT:e1", tenant=T2, last_seen_tick=NOW, confidence=0.5)
    second = sighting(key="UNIT:e1", tenant=T1, last_seen_tick=NOW, confidence=0.8)
    fused = fuse_entity_sightings([first, second], NOW)
    assert fused[0].source_tenants == ("t1", "t2")


def test_freshness_tiers_live_recent_historical() -> None:
    live = sighting(key="UNIT:live", last_seen_tick=NOW)
    recent = sighting(
        key="UNIT:recent",
        last_seen_tick=NOW - 5,
        currently_visible=False,
        evidence=EvidenceKind.CALIBRATION,
    )
    historical = sighting(
        key="UNIT:historical",
        last_seen_tick=NOW - 50,
        currently_visible=False,
        evidence=EvidenceKind.CALIBRATION,
    )
    fused = fuse_entity_sightings([live, recent, historical], NOW)
    by_key = {item.key: item for item in fused}
    assert by_key["UNIT:live"].freshness is IntelFreshness.LIVE
    assert by_key["UNIT:recent"].freshness is IntelFreshness.RECENT
    assert by_key["UNIT:historical"].freshness is IntelFreshness.HISTORICAL
    assert by_key["UNIT:historical"].decayed_confidence == DEFAULT_CONFIDENCE_FLOOR


def test_decayed_confidence_uses_tau_and_floor() -> None:
    sighting_item = sighting(
        key="UNIT:e1",
        last_seen_tick=NOW - 20,
        currently_visible=False,
        confidence=0.8,
        evidence=EvidenceKind.CALIBRATION,
    )
    fused = fuse_entity_sightings([sighting_item], NOW)
    expected = 0.8 * math.exp(-20 / DEFAULT_CONFIDENCE_TAU_TICKS)
    assert fused[0].decayed_confidence == pytest.approx(max(DEFAULT_CONFIDENCE_FLOOR, expected))


def test_custom_config_windows_change_classification() -> None:
    config = SharedIntelConfig(live_window_ticks=10, freshness_window_ticks=20)
    live = sighting(
        key="UNIT:e1",
        last_seen_tick=NOW - 5,
        currently_visible=True,
        evidence=EvidenceKind.LIVE,
    )
    fused = fuse_entity_sightings([live], NOW, config)
    assert fused[0].freshness is IntelFreshness.LIVE


def test_aggregate_filters_ally_ids_by_key_and_entity_id() -> None:
    ally_by_key = sighting(key="ally-key", entity_id="ally-key", unit_type=UnitType.RANGER)
    ally_by_id = sighting(
        key="other-key", entity_id="ally-id", unit_type=UnitType.RANGER, tenant=T2
    )
    enemy = sighting(key="UNIT:enemy", entity_id="enemy")
    view = aggregate_alliance_intel(
        sightings=[ally_by_key, ally_by_id, enemy],
        ally_entity_ids=["ally-key", "ally-id"],
        current_tick=NOW,
    )
    assert [item.key for item in view.historical_known] == ["UNIT:enemy"]
    assert view.counts.historical_enemy_units == 1


def test_aggregate_counts_by_freshness_tier_and_kind() -> None:
    live_unit = sighting(key="UNIT:a", entity_id="a")
    live_core = sighting(
        key="CORE:c", kind=SightingKind.CORE, entity_id="c", owner_username="owner-c"
    )
    recent_unit = sighting(
        key="UNIT:b",
        entity_id="b",
        last_seen_tick=NOW - 5,
        currently_visible=False,
        evidence=EvidenceKind.CALIBRATION,
    )
    historical_core = sighting(
        key="CORE:d",
        kind=SightingKind.CORE,
        entity_id="d",
        owner_username="owner-d",
        last_seen_tick=NOW - 50,
        currently_visible=False,
        evidence=EvidenceKind.CALIBRATION,
    )
    view = aggregate_alliance_intel(
        sightings=[live_unit, live_core, recent_unit, historical_core],
        current_tick=NOW,
    )
    assert view.counts.current_enemy_units == 1
    assert view.counts.current_enemy_cores == 1
    assert view.counts.recent_enemy_units == 2
    assert view.counts.recent_enemy_cores == 1
    assert view.counts.historical_enemy_units == 2
    assert view.counts.historical_enemy_cores == 2
    assert len(view.currently_visible) == 2
    assert len(view.recent_fused) == 3
    assert len(view.historical_known) == 4


def test_aggregate_empty_sightings_is_fail_closed() -> None:
    view = aggregate_alliance_intel(sightings=(), current_tick=NOW)
    assert view.currently_visible == ()
    assert view.recent_fused == ()
    assert view.historical_known == ()
    assert view.member_reports == ()
    assert view.counts.current_enemy_units == 0
    assert view.counts.current_enemy_cores == 0
    assert view.counts.recent_enemy_units == 0
    assert view.counts.recent_enemy_cores == 0
    assert view.counts.historical_enemy_units == 0
    assert view.counts.historical_enemy_cores == 0


def test_worker_sightings_are_counted_but_never_ally_boosted() -> None:
    worker = sighting(key="UNIT:w", unit_type=UnitType.WORKER, entity_id="w")
    view = aggregate_alliance_intel(sightings=[worker], current_tick=NOW)
    assert view.counts.current_enemy_units == 1


def test_resolve_config_sanitizes_invalid_values() -> None:
    config = resolve_shared_intel_config(
        {
            "live_window_ticks": -3,
            "freshness_window_ticks": 2,
            "confidence_tau_ticks": 0,
            "confidence_floor": 5,
        }
    )
    assert config.live_window_ticks == 0
    assert config.freshness_window_ticks == 2
    assert config.confidence_tau_ticks > 0
    assert config.confidence_floor == 1.0


def test_aggregate_rejects_non_entity_sighting() -> None:
    with pytest.raises(TypeError):
        aggregate_alliance_intel(sightings=[{"key": "x"}], current_tick=NOW)  # type: ignore
