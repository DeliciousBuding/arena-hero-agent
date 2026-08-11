# P4-11 SafetyPlanner/tactical — Behavior-Difference Registry

最后更新：2026-08-12

This file is the authoritative behavior-difference registry for the P4-11
deterministic safety/tactical layer (`src/arena_hero_agent/planning/` and
`src/arena_hero_agent/strategies/`). It classifies every observable difference
between the Python implementation and the legacy TypeScript oracle
(`arena-hero-agent-ts@8cf5cbb`).

Classification rules:

- **MATCH** — fixture-compared in this suite; the Python result equals the
  oracle capture (`tests/strategies/fixtures/known_answers_v1.json`).
- **ALLOWED_DIFFERENCE** — deliberate, documented deviation. The Python
  behavior is fail-closed and deterministic; the oracle cannot express it.
- **EXPECTED_UNKNOWN** — stateful oracle behavior that is intentionally NOT
  migrated. It stays visible and must never be counted as MATCH.

## MATCH sections (fixture-compared)

`threat_maps`, `forced_tasks`, `worker_dense_directions`,
`threat_weighted_directions`, `tier_of_damage_rank`, `spawn_choice`,
`defense_posts`, `home_cells`, `guard_home_cells`, `yield_anchors`,
`occupancy_counts`, `nearest_enemies`, `retreat_directions`,
`shot_priorities`, `can_shoot`, `predicted_enemy_cells`, `kite_cells`,
`core_shelters`, `is_core_shelter`, `rally_slots`,
`rally_points_at_slot`, `rally_member_slots`,
`rally_points_at_member_slot`, `tactical_squads`, `plan_validation`,
`step_toward`, `mission_value` (target_confidence / is_collectable /
refill_bonus / surveyor_ids).

## ALLOWED_DIFFERENCES

| id | Python behavior | Oracle behavior | Rationale |
|---|---|---|---|
| `forced_tasks.worker_on_invisible_resource` | Worker on a resource cell that is not visible this tick gets **no forced task** (falls through to the cost matrix). | Reports `HARVEST_CURRENT`. | The TS snapshot models `resourceCells` as a plain Set without visibility metadata. Python `ResourceCellInfo.visible` carries the documented v0.11 visibility fix; harvesting an unseen/memory cell is fail-closed. |
| `validator_unknown_shapes` | Unknown or malformed action shapes are **rejected** with a validation issue (and malformed shapes are rejected at DTO construction). | TS `validatePlan` silently skips unknown action types. | Fail-closed contract: an unsupported action must never silently pass through into a repaired plan. |
| `planner_composition` | `SafetyPlanner` is a deterministic composition of fixture-compared helpers (worker forced-task → action, vanguard sweep/guard, ranger shoot/guard, core spawn budget). | The oracle SafetyPlanner is stateful and spans thousands of lines. | The composition is deliberately simpler; every helper it calls is oracle-compared. Composition behavior is not a MATCH claim. |
| `beacon_sentinel` | When no beacon observation exists, the snapshot uses `BeaconInfo(position=(0,0), status=None, carrier_id=None)`. | TS snapshot leaves `beacon` undefined. | `status=None` means unseen; every beacon-dependent gate (pickup/drop/forced task) fails closed on it. |
| `threat_map_float_accumulation` | Threat contributions accumulate in enemy input order, then fixed `(dx, dy)` order, using IEEE-754 doubles. | Same accumulation order in the oracle. | The captured fixture values are bit-exact because both runtimes use doubles; registered so a future reorder cannot silently change values. |

## EXPECTED_UNKNOWN (not migrated, never MATCH)

Stateful SafetyPlanner behavior deliberately left out of the deterministic
layer. These remain visible as open work for later phases:

- `core_evade` — core emergency evasive movement
- `spawn_surge` — population surge / emergency spawn policies
- `core_migration` — core shelter migration decision loop
- `blockade` — resource-blockade tactical behavior
- `beacon_fetch_pipeline` — beacon retrieval sequencing
- `alliance_logic` — ally awareness and coordination
- `refill_prediction_pipeline` — mine refill prediction model
- `macro_policy` — economy/macro transition policy
- `worker_assignment_matrix` — worker cost-matrix assignment (P4-12)

The registry test (`tests/planning/test_differential_registry.py`) enforces
that every fixture section is classified and that the EXPECTED_UNKNOWN set can
never leak into MATCH.
