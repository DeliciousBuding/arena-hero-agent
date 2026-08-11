"""Behavior-difference classification for the P4-11/P4-12 oracle fixture.

Every fixture section must be classified as MATCH (fixture-compared in this
suite), ALLOWED_DIFFERENCE (intentional, documented), or EXPECTED_UNKNOWN
(not migrated; must remain visible and never counted as MATCH). The full
rationale lives in docs/planning-differences.md.
"""

from __future__ import annotations

from tests.strategies.fixture_loader import load_oracle_fixture

# Sections fixture-compared by the differential tests in this suite.
MATCH_SECTIONS = frozenset(
    {
        "threat_maps",
        "forced_tasks",
        "worker_dense_directions",
        "threat_weighted_directions",
        "tier_of_damage_rank",
        "spawn_choice",
        "defense_posts",
        "home_cells",
        "guard_home_cells",
        "yield_anchors",
        "occupancy_counts",
        "nearest_enemies",
        "retreat_directions",
        "shot_priorities",
        "can_shoot",
        "predicted_enemy_cells",
        "kite_cells",
        "core_shelters",
        "is_core_shelter",
        "rally_slots",
        "rally_points_at_slot",
        "rally_member_slots",
        "rally_points_at_member_slot",
        "tactical_squads",
        "plan_validation",
        "step_toward",
        "mission_value",
        "min_cost_assignment",
        "assignment_routing",
        "progress_decay",
        "sticky_bonus",
        "worker_assignments",
    }
)

# Registered intentional differences where Python deliberately deviates from
# the oracle snapshot; each is asserted directly in the suite.
ALLOWED_DIFFERENCES = frozenset(
    {
        "forced_tasks.worker_on_invisible_resource",
        "planner_composition",
        "validator_unknown_shapes",
        "beacon_sentinel",
        "worker_assignment_refill_predictions_parameter",
        "worker_assignment_claims_explicit_state",
    }
)

# Un-migrated stateful SafetyPlanner behaviors. They must never be claimed as
# MATCH; the deterministic composition is an ALLOWED_DIFFERENCE.
EXPECTED_UNKNOWN = frozenset(
    {
        "core_evade",
        "spawn_surge",
        "core_migration",
        "blockade",
        "beacon_fetch_pipeline",
        "alliance_logic",
        "refill_prediction_pipeline",
        "macro_policy",
        "worker_liveness_blockade",
    }
)


def test_all_fixture_sections_are_classified() -> None:
    fixture = load_oracle_fixture()
    sections = set(fixture) - {"metadata"}
    assert sections == MATCH_SECTIONS, (
        "every fixture section must be classified; add new sections to MATCH_SECTIONS "
        "or register an allowed difference in docs/planning-differences.md"
    )


def test_oracle_metadata_is_pinned() -> None:
    fixture = load_oracle_fixture()
    metadata = fixture["metadata"]
    assert metadata["oracle_commit"] == "8cf5cbbcccf396a8feee94404af44969c5388e15"
    for source in (
        "packages/arena-agent/src/planning/planning-snapshot.ts",
        "packages/arena-agent/src/planning/task.ts",
        "packages/arena-agent/src/strategies/safety-planner-helpers.ts",
        "packages/arena-agent/src/strategies/tactical-squads.ts",
        "packages/arena-agent/src/domain/plan-validator.ts",
        "packages/arena-agent/src/planning/worker-task-planner.ts",
        "packages/arena-agent/src/algorithms/min-cost-assignment.ts",
        "packages/arena-agent/src/domain/nav.ts",
    ):
        assert source in metadata["source_files"]


def test_unknown_behaviors_never_count_as_match() -> None:
    # EXPECTED_UNKNOWN behaviors are not part of any fixture section and must
    # never be silently added to the MATCH set.
    assert not EXPECTED_UNKNOWN.intersection(MATCH_SECTIONS)
    assert not EXPECTED_UNKNOWN.intersection(ALLOWED_DIFFERENCES)


def test_allowed_difference_keys_map_to_real_registered_cases() -> None:
    fixture = load_oracle_fixture()
    # forced-task invisible-resource difference references a real fixture case
    names = {case["name"] for case in fixture["forced_tasks"]}
    assert "worker_on_invisible_resource" in names
    for key in ALLOWED_DIFFERENCES:
        section, _, case = key.partition(".")
        if case:
            assert section in MATCH_SECTIONS
            assert case in {entry["name"] for entry in fixture[section]}
