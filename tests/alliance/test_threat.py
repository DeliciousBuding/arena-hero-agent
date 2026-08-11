"""P4-17 threat field + summaries: deterministic, stale-safe, TS-aligned."""

from __future__ import annotations

import math

import pytest

from arena_hero_agent.alliance.snapshot import (
    AllianceMemberState,
    CoreRef,
    EntitySighting,
    EvidenceKind,
    IntelFreshness,
    MemberStatus,
    SightingKind,
    UnitType,
    build_alliance_snapshot_from_sightings,
    classify_sighting_freshness,
)
from arena_hero_agent.alliance.threat import (
    TenantThreatSummary,
    ThreatDirection,
    ThreatField,
    ThreatSummaryConfig,
    adjust_with_leaderboard_prior,
    build_threat_summaries_from_snapshot,
    project_threat_field,
    proximity_weight,
    resolve_threat_summary_config,
    threat_direction,
)
from arena_hero_agent.domain import Coordinate, TenantId

T1 = TenantId("t1")
T2 = TenantId("t2")
NOW = 100
DEFAULT_POSITION = Coordinate(5, 5)
DEFAULT_CORE_POSITION = Coordinate(0, 0)


def sighting(
    *,
    key: str,
    kind: SightingKind = SightingKind.UNIT,
    unit_type: UnitType | None = UnitType.VANGUARD,
    position: Coordinate = DEFAULT_POSITION,
    source_tenant: TenantId = T1,
    last_seen_tick: int = NOW,
    first_seen_tick: int | None = None,
    currently_visible: bool = True,
    confidence: float = 1.0,
    owner_username: str | None = None,
) -> EntitySighting:
    first = first_seen_tick if first_seen_tick is not None else last_seen_tick
    return EntitySighting(
        key=key,
        kind=kind,
        unit_type=unit_type,
        entity_id=None,
        owner_username=owner_username,
        position=position,
        source_tenant=source_tenant,
        first_seen_tick=first,
        last_seen_tick=last_seen_tick,
        currently_visible=currently_visible,
        confidence=confidence,
        evidence=EvidenceKind.LIVE,
    )


def member(
    *,
    tenant: TenantId = T1,
    core_position: Coordinate | None = DEFAULT_CORE_POSITION,
    tick: int = NOW,
) -> AllianceMemberState:
    core = (
        None
        if core_position is None
        else CoreRef(id="core-1", position=core_position, hp=100, shield=0, moving=False)
    )
    return AllianceMemberState(
        tenant_id=tenant,
        tick=tick,
        observed_at_ms=0,
        core=core,
        resources=10,
        resource_capacity=100,
        population=5,
        workers=3,
        vanguards=1,
        rangers=1,
        carried_resources=0,
        active_fleet_ids=(),
        local_threat=0.0,
        local_harvest_rate=1.0,
        status=MemberStatus.READY,
    )


# --- threat-field.ts semantics ---


@pytest.mark.parametrize(
    ("distance", "expected"),
    [
        (0, 1.0),
        (1, 0.5),
        (2, 1 / 3),
        (11, 1 / 12),
        (12, 1 / 13),
    ],
)
def test_proximity_weight(distance: int, expected: float) -> None:
    assert proximity_weight(distance) == pytest.approx(expected)


def test_project_visible_unit_centers_direct_combat() -> None:
    field = project_threat_field(
        [sighting(key="UNIT:u1", position=Coordinate(5, 5))],
        NOW,
        generated_at_ms=1234,
    )
    assert isinstance(field, ThreatField)
    assert field.generated_at_ms == 1234
    assert field.max_direct is not None
    assert field.max_direct.position == Coordinate(5, 5)
    assert field.max_direct.direct_combat == pytest.approx(1.0)
    assert len(field.cells) == 313  # full 12-radius Manhattan diamond
    neighbor = field.cells["6,5"]
    assert neighbor.direct_combat == pytest.approx(0.5)
    assert neighbor.uncertainty == pytest.approx(0.0)  # visible => confidence 1
    assert field.estimated_combat_force == pytest.approx(1.0)
    assert field.tick_window == (NOW, NOW)


def test_project_worker_never_projects_threat() -> None:
    field = project_threat_field(
        [sighting(key="UNIT:w1", unit_type=UnitType.WORKER, position=Coordinate(5, 5))],
        NOW,
    )
    assert field.cells == {}
    assert field.max_direct is None
    assert field.estimated_combat_force == pytest.approx(0.0)


def test_project_remembered_unit_uses_projected_combat() -> None:
    remembered = sighting(
        key="UNIT:u1",
        position=Coordinate(5, 5),
        last_seen_tick=NOW - 5,
        currently_visible=False,
        confidence=0.5,
    )
    field = project_threat_field([remembered], NOW)
    center = field.cells["5,5"]
    # projectThreatField recomputes currentConfidence (decayed by age 5).
    expected = math.exp(-5 / 6)
    assert center.projected_combat == pytest.approx(expected)
    assert center.direct_combat == pytest.approx(0.0)
    # TS code picks the first cell even when directCombat is 0 everywhere.
    assert field.max_direct is not None
    assert field.max_direct.direct_combat == pytest.approx(0.0)
    assert field.estimated_combat_force == pytest.approx(expected)


def test_project_core_raid_radius_and_weight() -> None:
    core = sighting(
        key="CORE:enemy",
        kind=SightingKind.CORE,
        position=Coordinate(0, 0),
        owner_username="enemy",
        last_seen_tick=NOW - 5,
        currently_visible=False,
        confidence=0.8,
    )
    field = project_threat_field([core], NOW)
    assert len(field.cells) == 1201  # full 24-radius Manhattan diamond
    # projectThreatField recomputes currentConfidence (decayed by age 5).
    expected = math.exp(-5 / 96)
    assert field.cells["0,0"].core_raid == pytest.approx(expected)
    assert field.cells["1,0"].core_raid == pytest.approx(expected / 2)
    # TS code picks the first cell even when directCombat is 0 everywhere.
    assert field.max_direct is not None
    assert field.max_direct.direct_combat == pytest.approx(0.0)


def test_project_stale_never_amplifies_threat() -> None:
    stale = sighting(
        key="UNIT:u1",
        position=Coordinate(5, 5),
        last_seen_tick=NOW - 9,
        currently_visible=False,
        confidence=0.3,
    )
    assert classify_sighting_freshness(stale, NOW) is IntelFreshness.HISTORICAL
    field = project_threat_field([stale], NOW)
    assert field.cells == {}
    assert field.max_direct is None
    assert field.estimated_combat_force == pytest.approx(0.0)
    # include_historical reproduces the exact TS projection (decayed weight).
    ts_like = project_threat_field([stale], NOW, include_historical=True)
    assert "5,5" in ts_like.cells
    expected = math.exp(-9 / 6)  # recomputed decayed confidence
    assert ts_like.cells["5,5"].projected_combat == pytest.approx(expected)
    assert ts_like.estimated_combat_force == pytest.approx(expected)


def test_project_field_deterministic() -> None:
    entries = [
        sighting(key="UNIT:u1", position=Coordinate(5, 5)),
        sighting(key="UNIT:u2", position=Coordinate(20, 3)),
        sighting(
            key="CORE:enemy",
            kind=SightingKind.CORE,
            position=Coordinate(-10, -10),
            owner_username="enemy",
        ),
    ]
    first = project_threat_field(entries, NOW, generated_at_ms=0)
    second = project_threat_field(list(reversed(entries)), NOW, generated_at_ms=0)
    assert first.cells == second.cells
    assert first.max_direct == second.max_direct


def test_project_empty_field() -> None:
    field = project_threat_field([], NOW)
    assert field.cells == {}
    assert field.max_direct is None
    assert field.estimated_combat_force == pytest.approx(0.0)
    assert field.tick_window == (NOW, NOW)


def test_leaderboard_prior_idempotent_and_weak() -> None:
    core = sighting(
        key="CORE:enemy",
        kind=SightingKind.CORE,
        position=Coordinate(0, 0),
        owner_username="enemy",
    )
    field = project_threat_field([core], NOW)
    assert adjust_with_leaderboard_prior(field, [core], {}) is field  # empty prior: unchanged
    unchanged = adjust_with_leaderboard_prior(field, [core], {"enemy": 0.0})
    assert unchanged.cells == field.cells

    boosted = adjust_with_leaderboard_prior(field, [core], {"enemy": 0.8})
    assert boosted.cells["0,0"].core_raid == pytest.approx(
        1.0 + 0.8 * 0.3,  # existing coreRaid 1 + prior weight 0.24
    )
    assert boosted.cells["1,0"].core_raid == pytest.approx((1.0 + 0.24) / 2)
    assert boosted.cells["0,0"].uncertainty == pytest.approx(0.2)  # 1 - prior


# --- threat-summary.ts semantics ---


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (Coordinate(0, 0), ThreatDirection.N),
        (Coordinate(0, 5), ThreatDirection.N),
        (Coordinate(0, -5), ThreatDirection.S),
        (Coordinate(5, 0), ThreatDirection.E),
        (Coordinate(-5, 0), ThreatDirection.W),
        (Coordinate(5, 5), ThreatDirection.NE),
        (Coordinate(5, -5), ThreatDirection.SE),
        (Coordinate(-5, -5), ThreatDirection.SW),
        (Coordinate(-5, 5), ThreatDirection.NW),
    ],
)
def test_threat_direction(target: Coordinate, expected: ThreatDirection) -> None:
    assert threat_direction(Coordinate(0, 0), target) is expected


def test_summary_empty_when_core_missing() -> None:
    summaries = build_threat_summaries_from_snapshot(
        build_alliance_snapshot_from_sightings(
            revision=1,
            members=(member(core_position=None),),
            sightings=(),
            ally_entity_ids=(),
            now_tick=NOW,
        )
    )
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.core_position is None
    assert summary.total_score == pytest.approx(0.0)
    assert all(sector.score == 0.0 for sector in summary.sectors)
    assert summary.high_directions == ()
    assert summary.multi_direction_pressure is False


def _summary_fixture() -> TenantThreatSummary:
    """One tenant core at (0,0) with E/N units and a S core (TS formulas)."""

    snapshot = build_alliance_snapshot_from_sightings(
        revision=1,
        members=(member(core_position=Coordinate(0, 0)),),
        sightings=[
            sighting(key="UNIT:u1", position=Coordinate(5, 0), confidence=1.0),
            sighting(key="UNIT:u2", position=Coordinate(0, 5), confidence=1.0),
            sighting(
                key="CORE:c1",
                kind=SightingKind.CORE,
                position=Coordinate(0, -10),
                owner_username="enemy",
                confidence=0.8,
            ),
        ],
        ally_entity_ids=(),
        now_tick=NOW,
        generated_at_ms=0,
    )
    return build_threat_summaries_from_snapshot(snapshot)[0]


def test_summary_sector_scores_and_counts() -> None:
    summary = _summary_fixture()
    assert summary.tenant_id == T1
    assert summary.core_position == Coordinate(0, 0)
    by_direction = {sector.direction: sector for sector in summary.sectors}
    east = by_direction[ThreatDirection.E]
    north = by_direction[ThreatDirection.N]
    south = by_direction[ThreatDirection.S]
    # unitWeight 1, distance 5: 1/(1 + 5/16) rounded to 1e-6
    assert east.score == pytest.approx(0.761905, abs=1e-6)
    assert east.entity_count == 1
    assert east.nearest_distance == 5
    assert east.entity_keys == ("UNIT:u1",)
    assert north.score == pytest.approx(0.761905, abs=1e-6)
    # coreWeight 4, confidence 0.8, distance 10: 3.2/(1 + 10/16)
    assert south.score == pytest.approx(1.969231, abs=1e-6)
    assert south.entity_keys == ("CORE:c1",)
    assert summary.high_directions == (ThreatDirection.N, ThreatDirection.E, ThreatDirection.S)
    assert summary.multi_direction_pressure is True  # N, E, S are not all adjacent
    assert summary.total_score == pytest.approx(3.493041, abs=1e-6)


def test_summary_deterministic_same_snapshot() -> None:
    snapshot = build_alliance_snapshot_from_sightings(
        revision=1,
        members=(member(),),
        sightings=[
            sighting(key="UNIT:u1", position=Coordinate(5, 0)),
            sighting(key="UNIT:u2", position=Coordinate(0, 5)),
        ],
        ally_entity_ids=(),
        now_tick=NOW,
        generated_at_ms=0,
    )
    first = build_threat_summaries_from_snapshot(snapshot)
    second = build_threat_summaries_from_snapshot(snapshot)
    assert first == second
    assert first is not second


def test_summary_members_sorted_by_tenant() -> None:
    snapshot = build_alliance_snapshot_from_sightings(
        revision=1,
        members=(member(tenant=T2, core_position=Coordinate(3, 3)), member(tenant=T1)),
        sightings=(),
        ally_entity_ids=(),
        now_tick=NOW,
    )
    summaries = build_threat_summaries_from_snapshot(snapshot)
    assert [s.tenant_id for s in summaries] == [T1, T2]


def test_summary_excludes_stale_sightings() -> None:
    stale = sighting(
        key="UNIT:old",
        position=Coordinate(10, 0),
        last_seen_tick=NOW - 9,
        currently_visible=False,
        confidence=0.9,
    )
    snapshot = build_alliance_snapshot_from_sightings(
        revision=1,
        members=(member(core_position=Coordinate(0, 0)),),
        sightings=[stale],
        ally_entity_ids=(),
        now_tick=NOW,
        generated_at_ms=0,
    )
    assert snapshot.stale_sighting_keys == frozenset({"UNIT:old"})
    summary = build_threat_summaries_from_snapshot(snapshot)[0]
    assert summary.total_score == pytest.approx(0.0)
    assert all(sector.entity_count == 0 for sector in summary.sectors)


def test_summary_non_adjacent_high_pressure_adjacent_only() -> None:
    # N + NE are adjacent -> no multi-direction pressure.
    snapshot = build_alliance_snapshot_from_sightings(
        revision=1,
        members=(member(core_position=Coordinate(0, 0)),),
        sightings=[
            sighting(key="UNIT:a", position=Coordinate(0, 10)),
            sighting(key="UNIT:b", position=Coordinate(5, 5)),
        ],
        ally_entity_ids=(),
        now_tick=NOW,
        generated_at_ms=0,
    )
    summary = build_threat_summaries_from_snapshot(snapshot)[0]
    assert summary.high_directions == (ThreatDirection.N, ThreatDirection.NE)
    assert summary.multi_direction_pressure is False


def test_summary_high_score_threshold_boundary() -> None:
    # Score exactly at the threshold is high (>= 0.55).
    # distance 13, confidence 0.996875 => score = 0.996875/(1 + 13/16) = 0.55.
    snapshot = build_alliance_snapshot_from_sightings(
        revision=1,
        members=(member(core_position=Coordinate(0, 0)),),
        sightings=[sighting(key="UNIT:b", position=Coordinate(13, 0), confidence=0.996875)],
        ally_entity_ids=(),
        now_tick=NOW,
        generated_at_ms=0,
    )
    summary = build_threat_summaries_from_snapshot(snapshot)[0]
    east = next(s for s in summary.sectors if s.direction is ThreatDirection.E)
    assert east.score == pytest.approx(0.55, abs=1e-6)
    assert ThreatDirection.E in summary.high_directions


def test_resolve_threat_summary_config_defaults_and_fallbacks() -> None:
    config = resolve_threat_summary_config()
    assert config == ThreatSummaryConfig()
    partial = resolve_threat_summary_config({"core_weight": 8.0})
    assert partial.core_weight == 8.0
    assert partial.unit_weight == 1.0
    # TS finitePositive fallback: invalid values reset to defaults.
    invalid = resolve_threat_summary_config({"distance_scale": 0.0, "max_distance": -3})
    assert invalid.distance_scale == 16.0
    assert invalid.max_distance == 96.0
    with pytest.raises(TypeError):
        resolve_threat_summary_config({"core_weight": "heavy"})


def test_project_threat_field_matches_snapshot_integration() -> None:
    """Snapshot carries a project_threat_field result over the same sightings."""

    from arena_hero_agent.alliance.threat import project_threat_field

    entries = [sighting(key="UNIT:u1", position=Coordinate(3, 4))]
    snapshot = build_alliance_snapshot_from_sightings(
        revision=1,
        members=(member(),),
        sightings=entries,
        ally_entity_ids=(),
        now_tick=NOW,
        generated_at_ms=7,
    )
    assert snapshot.threat == project_threat_field(entries, NOW, generated_at_ms=7)
    assert snapshot.threat.max_direct is not None
    assert snapshot.threat.max_direct.position == Coordinate(3, 4)


def test_summary_rounding_is_half_away_from_zero() -> None:
    # Score x.xxxxxx5 boundaries round up like JS Math.round (not banker's).
    snapshot = build_alliance_snapshot_from_sightings(
        revision=1,
        members=(member(core_position=Coordinate(0, 0)),),
        sightings=[sighting(key="UNIT:u1", position=Coordinate(16, 0), confidence=1.0)],
        ally_entity_ids=(),
        now_tick=NOW,
        generated_at_ms=0,
    )
    summary = build_threat_summaries_from_snapshot(snapshot)[0]
    east = next(s for s in summary.sectors if s.direction is ThreatDirection.E)
    # distance 16 => score = 1/(1+1) = 0.5 exactly; JS rounds 0.500000 -> 0.5
    assert east.score == pytest.approx(0.5, abs=1e-12)
