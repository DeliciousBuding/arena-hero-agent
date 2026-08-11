"""P4-17 snapshot: cross-tenant merge, stale-data detection, deterministic output.

Expected values in the table-driven tests are transcribed from the TS
lib/alliance semantics (sightings.ts / counts.ts / shared-intel.ts) read as
the read-only oracle; the live TS-vs-Python comparison is delivered separately.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from arena_hero_agent.alliance.snapshot import (
    CONFIDENCE_FLOOR,
    AllianceObservation,
    EntitySighting,
    EvidenceKind,
    FreshnessWindow,
    IntelFreshness,
    SightingKind,
    UnitType,
    build_alliance_snapshot,
    build_alliance_snapshot_from_sightings,
    classify_sighting_freshness,
    compute_force_counts,
    confidence_at,
    current_confidence,
    merge_key,
    merge_sightings,
    normalize_sighting,
)
from arena_hero_agent.domain import Coordinate, TenantId

T1 = TenantId("t1")
T2 = TenantId("t2")
T3 = TenantId("t3")

NOW = 100
DEFAULT_POSITION = Coordinate(5, 5)


def observation(
    *,
    tenant: TenantId = T1,
    tick: int = NOW,
    kind: SightingKind = SightingKind.UNIT,
    entity_id: str | None = None,
    owner_username: str | None = None,
    unit_type: UnitType | None = UnitType.VANGUARD,
    controlled: bool = False,
    position: Coordinate = DEFAULT_POSITION,
    evidence: EvidenceKind = EvidenceKind.LIVE,
) -> AllianceObservation:
    return AllianceObservation(
        tenant_id=tenant,
        tick=tick,
        kind=kind,
        entity_id=entity_id,
        owner_username=owner_username,
        unit_type=unit_type,
        controlled=controlled,
        position=position,
        evidence=evidence,
    )


def sighting(
    *,
    key: str,
    kind: SightingKind = SightingKind.UNIT,
    unit_type: UnitType | None = UnitType.VANGUARD,
    entity_id: str | None = None,
    owner_username: str | None = None,
    position: Coordinate = DEFAULT_POSITION,
    source_tenant: TenantId = T1,
    first_seen_tick: int | None = None,
    last_seen_tick: int = NOW,
    currently_visible: bool = True,
    confidence: float = 1.0,
    evidence: EvidenceKind = EvidenceKind.LIVE,
) -> EntitySighting:
    first = first_seen_tick if first_seen_tick is not None else last_seen_tick
    return EntitySighting(
        key=key,
        kind=kind,
        unit_type=unit_type,
        entity_id=entity_id,
        owner_username=owner_username,
        position=position,
        source_tenant=source_tenant,
        first_seen_tick=first,
        last_seen_tick=last_seen_tick,
        currently_visible=currently_visible,
        confidence=confidence,
        evidence=evidence,
    )


# --- sightings.ts semantics ---


@pytest.mark.parametrize(
    ("age", "tau", "expected"),
    [
        (0, 6, 1.0),
        (6, 6, 0.36787944117144233),  # exp(-1); matches TS probe
        (100, 6, CONFIDENCE_FLOOR),  # decays to the floor, never zero
        (96, 96, 0.36787944117144233),
        (8, 96, 0.9200444146293233),
        (0, 96, 1.0),
        (1, 6, 0.8464817248906141),  # exp(-1/6)
    ],
)
def test_confidence_at_matches_ts(age: int, tau: float, expected: float) -> None:
    assert confidence_at(age, tau) == pytest.approx(expected, abs=1e-15)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (dict(kind=SightingKind.UNIT, entity_id="u1", position=Coordinate(0, 0)), "UNIT:u1"),
        (
            dict(kind=SightingKind.CORE, owner_username="enemy", position=Coordinate(0, 0)),
            "CORE:enemy",
        ),
        (dict(kind=SightingKind.UNIT, tick=5, position=Coordinate(1, 2)), "UNIT:t1:5:1,2"),
        (dict(kind=SightingKind.RESOURCE, position=Coordinate(1, 2)), "RESOURCE:t1:1,2"),
        (
            dict(kind=SightingKind.CORE, entity_id="core-a", position=Coordinate(0, 0)),
            "CORE:core-a",
        ),
    ],
)
def test_merge_key_rules(kwargs: dict, expected: str) -> None:
    assert merge_key(source_tenant=T1, **kwargs) == expected


def test_merge_sightings_dedupes_same_entity_across_tenants() -> None:
    merged = merge_sightings(
        (),
        [
            observation(
                tenant=T1,
                tick=90,
                entity_id="u1",
                position=Coordinate(1, 1),
                evidence=EvidenceKind.CALIBRATION,
            ),
            observation(
                tenant=T2,
                tick=95,
                entity_id="u1",
                position=Coordinate(2, 2),
                evidence=EvidenceKind.CALIBRATION,
            ),
        ],
        now_tick=NOW,
    )
    assert len(merged) == 1
    entry = merged[0]
    assert entry.key == "UNIT:u1"
    assert entry.first_seen_tick == 90  # preserved from the first sighting
    assert entry.last_seen_tick == 95
    assert entry.source_tenant == T1  # first contributor retained
    assert entry.position == Coordinate(2, 2)
    assert entry.currently_visible is False
    assert entry.confidence == pytest.approx(math.exp(-5 / 6), abs=1e-15)


def test_merge_sightings_idempotent() -> None:
    raws = [observation(tenant=T1, tick=NOW, entity_id="u1")]
    once = merge_sightings((), raws, now_tick=NOW)
    twice = merge_sightings(once, raws, now_tick=NOW)
    assert once == twice


def test_merge_sightings_keeps_visible_at_current_tick() -> None:
    merged = merge_sightings((), [observation(tenant=T1, tick=NOW, entity_id="u1")], now_tick=NOW)
    assert merged[0].currently_visible is True
    assert merged[0].confidence == 1.0


def test_leaderboard_evidence_does_not_downgrade_existing() -> None:
    existing = sighting(
        key="UNIT:u1", evidence=EvidenceKind.CALIBRATION, last_seen_tick=90, currently_visible=False
    )
    merged = normalize_sighting(
        observation(tenant=T1, tick=95, entity_id="u1", evidence=EvidenceKind.LEADERBOARD),
        existing,
        now_tick=NOW,
    )
    assert merged.evidence is EvidenceKind.CALIBRATION


def test_core_spatial_gate_splits_far_drift() -> None:
    existing = sighting(
        key="CORE:enemy",
        kind=SightingKind.CORE,
        owner_username="enemy",
        position=Coordinate(0, 0),
        first_seen_tick=10,
        last_seen_tick=10,
        currently_visible=False,
    )
    result = normalize_sighting(
        observation(
            tenant=T1,
            tick=20,
            kind=SightingKind.CORE,
            owner_username="enemy",
            position=Coordinate(10, 0),
        ),
        existing,
        now_tick=30,
    )
    assert result.key == "CORE:enemy:10,0"  # drift 10 > gate 8 splits into a new entity


def test_core_spatial_gate_merges_near_drift() -> None:
    existing = sighting(
        key="CORE:enemy",
        kind=SightingKind.CORE,
        owner_username="enemy",
        position=Coordinate(0, 0),
        first_seen_tick=10,
        last_seen_tick=10,
        currently_visible=False,
    )
    result = normalize_sighting(
        observation(
            tenant=T1,
            tick=20,
            kind=SightingKind.CORE,
            owner_username="enemy",
            position=Coordinate(5, 0),
        ),
        existing,
        now_tick=30,
    )
    assert result.key == "CORE:enemy"  # drift 5 <= gate 8 merges back
    assert result.last_seen_tick == 20
    assert result.position == Coordinate(5, 0)


def test_core_spatial_gate_reunites_split_history() -> None:
    split_entry = sighting(
        key="CORE:enemy:5,0",
        kind=SightingKind.CORE,
        owner_username="enemy",
        position=Coordinate(5, 0),
        first_seen_tick=20,
        last_seen_tick=20,
        currently_visible=False,
    )
    result = normalize_sighting(
        observation(
            tenant=T1,
            tick=30,
            kind=SightingKind.CORE,
            owner_username="enemy",
            position=Coordinate(0, 0),
        ),
        split_entry,
        now_tick=40,
    )
    assert result.key == "CORE:enemy"
    assert result.last_seen_tick == 30
    assert result.position == Coordinate(0, 0)


# --- freshness (stale-data detection) ---


@pytest.mark.parametrize(
    ("last_seen_tick", "currently_visible", "expected"),
    [
        (NOW, True, IntelFreshness.LIVE),  # visible at the current tick
        (NOW - 1, True, IntelFreshness.LIVE),  # visible, age 1 == live window
        (NOW - 2, True, IntelFreshness.RECENT),  # visible but past the live window
        (NOW - 1, False, IntelFreshness.RECENT),  # not visible: age 1 is not LIVE
        (NOW - 8, False, IntelFreshness.RECENT),  # boundary: age == freshness window
        (NOW - 9, False, IntelFreshness.HISTORICAL),  # boundary: stale past the window
        (NOW - 100, False, IntelFreshness.HISTORICAL),
    ],
)
def test_freshness_window_boundaries(
    last_seen_tick: int,
    currently_visible: bool,
    expected: IntelFreshness,
) -> None:
    entry = sighting(
        key="UNIT:u1",
        last_seen_tick=last_seen_tick,
        currently_visible=currently_visible,
        confidence=0.5,
    )
    assert classify_sighting_freshness(entry, NOW) is expected


def test_freshness_custom_window() -> None:
    window = FreshnessWindow(live_window_ticks=2, freshness_window_ticks=5)
    entry = sighting(key="UNIT:u1", last_seen_tick=NOW - 2, currently_visible=True, confidence=0.5)
    assert classify_sighting_freshness(entry, NOW, window) is IntelFreshness.LIVE
    entry = sighting(key="UNIT:u2", last_seen_tick=NOW - 5, currently_visible=False, confidence=0.5)
    assert classify_sighting_freshness(entry, NOW, window) is IntelFreshness.RECENT
    entry = sighting(key="UNIT:u3", last_seen_tick=NOW - 6, currently_visible=False, confidence=0.5)
    assert classify_sighting_freshness(entry, NOW, window) is IntelFreshness.HISTORICAL


def test_freshness_window_rejects_inverted_windows() -> None:
    with pytest.raises(ValueError):
        FreshnessWindow(live_window_ticks=5, freshness_window_ticks=2)


# --- counts.ts semantics ---


def test_compute_force_counts_four_ways() -> None:
    sightings_list = [
        sighting(
            key="UNIT:u1", unit_type=UnitType.VANGUARD, last_seen_tick=NOW, currently_visible=True
        ),
        sighting(
            key="UNIT:u2",
            unit_type=UnitType.VANGUARD,
            last_seen_tick=NOW - 2,
            currently_visible=False,
        ),
        sighting(
            key="UNIT:w1", unit_type=UnitType.WORKER, last_seen_tick=NOW, currently_visible=True
        ),
        sighting(
            key="UNIT:u3",
            unit_type=UnitType.RANGER,
            last_seen_tick=NOW - 50,
            currently_visible=False,
        ),
    ]
    counts = compute_force_counts(sightings_list, NOW)
    assert counts.current_visible_combat == 1  # only u1
    assert counts.recent_unique_combat == 3  # u1, u2, u3 within the 300-tick window
    assert counts.historical_sighting_count == 3  # combat rows, worker excluded
    assert counts.estimated_force == pytest.approx(
        1.0 + math.exp(-2 / 6) + CONFIDENCE_FLOOR,
        abs=1e-15,
    )


def test_compute_force_counts_historical_override() -> None:
    counts = compute_force_counts(
        [sighting(key="UNIT:u1")],
        NOW,
        historical_count_override=7,
    )
    assert counts.historical_sighting_count == 7


def test_current_confidence_visible_forces_one() -> None:
    entry = sighting(key="UNIT:u1", last_seen_tick=NOW - 10, currently_visible=True, confidence=0.2)
    assert current_confidence(entry, NOW) == 1.0


# --- snapshot build ---


def test_snapshot_merges_multi_tenant_observations() -> None:
    snapshot = build_alliance_snapshot(
        revision=3,
        members=(),
        observations=[
            observation(
                tenant=T1,
                tick=90,
                entity_id="u1",
                position=Coordinate(1, 1),
                evidence=EvidenceKind.CALIBRATION,
            ),
            observation(tenant=T2, tick=NOW, entity_id="u1", position=Coordinate(2, 2)),
            observation(tenant=T3, tick=NOW, entity_id="u2", position=Coordinate(8, 8)),
        ],
        roster_ally_entity_ids=(),
        now_tick=NOW,
        generated_at_ms=1234,
    )
    assert snapshot.revision == 3
    assert snapshot.generated_at_ms == 1234
    assert [s.key for s in snapshot.sightings] == ["UNIT:u1", "UNIT:u2"]  # key asc
    by_key = {s.key: s for s in snapshot.sightings}
    assert by_key["UNIT:u1"].source_tenant == T1
    assert by_key["UNIT:u1"].last_seen_tick == NOW
    assert by_key["UNIT:u1"].first_seen_tick == 90
    assert snapshot.tick_window == (90, NOW)
    assert snapshot.counts.current_visible_combat == 2


def test_snapshot_stale_marking_window_boundary() -> None:
    snapshot = build_alliance_snapshot(
        revision=1,
        members=(),
        observations=[
            observation(tenant=T1, tick=NOW, entity_id="u-live"),
            observation(
                tenant=T1, tick=NOW - 5, entity_id="u-recent", evidence=EvidenceKind.CALIBRATION
            ),
            observation(
                tenant=T1, tick=NOW - 8, entity_id="u-boundary", evidence=EvidenceKind.CALIBRATION
            ),
            observation(
                tenant=T1, tick=NOW - 9, entity_id="u-stale", evidence=EvidenceKind.CALIBRATION
            ),
        ],
        roster_ally_entity_ids=(),
        now_tick=NOW,
        generated_at_ms=0,
    )
    assert dict(snapshot.freshness) == {
        "UNIT:u-live": IntelFreshness.LIVE,
        "UNIT:u-recent": IntelFreshness.RECENT,
        "UNIT:u-boundary": IntelFreshness.RECENT,
        "UNIT:u-stale": IntelFreshness.HISTORICAL,
    }
    assert snapshot.stale_sighting_keys == frozenset({"UNIT:u-stale"})
    # Stale entries never count as currently visible combat.
    assert snapshot.counts.current_visible_combat == 1
    assert snapshot.counts.recent_unique_combat == 4  # TS counts.ts 300-tick window


def test_snapshot_ally_filter_removes_friendly_entities() -> None:
    snapshot = build_alliance_snapshot_from_sightings(
        revision=1,
        members=(),
        sightings=[
            sighting(key="UNIT:ally-1", entity_id="ally-1"),
            sighting(key="UNIT:enemy-1", entity_id="enemy-1"),
            sighting(key="CORE:ally-core", entity_id="ally-core"),
        ],
        ally_entity_ids=frozenset({"ally-1", "ally-core"}),
        now_tick=NOW,
        generated_at_ms=0,
    )
    assert [s.key for s in snapshot.sightings] == ["UNIT:enemy-1"]
    assert snapshot.counts.current_visible_combat == 1


def test_snapshot_sightings_deterministic_sort() -> None:
    snapshot = build_alliance_snapshot_from_sightings(
        revision=1,
        members=(),
        sightings=[
            sighting(key="UNIT:z", last_seen_tick=90, currently_visible=False),
            sighting(key="UNIT:a", last_seen_tick=NOW),
            sighting(key="UNIT:a", last_seen_tick=95, currently_visible=False),
            sighting(key="UNIT:m", source_tenant=T2, last_seen_tick=95, currently_visible=False),
            sighting(key="UNIT:m", source_tenant=T1, last_seen_tick=95, currently_visible=False),
        ],
        ally_entity_ids=(),
        now_tick=NOW,
        generated_at_ms=0,
    )
    # key asc, then lastSeenTick desc, then sourceTenant asc (TS snapshot.ts)
    assert [s.key for s in snapshot.sightings] == ["UNIT:a", "UNIT:a", "UNIT:m", "UNIT:m", "UNIT:z"]
    unit_a = [s for s in snapshot.sightings if s.key == "UNIT:a"]
    assert [s.last_seen_tick for s in unit_a] == [NOW, 95]
    unit_m = [s for s in snapshot.sightings if s.key == "UNIT:m"]
    assert [s.source_tenant for s in unit_m] == [T1, T2]


def test_snapshot_deterministic_same_input() -> None:
    inputs: dict[str, Any] = dict(
        revision=2,
        members=(),
        observations=[
            observation(tenant=T1, tick=NOW, entity_id="u1"),
            observation(tenant=T2, tick=95, entity_id="u2", evidence=EvidenceKind.CALIBRATION),
        ],
        roster_ally_entity_ids=(),
        now_tick=NOW,
        generated_at_ms=0,
    )
    first = build_alliance_snapshot(**inputs)
    second = build_alliance_snapshot(**inputs)
    assert first == second
    assert first is not second


def test_snapshot_empty_market() -> None:
    snapshot = build_alliance_snapshot_from_sightings(
        revision=0,
        members=(),
        sightings=(),
        ally_entity_ids=(),
        now_tick=NOW,
        generated_at_ms=0,
    )
    assert snapshot.sightings == ()
    assert snapshot.counts.current_visible_combat == 0
    assert snapshot.counts.estimated_force == 0.0
    assert snapshot.tick_window == (NOW, NOW)
    assert snapshot.stale_sighting_keys == frozenset()


def test_snapshot_members_are_immutable_mapping() -> None:
    from types import MappingProxyType

    snapshot = build_alliance_snapshot_from_sightings(
        revision=0,
        members=(),
        sightings=(),
        ally_entity_ids=(),
        now_tick=NOW,
        generated_at_ms=0,
    )
    assert isinstance(snapshot.members, MappingProxyType)
    assert isinstance(snapshot.freshness, MappingProxyType)
    assert isinstance(snapshot.sightings, tuple)


# --- fail-closed input rejection ---


def test_observation_rejects_malformed() -> None:
    with pytest.raises(TypeError):
        AllianceObservation(tenant_id="t1", tick=1, kind=SightingKind.UNIT)  # type: ignore
    with pytest.raises(ValueError):
        AllianceObservation(tenant_id=T1, tick=-1, kind=SightingKind.UNIT)
    with pytest.raises(TypeError):
        AllianceObservation(tenant_id=T1, tick=True, kind=SightingKind.UNIT)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AllianceObservation(tenant_id=T1, tick=1, kind="UNIT")  # type: ignore
    with pytest.raises(TypeError):
        AllianceObservation(
            tenant_id=T1,
            tick=1,
            kind=SightingKind.UNIT,
            position=(1, 2),  # type: ignore
        )
    with pytest.raises(TypeError):
        AllianceObservation(tenant_id=T1, tick=1, kind=SightingKind.UNIT, controlled="yes")  # type: ignore


def test_sighting_rejects_non_coordinate_position() -> None:
    with pytest.raises(TypeError):
        EntitySighting(
            key="UNIT:u1",
            kind=SightingKind.UNIT,
            unit_type=UnitType.VANGUARD,
            entity_id=None,
            owner_username=None,
            position=(1, 2),  # type: ignore
            source_tenant=T1,
            first_seen_tick=1,
            last_seen_tick=1,
            currently_visible=True,
            confidence=1.0,
            evidence=EvidenceKind.LIVE,
        )


def test_sighting_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValueError):
        sighting(key="UNIT:u1", confidence=1.5)


def test_sighting_rejects_inverted_window() -> None:
    with pytest.raises(ValueError):
        sighting(key="UNIT:u1", first_seen_tick=10, last_seen_tick=5)


def test_snapshot_rejects_bad_ally_ids() -> None:
    with pytest.raises(TypeError):
        build_alliance_snapshot_from_sightings(
            revision=0,
            members=(),
            sightings=(),
            ally_entity_ids=[1, 2],  # type: ignore
            now_tick=NOW,
        )


def test_snapshot_rejects_non_sighting_entries() -> None:
    with pytest.raises(TypeError):
        build_alliance_snapshot_from_sightings(
            revision=0,
            members=(),
            sightings=[("not", "a", "sighting")],  # type: ignore
            ally_entity_ids=(),
            now_tick=NOW,
        )


def test_snapshot_unknown_sighting_kind_rejected() -> None:
    with pytest.raises(ValueError):
        SightingKind("BOGUS")
