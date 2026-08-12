# P4-11/P4-12/P4-13 SafetyPlanner/tactical + worker assignment + variants/human override — Behavior-Difference Registry

最后更新：2026-08-12

This file is the authoritative behavior-difference registry for the P4-11
deterministic safety/tactical layer (`src/arena_hero_agent/planning/` and
`src/arena_hero_agent/strategies/`), the P4-12 worker assignment layer
(`src/arena_hero_agent/planning/worker_assignment.py` and
`min_cost_assignment.py`), and the P4-13 strategy variant selection +
human override layer (`src/arena_hero_agent/strategies/variant_registry.py`
and `src/arena_hero_agent/command_center/human_override.py`). It classifies
every observable difference between the Python implementation and the
legacy TypeScript oracle (`arena-hero-agent-ts@8cf5cbb`).

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

P4-12 worker assignment sections:

- `min_cost_assignment` — the deterministic rectangular Hungarian solver
  (`minimumCostAssignment` / `minimum_cost_assignment`), tie-break included.
- `assignment_routing` — the bounded obstacle-aware BFS distance field
  (`shortestPathDistances` / `shortest_path_distances`).
- `progress_decay` and `sticky_bonus` — the progress-aware sticky kernel and
  its application over the previous tick's assignments.
- `worker_assignments` — full `WorkerTaskPlanner.plan()` outputs (single-tick
  and multi-tick claim-lease sequences). This replaces the former
  EXPECTED_UNKNOWN `worker_assignment_matrix` entry.

Note on dead mission config: `alwaysSurvey` and `surveyOnSupplyGap` are dead
variables in the oracle's worker-task-planner (edfa8ab cleanup) and have no
behavioral effect in either runtime at this layer. The `always_survey_noop`
fixture case pins that the Python layer also ignores them. The config fields
remain in `MissionConfig` for parity only.

P4-13 sections:

- `variant_config` — `resolveSafetyVariantConfig` /
  `resolveVariantsConfig` outputs for every variant registered on the
  Python config surface (`population-ceiling-30/35/40-v1`), including the
  empty-list and merge-order cases. Field names are translated from the
  oracle's camelCase to the Python snake_case surface.
- `human_override` — `applyHumanOverrides` results for 23 deterministic
  cases (one-shot command apply/reject, mine/goto goal flows, stale expiry,
  disabled stores, core START_MOVE/CANCEL_MOVE/SPAWN, capability and unknown
  unit rejections, far-target interpolation). Every oracle-expressible field
  (active/applied/rejected/satisfied/updatedAt/plan actions) matches.

## ALLOWED_DIFFERENCES

| id | Python behavior | Oracle behavior | Rationale |
|---|---|---|---|
| `forced_tasks.worker_on_invisible_resource` | Worker on a resource cell that is not visible this tick gets **no forced task** (falls through to the cost matrix). | Reports `HARVEST_CURRENT`. | The TS snapshot models `resourceCells` as a plain Set without visibility metadata. Python `ResourceCellInfo.visible` carries the documented v0.11 visibility fix; harvesting an unseen/memory cell is fail-closed. |
| `validator_unknown_shapes` | Unknown or malformed action shapes are **rejected** with a validation issue (and malformed shapes are rejected at DTO construction). | TS `validatePlan` silently skips unknown action types. | Fail-closed contract: an unsupported action must never silently pass through into a repaired plan. |
| `planner_composition` | `SafetyPlanner` is a deterministic composition of fixture-compared helpers (worker forced-task → action, vanguard sweep/guard, ranger shoot/guard, core spawn budget); the P4-21 live decider additionally runs `SafetyPlanner` baseline → `assign_worker_tasks` override → task-to-action/`Plan`→`Decision` conversion. | The oracle `DeterministicPlanner` is stateful and spans thousands of lines. | The composition is deliberately simpler; every helper it calls is oracle-compared (worker_assignment fixture 33 cases, safety_helpers). Composition behavior is not a MATCH claim. |
| `beacon_sentinel` | When no beacon observation exists, the snapshot uses `BeaconInfo(position=(0,0), status=None, carrier_id=None)`. | TS snapshot leaves `beacon` undefined. | `status=None` means unseen; every beacon-dependent gate (pickup/drop/forced task) fails closed on it. |
| `threat_map_float_accumulation` | Threat contributions accumulate in enemy input order, then fixed `(dx, dy)` order, using IEEE-754 doubles. | Same accumulation order in the oracle. | The captured fixture values are bit-exact because both runtimes use doubles; registered so a future reorder cannot silently change values. |
| `worker_assignment_refill_predictions_parameter` | `assign_worker_tasks` receives `refillPredictions` as an explicit `refill_predictions` parameter (default `None` = zero regression). | The oracle reads `snapshot.refillPredictions` as a snapshot field. | The prediction pipeline itself is EXPECTED_UNKNOWN; the deterministic layer takes the predictions as an input so the same snapshot contract stays pure and diffable. |
| `worker_assignment_claims_explicit_state` | The cross-tick GO_RESOURCE claim lease is an explicit deterministic input/output (`claims` → result `claims`). | The oracle hides the lease in `WorkerTaskPlanner` class state (`this.claims`). | Same pruning/reservation/update behavior, but as a pure function: identical inputs always produce identical outputs and claims, which is what the P4-12 differential requires. |
| `human_override.stale_override_ignored` | The Python result exposes an explicit `stale=True` audit flag when the store expired. | The oracle result has no stale field; expiry is inferred from `active=false` with a non-empty store. | All oracle-expressible fields are equal; the explicit flag makes the expiry auditable in the apply/reject loop. |
| `action_from_wire_surface_strict` | Wire actions parse strictly against the target surface (core vs unit); a cross-surface type is rejected as `invalid_action`. | The oracle parses any shape and defers some cross-surface cases to plan validation. | Fail-closed parse: a unit can never silently receive a core-only action shape or vice versa. |

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
- `worker_liveness_blockade` — W5 cell-blocker view (`options.cellBlocker`):
  a stateful liveness-tracker hook that the deterministic worker-assignment
  layer does not take; the oracle default (no blocker) is bit-identical
- `mine_hold_goals` — the oracle `mine_hold` goal kind (mine-belt watch)
  is not carried by the Python store: the P5-3 store parses goal kinds
  `mine|goto` only. The oracle's mine_hold behavior stays visible and is
  never counted as MATCH.
- `variant_config_unmigrated_ids` — every oracle variant whose effect is
  not fully expressible on the Python `SafetyPlannerConfig` surface
  (threat-recall, strike-core, tactical-squads, ...) is deliberately NOT
  registered: enabling one in a Python config fails fast instead of
  silently running with weakened behavior.
- `human_override_path_abandon_pruning` — the oracle's `stepTowardPath`
  abandon-factor pruning (abandon detours > 3x the direct distance) is not
  migrated; Python reuses the oracle-compared domain `first_step` with the
  oracle's adaptive search radius. Captured fixture cases match; a detour
  beyond the abandon factor may still route in Python where the oracle
  would WAIT.

The registry test (`tests/planning/test_differential_registry.py`) enforces
that every fixture section is classified and that the EXPECTED_UNKNOWN set can
never leak into MATCH.
