"""Unit tests for the event-driven ring-quota exploration module (research, off)."""

from __future__ import annotations

from arena_hero_agent.domain import CURRENT_RULES_VERSION, Coordinate, EntityId, UnitRole, manhattan
from arena_hero_agent.planning import (
    Assignment,
    BeaconInfo,
    ExplorationState,
    PlanningSnapshot,
    PlanningUnit,
    ResourceCellInfo,
    Task,
    TaskType,
    build_exploration_targets,
    chunk_of,
    chunk_quota,
    chunk_ring,
    explorer_slot,
    is_hungry,
    mark_reached,
    observe_exploration,
    refill_tick_at_or_after,
    ring_radii,
    with_memory_resource_cells,
)
from arena_hero_agent.planning.exploration import HUNGER_TICKS, _chunk_recheck_ladder, chunk_center

RULES = CURRENT_RULES_VERSION
_ORIGIN = Coordinate(0, 0)

_DIRS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))


def _worker(identifier: str, x: int, y: int, *, cargo: int = 0) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(identifier),
        unit_role=UnitRole.WORKER,
        position=Coordinate(x, y),
        health=2,
        cargo=cargo,
    )


def _snapshot(
    *,
    tick: int = 100,
    units: tuple[PlanningUnit, ...] = (),
    core: Coordinate | None = _ORIGIN,
    resource_cells: dict[str, ResourceCellInfo] | None = None,
    obstacles: frozenset[str] = frozenset(),
    enemies: frozenset[str] = frozenset(),
) -> PlanningSnapshot:
    return PlanningSnapshot(
        tick=tick,
        rules_version=RULES,
        resources=0,
        resource_capacity=100,
        resource_space=100,
        population=len(units),
        units=units,
        resource_cells={} if resource_cells is None else resource_cells,
        obstacle_cells=obstacles,
        enemy_cells=enemies,
        enemy_units=(),
        core_id=None if core is None else "core",
        core_position=core,
        core_health=None if core is None else 5,
        core_shield=None if core is None else 5,
        core_state=None if core is None else "normal",
        beacon=BeaconInfo(position=Coordinate(0, 0), status=None),
        threat_map={},
    )


def _candidates(unit_id: str, core: Coordinate, *, hungry: bool = False) -> list[Coordinate]:
    slot = explorer_slot(unit_id)
    vx, vy = _DIRS[slot % 8]
    result: list[Coordinate] = []
    for radius in ring_radii(slot, hungry):
        scale = radius // (abs(vx) + abs(vy))
        result.append(Coordinate(core.x + vx * scale, core.y + vy * scale))
    return result


def _harvest_assignment(unit_id: str, target: Coordinate) -> Assignment:
    return Assignment(
        unit_id=unit_id,
        task=Task(
            type=TaskType.HARVEST_CURRENT,
            target=target,
            target_cell_key=target.cell_key,
        ),
    )


def test_explorer_slot_is_stable_and_bounded() -> None:
    for identifier in ("w1", "w2", "w3", "worker-alpha", "unit:42"):
        assert 0 <= explorer_slot(identifier) < 16
        assert explorer_slot(identifier) == explorer_slot(identifier)


def test_chunk_quota_matches_reference_formula() -> None:
    assert chunk_quota((0, 0)) == 16
    assert chunk_quota((1, 0)) == 14
    assert chunk_quota((2, 0)) == 12
    assert chunk_quota((-1, -1)) == 16


def test_chunk_ring_maps_negative_axes() -> None:
    assert chunk_ring((0, 0)) == 0
    assert chunk_ring((-1, 0)) == 0
    assert chunk_ring((-2, 0)) == 1
    assert chunk_ring((1, 2)) == 3
    assert chunk_of(Coordinate(-1, -1)) == (-1, -1)


def test_refill_tick_at_or_after_aligns_to_four_tick_boundary() -> None:
    assert refill_tick_at_or_after(0) == 0
    assert refill_tick_at_or_after(1) == 4
    assert refill_tick_at_or_after(3) == 4
    assert refill_tick_at_or_after(4) == 4
    assert refill_tick_at_or_after(5) == 8


def test_ring_radii_first_group_and_second_group() -> None:
    assert ring_radii(0, hungry=False) == (10, 20, 30)
    assert ring_radii(7, hungry=False) == (10, 20, 30)
    assert ring_radii(8, hungry=False) == (20, 30, 40)
    assert ring_radii(15, hungry=False) == (20, 30, 40)
    hungry_0 = ring_radii(0, hungry=True)
    assert hungry_0[:5] == (8, 16, 24, 32, 40)
    assert len(hungry_0) == 20
    assert hungry_0[-1] == 160
    hungry_8 = ring_radii(8, hungry=True)
    assert hungry_8[:5] == (16, 24, 32, 40, 48)
    assert len(hungry_8) == 20
    assert hungry_8[-1] == 168


def test_build_targets_empty_without_core() -> None:
    snapshot = _snapshot(units=(_worker("w1", 0, 0),), core=None)
    assert build_exploration_targets(snapshot, ExplorationState()) == {}


def test_build_targets_ignores_non_workers() -> None:
    vanguard = PlanningUnit(
        id=EntityId("v1"),
        unit_role=UnitRole.VANGUARD,
        position=Coordinate(0, 0),
        health=4,
        cargo=0,
    )
    snapshot = _snapshot(units=(vanguard,))
    assert build_exploration_targets(snapshot, ExplorationState()) == {}


def test_build_targets_lie_on_normal_ring_band() -> None:
    snapshot = _snapshot(
        units=(_worker("w1", 0, 0), _worker("w2", 1, 0), _worker("w3", 0, 1)),
    )
    targets = build_exploration_targets(snapshot, ExplorationState())
    assert set(targets) == {"w1", "w2", "w3"}
    for target in targets.values():
        assert manhattan(target, Coordinate(0, 0)) in (8, 16, 24, 32, 40, 48)


def test_build_targets_skip_obstacle_and_pick_next_valid_direction() -> None:
    unit_id = "w1"
    # First ring-band candidate (8,0) is blocked; the next valid candidate is
    # a different direction at the same radius instead of the blocked cell.
    snapshot = _snapshot(units=(_worker(unit_id, 0, 0),), obstacles=frozenset({"8,0"}))
    targets = build_exploration_targets(snapshot, ExplorationState())
    assert targets[unit_id] == Coordinate(4, 4)


def test_build_targets_advance_outward_after_full_ring() -> None:
    core = Coordinate(0, 0)
    unit_id = "w1"
    state = ExplorationState()
    snapshot = _snapshot(units=(_worker(unit_id, 0, 0),))
    # Visit every ring-8 point; the next target must fall on ring 16.
    for vx, vy in _DIRS:
        scale = 8 // (abs(vx) + abs(vy))
        point = Coordinate(core.x + vx * scale, core.y + vy * scale)
        state.point_visited[point.cell_key] = snapshot.tick
    target = build_exploration_targets(snapshot, state)[unit_id]
    assert manhattan(target, core) == 16


def test_build_targets_hungry_extends_beyond_thirty() -> None:
    core = Coordinate(0, 0)
    unit_id = "w1"
    # Hungry mode extends the sweep to 64; block everything up to 56 so the
    # only valid target is the outer 64 ring.
    blocked = set()
    for radius in (8, 16, 24, 32, 40, 48, 56):
        for vx, vy in _DIRS:
            scale = radius // (abs(vx) + abs(vy))
            blocked.add(Coordinate(core.x + vx * scale, core.y + vy * scale).cell_key)
    snapshot = _snapshot(units=(_worker(unit_id, 0, 0),), obstacles=frozenset(blocked))
    targets = build_exploration_targets(snapshot, ExplorationState(), hungry=True)
    assert manhattan(targets[unit_id], core) == 64


def test_build_targets_rejects_unreachable_ring_candidate() -> None:
    unit_id = "w1"
    # A vertical wall at x=1 (taller than the flood radius) separates the worker
    # from the first ring candidates (8, 0) and (4, 4).  Those are not blocked
    # themselves, so the old validity check would accept them and stall; the
    # reachability gate must reject them and pick the first reachable ring point
    # on the worker's side, (0, 8).
    wall = frozenset(Coordinate(1, y).cell_key for y in range(-100, 101))
    snapshot = _snapshot(units=(_worker(unit_id, 0, 0),), obstacles=wall)
    targets = build_exploration_targets(snapshot, ExplorationState())
    assert targets[unit_id] == Coordinate(0, 8)


def test_build_targets_bfs_frontier_fallback_in_maze() -> None:
    unit_id = "w1"
    core = Coordinate(0, 0)
    # Block every normal sweep-ring candidate so the directional ring has
    # no valid target; the scout must fall back to the BFS nearest-unvisited
    # frontier from its own position instead of stalling.
    blocked = set()
    for radius in (8, 16, 24, 32, 40, 48):
        for vx, vy in _DIRS:
            scale = radius // (abs(vx) + abs(vy))
            blocked.add(Coordinate(core.x + vx * scale, core.y + vy * scale).cell_key)
    snapshot = _snapshot(
        units=(_worker(unit_id, 0, 0),),
        obstacles=frozenset(blocked),
    )
    targets = build_exploration_targets(snapshot, ExplorationState())
    # Nearest reachable, unvisited, unclaimed cell is the first cardinal step.
    assert targets[unit_id] == Coordinate(1, 0)


def test_observe_exploration_marks_visible_chunk_seen() -> None:
    state = ExplorationState()
    cell = ResourceCellInfo(position=Coordinate(8, 8), visible=True, last_seen_tick=10)
    snapshot = _snapshot(tick=10, resource_cells={cell.position.cell_key: cell})
    observe_exploration(snapshot, (), state)
    assert state.chunk_seen_tick[(0, 0)] == 10
    assert state.chunk_anchor[(0, 0)] == Coordinate(8, 8)


def test_observe_exploration_records_harvest_refill_and_hunger() -> None:
    state = ExplorationState()
    unit = _worker("w1", 10, 0, cargo=3)
    snapshot = _snapshot(tick=7, units=(unit,))
    target = Coordinate(10, 0)
    observe_exploration(snapshot, (_harvest_assignment("w1", target),), state)
    chunk = chunk_of(target)
    assert state.chunk_harvest_count[chunk] == 1
    assert state.chunk_last_harvest_tick[chunk] == 7
    assert state.chunk_next_refill_tick[chunk] == 8
    assert state.hungry_since == 7


def test_is_hungry_strictly_after_hunger_ticks() -> None:
    state = ExplorationState(hungry_since=10)
    assert is_hungry(state, 10 + HUNGER_TICKS) is False
    assert is_hungry(state, 10 + HUNGER_TICKS + 1) is True


def test_build_targets_prefer_refill_due_chunk_anchor() -> None:
    unit_id = "w1"
    state = ExplorationState()
    anchor = Coordinate(100, 0)
    chunk = chunk_of(anchor)
    state.chunk_next_refill_tick[chunk] = 100
    state.chunk_anchor[chunk] = anchor
    snapshot = _snapshot(tick=100, units=(_worker(unit_id, 0, 0),))
    targets = build_exploration_targets(snapshot, state)
    assert targets[unit_id] == anchor


def test_mark_reached_records_visited_and_probe() -> None:
    state = ExplorationState()
    unit = _worker("w1", 9, 0)
    snapshot = _snapshot(tick=20, units=(unit,))
    target = Coordinate(10, 0)
    assignment = Assignment(unit_id="w1", task=Task(type=TaskType.EXPLORE, target=target))
    mark_reached(snapshot, (assignment,), state)
    assert state.point_visited[target.cell_key] == 20
    assert state.chunk_last_probe_tick[chunk_of(target)] == 20


def test_mark_reached_ignores_non_explore_and_far_targets() -> None:
    state = ExplorationState()
    unit = _worker("w1", 0, 0)
    snapshot = _snapshot(tick=20, units=(unit,))
    far = Assignment(unit_id="w1", task=Task(type=TaskType.EXPLORE, target=Coordinate(10, 0)))
    harvest = _harvest_assignment("w1", Coordinate(0, 0))
    mark_reached(snapshot, (far, harvest), state)
    assert state.point_visited == {}
    assert state.chunk_last_probe_tick == {}


def test_with_memory_resource_cells_merges_remembered_cells() -> None:
    state = ExplorationState()
    remembered = Coordinate(30, 0)
    state.cell_positions[remembered.cell_key] = remembered
    state.cell_last_seen[remembered.cell_key] = 5
    visible = ResourceCellInfo(position=Coordinate(0, 0), visible=True, last_seen_tick=7)
    snapshot = _snapshot(tick=7, resource_cells={visible.position.cell_key: visible})
    merged = with_memory_resource_cells(snapshot, state)
    assert merged.resource_cells[visible.position.cell_key].visible is True
    remembered_cell = merged.resource_cells[remembered.cell_key]
    assert remembered_cell.visible is False
    assert remembered_cell.position == remembered
    assert remembered_cell.last_seen_tick == 5


def test_observe_exploration_removes_harvested_cell_from_memory() -> None:
    state = ExplorationState()
    resource = Coordinate(10, 0)
    state.cell_positions[resource.cell_key] = resource
    state.cell_last_seen[resource.cell_key] = 4
    state.prev_cargo["w1"] = 0
    unit = _worker("w1", 10, 0, cargo=1)
    snapshot = _snapshot(tick=5, units=(unit,))
    observe_exploration(snapshot, (), state)
    assert resource.cell_key not in state.cell_positions
    assert state.chunk_harvest_count[chunk_of(resource)] == 1
    assert state.chunk_next_refill_tick[chunk_of(resource)] == 8
    assert state.prev_cargo["w1"] == 1


def test_harvested_cell_tombstone_expires_without_readmission() -> None:
    state = ExplorationState()
    resource = Coordinate(10, 0)
    state.cell_positions[resource.cell_key] = resource
    state.cell_last_seen[resource.cell_key] = 4
    state.prev_cargo["w1"] = 0
    # Harvest at tick 5: refill boundary is tick 8.
    unit = _worker("w1", 10, 0, cargo=1)
    observe_exploration(_snapshot(tick=5, units=(unit,)), (), state)
    assert resource.cell_key not in state.cell_positions
    assert state.harvested_cells[resource.cell_key] == (resource, 8)
    # Before the boundary the tombstone persists.
    observe_exploration(_snapshot(tick=6, units=(unit,)), (), state)
    assert resource.cell_key not in state.cell_positions
    assert resource.cell_key in state.harvested_cells
    # R1a: at the boundary the tombstone simply expires. The cell is NOT
    # re-admitted — official replenishment places replacements at random
    # positions inside the chunk, so the old cell is almost always empty.
    observe_exploration(_snapshot(tick=8, units=(unit,)), (), state)
    assert resource.cell_key not in state.cell_positions
    assert resource.cell_key not in state.harvested_cells


def test_harvested_cells_stay_out_of_the_assignment_matrix() -> None:
    state = ExplorationState()
    resource = Coordinate(10, 0)
    state.harvested_cells[resource.cell_key] = (resource, 6)
    snapshot = _snapshot(tick=5, units=())
    merged = with_memory_resource_cells(snapshot, state)
    assert resource.cell_key not in merged.resource_cells


def test_reset_location_state_clears_harvested_cells() -> None:
    state = ExplorationState()
    state.harvested_cells["10,0"] = 8
    state.cell_positions["30,0"] = Coordinate(30, 0)
    state.reset_location_state()
    assert state.harvested_cells == {}
    assert state.cell_positions == {}


def test_chunk_recheck_ladder_anchor_center_then_corners() -> None:
    chunk = (1, 2)
    anchor = Coordinate(40, 80)
    ladder = _chunk_recheck_ladder(chunk, anchor)
    assert ladder[0] == anchor
    assert ladder[1] == chunk_center(chunk)
    assert set(ladder[2:]) == {
        Coordinate(32, 64),
        Coordinate(32, 95),
        Coordinate(63, 64),
        Coordinate(63, 95),
    }


def test_chunk_recheck_ladder_without_anchor_starts_at_center() -> None:
    ladder = _chunk_recheck_ladder((0, 0), None)
    assert ladder[0] == chunk_center((0, 0))
    assert len(ladder) == 5


def test_chunk_recheck_ladder_dedups_center_anchor() -> None:
    center = chunk_center((0, 0))
    ladder = _chunk_recheck_ladder((0, 0), center)
    assert ladder.count(center) == 1


def test_build_targets_spread_workers_across_due_chunk_ladder() -> None:
    unit_ids = ("w1", "w2", "w3")
    state = ExplorationState()
    anchor = Coordinate(100, 0)
    chunk = chunk_of(anchor)
    state.chunk_next_refill_tick[chunk] = 100
    state.chunk_anchor[chunk] = anchor
    snapshot = _snapshot(tick=100, units=tuple(_worker(unit_id, 0, 0) for unit_id in unit_ids))
    targets = build_exploration_targets(snapshot, state)
    ladder = _chunk_recheck_ladder(chunk, anchor)
    assert targets["w1"] == anchor
    assert targets["w2"] == chunk_center(chunk)
    assert targets["w3"] in set(ladder[2:])
    assert len(set(targets.values())) == len(unit_ids)


def test_observe_exploration_refutes_probed_empty_due_chunk() -> None:
    state = ExplorationState()
    chunk = (3, 0)
    state.chunk_next_refill_tick[chunk] = 8
    state.chunk_last_probe_tick[chunk] = 10
    observe_exploration(_snapshot(tick=10, units=()), (), state)
    assert state.chunk_refuted_tick[chunk] == 10


def test_observe_exploration_does_not_refute_before_probe() -> None:
    state = ExplorationState()
    chunk = (3, 0)
    state.chunk_next_refill_tick[chunk] = 8
    observe_exploration(_snapshot(tick=10, units=()), (), state)
    assert chunk not in state.chunk_refuted_tick


def test_observe_exploration_refutation_needs_probe_after_boundary() -> None:
    state = ExplorationState()
    chunk = (3, 0)
    state.chunk_next_refill_tick[chunk] = 8
    state.chunk_last_probe_tick[chunk] = 6
    observe_exploration(_snapshot(tick=10, units=()), (), state)
    assert chunk not in state.chunk_refuted_tick


def test_observe_exploration_clears_refutation_on_visible_resource() -> None:
    state = ExplorationState()
    chunk = (0, 0)
    state.chunk_refuted_tick[chunk] = 5
    cell = ResourceCellInfo(position=Coordinate(8, 8), visible=True, last_seen_tick=10)
    observe_exploration(_snapshot(tick=10, resource_cells={cell.position.cell_key: cell}), (), state)
    assert chunk not in state.chunk_refuted_tick


def test_refuted_chunk_memory_cells_stay_out_of_matrix() -> None:
    state = ExplorationState()
    remembered = Coordinate(30, 0)
    state.cell_positions[remembered.cell_key] = remembered
    state.cell_last_seen[remembered.cell_key] = 5
    state.chunk_refuted_tick[(0, 0)] = 10
    merged = with_memory_resource_cells(_snapshot(tick=10, units=()), state)
    assert remembered.cell_key not in merged.resource_cells


def test_reset_location_state_clears_refutation() -> None:
    state = ExplorationState()
    state.chunk_refuted_tick[(0, 0)] = 5
    state.reset_location_state()
    assert state.chunk_refuted_tick == {}


def test_build_targets_prefer_near_core_due_chunk() -> None:
    unit_id = "w1"
    state = ExplorationState()
    far_anchor = Coordinate(300, 0)
    near_anchor = Coordinate(100, 0)
    state.chunk_next_refill_tick[chunk_of(far_anchor)] = 100
    state.chunk_next_refill_tick[chunk_of(near_anchor)] = 100
    state.chunk_anchor[chunk_of(far_anchor)] = far_anchor
    state.chunk_anchor[chunk_of(near_anchor)] = near_anchor
    snapshot = _snapshot(tick=100, units=(_worker(unit_id, 0, 0),))
    targets = build_exploration_targets(snapshot, state)
    assert targets[unit_id] == near_anchor

