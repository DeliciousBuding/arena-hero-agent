"""Golden parity: Python projection cores vs TS oracle outputs (P5-4).

Every fixture under ``fixtures/`` pairs a synthetic runtime-artifact input
(the same shape the TypeScript oracle consumes) with a ``.golden.json`` output
produced by running the actual legacy oracle with Node (see
``tools/regenerate_goldens.py``). Each parametrized case must classify MATCH;
documented divergences live in :data:`ALLOWED_DIFFERENCES` (conftest) and stay
visible. Any mismatch is an UNKNOWN and fails the suite.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from arena_hero_agent.alliance.shared_intel import aggregate_alliance_intel
from arena_hero_agent.alliance.snapshot import (
    AllianceMemberState,
    CoreRef,
    EntitySighting,
    EvidenceKind,
    MemberStatus,
    SightingKind,
    UnitType,
)
from arena_hero_agent.command_center.projections import (
    aggregate_alignment,
    aggregate_allocation_effectiveness,
    aggregate_decision_audit,
    aggregate_decision_trend,
    aggregate_human_conflict,
    aggregate_map_lod,
    aggregate_mine_utilization,
    aggregate_mine_utilization_trend,
    aggregate_shop_history,
    aggregate_worker_liveness,
    assign_alliance_mining,
    build_alliance_cluster_view,
    build_alliance_defense_payload,
    build_decision_input,
    build_enemy_core_states,
    build_leaderboard_payload,
    compute_exploration_stats,
    enrich_consensus_mining,
    load_alliance_advice,
    load_alliance_intel,
    load_arbitrations,
    load_exploration,
    load_human_audit,
    load_lifecycle_audit,
    load_survey,
    load_survey_mine,
    merge_audit_trails,
    normalize_audit_trails,
    normalize_products,
    should_append,
    snapshot_signature,
)
from arena_hero_agent.command_center.projections._common import num
from arena_hero_agent.command_center.projections.alliance_snapshot import (
    _intel_payload,
    build_alliance_snapshot_payload,
)
from arena_hero_agent.command_center.projections.mine_patterns import (
    compute_absent_stats,
    compute_dead_mines,
    compute_prediction_accuracy,
    compute_refill_predictions,
    compute_refill_predictions_from_absences,
)
from arena_hero_agent.domain import Coordinate, TenantId

from .conftest import assert_matches, load_fixture, load_golden

NOW_MS = 1_752_000_000_000


def _parse_lines(raw_lines: list[object]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in raw_lines:
        if not isinstance(line, str):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _sighting_from_fixture(item: dict[str, Any]) -> EntitySighting:
    return EntitySighting(
        key=str(item["key"]),
        kind=SightingKind(str(item["kind"])),
        unit_type=UnitType(str(item["unitType"])) if item.get("unitType") else None,
        entity_id=item.get("entityId"),
        owner_username=item.get("ownerUsername"),
        position=Coordinate(int(item["position"][0]), int(item["position"][1])),
        source_tenant=TenantId(str(item["sourceTenant"])),
        first_seen_tick=int(item["firstSeenTick"]),
        last_seen_tick=int(item["lastSeenTick"]),
        currently_visible=bool(item["currentlyVisible"]),
        confidence=float(item["confidence"]),
        evidence=EvidenceKind(str(item["evidence"])),
    )


def _member_from_fixture(item: dict[str, Any]) -> AllianceMemberState:
    raw_core = item["core"]
    core = (
        CoreRef(
            id=str(raw_core["id"]),
            position=Coordinate(int(raw_core["position"][0]), int(raw_core["position"][1])),
            hp=int(raw_core["hp"]),
            shield=int(raw_core["shield"]),
            moving=bool(raw_core["moving"]),
        )
        if raw_core is not None
        else None
    )
    return AllianceMemberState(
        tenant_id=TenantId(str(item["tenantId"])),
        tick=int(item["tick"]),
        observed_at_ms=int(item["observedAtMs"]),
        core=core,
        resources=int(item["resources"]),
        resource_capacity=int(item["resourceCapacity"]),
        population=int(item["population"]),
        workers=int(item["workers"]),
        vanguards=int(item["vanguards"]),
        rangers=int(item["rangers"]),
        carried_resources=int(item["carriedResources"]),
        active_fleet_ids=tuple(str(v) for v in item.get("activeFleetIds") or ()),
        local_threat=float(item.get("localThreat", 0)),
        local_harvest_rate=float(item.get("localHarvestRate", 0)),
        status=MemberStatus(str(item["status"])),
    )


def _run_python(name: str, fixture: dict[str, Any]) -> object:
    if name == "alliance_intel_basic":
        return _intel_payload(
            aggregate_alliance_intel(
                sightings=[_sighting_from_fixture(item) for item in fixture["sightings"]],
                ally_entity_ids=list(fixture.get("allyEntityIds") or ()),
                current_tick=int(fixture["currentTick"]),
            )
        )
    if name == "alliance_snapshot_basic":
        treasury = fixture.get("treasuryTenant")
        return build_alliance_snapshot_payload(
            revision=int(fixture.get("revision", 1)),
            members=[_member_from_fixture(item) for item in fixture["members"]],
            sightings=[_sighting_from_fixture(item) for item in fixture["sightings"]],
            ally_entity_ids=list(fixture.get("allyEntityIds") or ()),
            now_tick=int(fixture["currentTick"]),
            generated_at_ms=int(fixture.get("generatedAtMs", 0)),
            leaderboard_aggression=dict(fixture.get("leaderboardAggression") or {}),
            treasury_tenant=TenantId(str(treasury)) if treasury else None,
        )
    if name == "alliance_cluster_basic":
        return build_alliance_cluster_view(
            [dict(item) for item in fixture["input"]], int(fixture["nowMs"])
        )
    if name == "alliance_defense_basic":
        return build_alliance_defense_payload(
            members=dict(fixture["members"]),
            sightings=[dict(item) for item in fixture["sightings"]],
            threat_summaries=[dict(item) for item in fixture["threatSummaries"]],
            now_ms=int(fixture["nowMs"]),
        )
    if name == "alliance_mining_basic":
        return assign_alliance_mining(
            {str(k): (tuple(v) if v is not None else None) for k, v in fixture["cores"].items()},
            {str(k): v for k, v in fixture["workers"].items()},
            {str(k): [dict(item) for item in v] for k, v in fixture["candidatesByTenant"].items()},
            {str(k): [str(t) for t in v] for k, v in fixture["observersByCell"].items()},
            set(fixture["conflictCells"]),
            {str(k): dict(v) for k, v in fixture["metaByCell"].items()},
            {str(k): dict(v) for k, v in fixture["heatByBucket"].items()},
            now_ms=NOW_MS,
        )
    if name == "decisions_audit_basic":
        return aggregate_decision_audit(
            str(fixture["tenant"]),
            int(fixture["window"]),
            _parse_lines(fixture["dLines"]),
            _parse_lines(fixture["oLines"]),
            now_ms=NOW_MS,
        )
    if name == "decisions_trend_basic":
        return aggregate_decision_trend(
            str(fixture["tenant"]),
            int(fixture["window"]),
            int(fixture["steps"]),
            _parse_lines(fixture["dLines"]),
            _parse_lines(fixture["oLines"]),
            now_ms=NOW_MS,
        )
    if name == "workers_basic":
        return aggregate_worker_liveness(str(fixture["tenant"]), [dict(r) for r in fixture["rows"]])
    if name == "conflicts_basic":
        return aggregate_human_conflict(
            str(fixture["tenant"]),
            int(fixture["window"]),
            _parse_lines(fixture["oLines"]),
            [dict(e) for e in fixture["auditEntries"]],
            now_ms=NOW_MS,
        )
    if name == "trail_normalize_basic":
        return normalize_audit_trails(
            [dict(e) for e in fixture["human"]],
            {str(k): [dict(r) for r in v] for k, v in fixture["commandsByTenant"].items()},
            [dict(e) for e in fixture["arbitrations"]],
            [dict(e) for e in fixture["supervisors"]],
        )
    if name == "trail_merge_basic":
        return merge_audit_trails(
            [dict(e) for e in fixture["normalized"]],
            tenant=fixture["opts"].get("tenant"),
            source=fixture["opts"].get("source"),
            limit=int(fixture["opts"].get("limit", 200)),
        )
    if name == "map_lod_basic":
        return aggregate_map_lod(
            str(fixture["tenant"]),
            [dict(r) for r in fixture["resources"]],
            [dict(r) for r in fixture["obstacles"]],
            [dict(r) for r in fixture["cores"]],
        )
    if name == "mines_util_basic":
        return aggregate_mine_utilization(
            str(fixture["tenant"]),
            fixture["currentTick"],
            [dict(r) for r in fixture["resources"]],
            [dict(r) for r in fixture["harvestEvents"]],
        )
    if name == "mines_trend_basic":
        return aggregate_mine_utilization_trend(
            str(fixture["tenant"]),
            int(fixture["window"]),
            int(fixture["steps"]),
            [dict(r) for r in fixture["resources"]],
            [dict(r) for r in fixture["harvestEvents"]],
            fixture["currentTick"],
            now_ms=NOW_MS,
        )
    if name == "shop_history_agg_basic":
        return aggregate_shop_history([dict(e) for e in fixture["entries"]])
    if name == "shop_history_normalize_basic":
        return normalize_products([dict(p) for p in fixture["products"]])
    if name == "shop_history_signature_basic":
        return snapshot_signature([dict(p) for p in fixture["products"]])
    if name == "shop_history_should_append_basic":
        prev = fixture["prev"]
        return should_append(
            dict(prev) if prev is not None else None,
            [dict(p) for p in fixture["products"]],
        )
    if name == "mining_effectiveness_basic":
        return aggregate_allocation_effectiveness(
            [dict(a) for a in fixture["assignments"]],
            {
                str(t): {str(c): dict(v) for c, v in cells.items()}
                for t, cells in fixture["harvestByTenantCell"].items()
            },
            fixture["currentTick"],
            now_ms=NOW_MS,
        )
    if name == "arbitrations_basic":
        rows = [dict(row) for row in fixture["lines"] if isinstance(row, dict)]
        return [[cell, entry] for cell, entry in load_arbitrations(rows).items()]
    if name == "human_audit_basic":
        rows = [dict(row) for row in fixture["lines"] if isinstance(row, dict)]
        tenant = fixture.get("tenant")
        return load_human_audit(rows, tenant=tenant, limit=int(fixture["limit"]))
    if name in ("alliance_advice_basic", "alliance_advice_full"):
        import tempfile
        from pathlib import Path

        from .tools.advice_fixture import materialize_advice_data_root

        with tempfile.TemporaryDirectory(prefix="cc-advice-parity-") as root_dir:
            root = Path(root_dir)
            materialize_advice_data_root(fixture, root)
            return load_alliance_advice(root, now_ms=int(fixture["nowMs"]))
    if name == "alignment_basic":
        return aggregate_alignment(
            fixture["decisions"],
            fixture["mines"],
            fixture["effectiveness"],
            fixture["trends"],
            fixture["workersByTenant"],
            now_ms=NOW_MS,
        )
    if name == "exploration_coverage_basic":
        return compute_exploration_stats(
            fixture["chunksByTenant"],
            {
                str(t): (tuple(core) if core is not None else None)
                for t, core in fixture["coresByTenant"].items()
            },
            int(fixture["currentTick"]),
        )
    if name == "mine_patterns_predict_basic":
        return compute_refill_predictions(
            [dict(r) for r in fixture["rows"]],
            [dict(r) for r in fixture["resources"]],
            int(fixture["currentTick"]),
        )
    if name == "mine_patterns_predict_absences_basic":
        return compute_refill_predictions_from_absences(
            [dict(r) for r in fixture["absences"]],
            [dict(r) for r in fixture["seenHistory"]],
            [dict(r) for r in fixture["resources"]],
            int(fixture["currentTick"]),
        )
    if name == "mine_patterns_absent_stats_basic":
        return compute_absent_stats([dict(r) for r in fixture["absences"]])
    if name == "mine_patterns_dead_mines_basic":
        return compute_dead_mines(
            [dict(r) for r in fixture["absences"]],
            [dict(r) for r in fixture["resources"]],
        )
    if name == "mine_patterns_accuracy_basic":
        return compute_prediction_accuracy(
            [dict(r) for r in fixture["predictions"]],
            [dict(r) for r in fixture["rows"]],
            int(fixture["currentTick"]),
        )
    if name == "consensus_mining_basic":
        return enrich_consensus_mining(
            fixture["survey"],
            fixture["effectiveness"],
            fixture["mines"],
            fixture["heatByBucket"],
        )
    if name in ("survey_mine_basic", "survey_mine_default_cell"):
        import tempfile
        from pathlib import Path

        from .tools.advice_fixture import materialize_advice_data_root

        with tempfile.TemporaryDirectory(prefix="cc-survey-mine-parity-") as root_dir:
            root = Path(root_dir)
            materialize_advice_data_root(fixture, root)
            return load_survey_mine(root, str(fixture["tenant"]), fixture.get("cell"))
    if name in ("lifecycle_basic", "survey_basic", "exploration_basic"):
        import tempfile
        from pathlib import Path

        from .tools.advice_fixture import materialize_advice_data_root

        with tempfile.TemporaryDirectory(prefix="cc-wave6-parity-") as root_dir:
            root = Path(root_dir)
            materialize_advice_data_root(fixture, root)
            if name == "lifecycle_basic":
                return load_lifecycle_audit(root, str(fixture.get("tenant", "t1")), now_ms=NOW_MS)
            if name == "survey_basic":
                return load_survey(root, str(fixture.get("tenant", "all")), now_ms=NOW_MS)
            return load_exploration(root, str(fixture.get("tenant", "t1")), now_ms=NOW_MS)
    if name in ("intel_basic", "leaderboard_basic"):
        import tempfile
        from pathlib import Path

        from .tools.advice_fixture import materialize_advice_data_root

        with tempfile.TemporaryDirectory(prefix="cc-wave7-parity-") as root_dir:
            root = Path(root_dir)
            materialize_advice_data_root(fixture, root)
            if name == "intel_basic":
                return load_alliance_intel(root, now_ms=NOW_MS)
            return build_leaderboard_payload(root, now_ms=NOW_MS)
    if name == "enemy_cores_basic":
        return build_enemy_core_states(
            [dict(r) for r in fixture["hunts"]],
            int(fixture["currentTick"]),
            [tuple(c) for c in fixture["friendlyCores"]],
        )
    if name == "decision_input_basic":
        return build_decision_input(
            str(fixture["tenant"]),
            fixture["currentTick"],
            [dict(p) for p in fixture["predictions"]],
            [dict(c) for c in fixture["chunks"]],
            {str(k): dict(v) for k, v in fixture["threatByCell"].items()},
            [dict(r) for r in fixture["resurvey"]],
            [dict(c) for c in fixture["coreThreats"]],
            [dict(c) for c in fixture["miningCandidates"]],
            now_ms=NOW_MS,
        )
    raise AssertionError(f"no Python runner registered for fixture {name}")


def _remove_key(value: object, key: str) -> object:
    """Recursively drop a wall-clock-derived key (leaderboard ``stale``)."""
    if isinstance(value, dict):
        return {k: _remove_key(item, key) for k, item in value.items() if k != key}
    if isinstance(value, list):
        return [_remove_key(item, key) for item in value]
    return value


def _normalize_for_case(case: str, value: object) -> object:
    """Per-case parity normalization for non-oracle-comparable fields."""
    if case == "leaderboard_basic":
        return _remove_key(value, "stale")
    return value


CASES = [
    "alliance_cluster_basic",
    "alliance_defense_basic",
    "alliance_intel_basic",
    "alliance_mining_basic",
    "alliance_snapshot_basic",
    "decisions_audit_basic",
    "decisions_trend_basic",
    "workers_basic",
    "conflicts_basic",
    "trail_normalize_basic",
    "trail_merge_basic",
    "map_lod_basic",
    "mines_util_basic",
    "mines_trend_basic",
    "shop_history_agg_basic",
    "shop_history_normalize_basic",
    "shop_history_signature_basic",
    "shop_history_should_append_basic",
    "mining_effectiveness_basic",
    "arbitrations_basic",
    "human_audit_basic",
    "alliance_advice_basic",
    "alliance_advice_full",
    "alignment_basic",
    "exploration_coverage_basic",
    "mine_patterns_predict_basic",
    "mine_patterns_predict_absences_basic",
    "mine_patterns_absent_stats_basic",
    "mine_patterns_dead_mines_basic",
    "mine_patterns_accuracy_basic",
    "consensus_mining_basic",
    "survey_mine_basic",
    "survey_mine_default_cell",
    "enemy_cores_basic",
    "decision_input_basic",
    "lifecycle_basic",
    "survey_basic",
    "exploration_basic",
    "intel_basic",
    "leaderboard_basic",
]


@pytest.mark.parametrize("case", CASES)
def test_projection_matches_ts_oracle_golden(case: str) -> None:
    """Every projection core classifies MATCH against the TS oracle golden."""
    fixture = load_fixture(case)
    actual = _normalize_for_case(case, _run_python(case, fixture))
    expected = _normalize_for_case(case, load_golden(case))
    assert_matches(actual, expected, case)


def test_all_golden_cases_classify_match() -> None:
    """Aggregate classification: every registered case is MATCH, no UNKNOWN."""
    mismatches: list[str] = []
    for case in CASES:
        fixture = load_fixture(case)
        actual = _normalize_for_case(case, _run_python(case, fixture))
        expected = _normalize_for_case(case, load_golden(case))
        try:
            assert_matches(actual, expected, case)
        except AssertionError:
            mismatches.append(case)
    assert mismatches == [], f"UNKNOWN parity results: {mismatches}"


def test_fixture_golden_pairs_are_complete() -> None:
    """Every fixture has a golden and every golden has a fixture."""
    from .conftest import FIXTURES

    json_bases = sorted(
        p.name[: -len(".json")]
        for p in FIXTURES.glob("*.json")
        if not p.name.endswith(".golden.json")
    )
    goldens = sorted(p.name for p in FIXTURES.glob("*.golden.json"))
    assert {f"{base}.golden.json" for base in json_bases} == set(goldens)


def test_num_coercion_matches_ts_number() -> None:
    """The shared number coercion mirrors the TS ``num`` helper."""
    assert num(3) == 3
    assert num("3") == 3
    assert num("12.5") == 12.5
    assert num("") == 0
    assert num("abc") == 0
    assert num(None) == 0
    assert num(True) == 0
    assert num(False) == 0
    assert num(3.5) == 3.5
