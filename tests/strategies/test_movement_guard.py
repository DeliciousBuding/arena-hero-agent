"""Movement-guard layer tests: pure escape mechanisms (not wired into composition)."""

from __future__ import annotations

from arena_hero_agent.domain import Coordinate, Direction
from arena_hero_agent.strategies.movement_guard import (
    DepositProgress,
    LoopTrail,
    MoveBackoffState,
    cargo_spin_self_heal,
    deposit_escape_needed,
    detect_spatial_loop,
    footprint_diameter,
    forced_escape_step,
    mark_loop_repath,
    observe_loop_position,
    record_deposit_repath,
    refresh_deposit_progress,
    should_pause_move,
    soft_obstacles_from_trail,
    update_move_backoff,
)


def _c(x: int, y: int) -> Coordinate:
    return Coordinate(x, y)


# --- Mechanism 1: MOVE_BLOCKED short-stop backoff -----------------------------


def test_backoff_blocks_after_three_failures() -> None:
    state: MoveBackoffState | None = None
    for tick in range(3):
        state = update_move_backoff(state, tick=tick, blocked=True)
    assert state is not None
    assert state.fail_streak == 3
    assert state.pause_until_tick == 2 + 2
    assert should_pause_move(state, tick=2) is True
    assert should_pause_move(state, tick=3) is True
    assert should_pause_move(state, tick=4) is False


def test_backoff_requires_three_failures() -> None:
    state: MoveBackoffState | None = None
    state = update_move_backoff(state, tick=0, blocked=True)
    state = update_move_backoff(state, tick=1, blocked=True)
    assert state.fail_streak == 2
    assert should_pause_move(state, tick=1) is False


def test_backoff_move_clears_streak() -> None:
    state: MoveBackoffState | None = None
    state = update_move_backoff(state, tick=0, blocked=True)
    state = update_move_backoff(state, tick=1, blocked=True)
    state = update_move_backoff(state, tick=2, blocked=True)
    state = update_move_backoff(state, tick=3, moved=True)
    assert state == MoveBackoffState()
    assert should_pause_move(state, tick=3) is False


def test_backoff_non_event_keeps_state() -> None:
    state = MoveBackoffState(fail_streak=2, pause_until_tick=-1)
    unchanged = update_move_backoff(state, tick=5)
    assert unchanged == state


# --- Mechanism 2: spatial-loop detection --------------------------------------


def test_footprint_diameter_empty() -> None:
    assert footprint_diameter([]) == 0


def test_footprint_diameter_manhattan_bbox() -> None:
    assert footprint_diameter([_c(0, 0), _c(2, 1)]) == 2 + 1


def test_loop_detects_static_streak() -> None:
    trail = LoopTrail()
    for _ in range(4):
        trail = observe_loop_position(trail, _c(0, 0))
    assert trail.static_ticks == 4
    assert detect_spatial_loop(trail) is True


def test_loop_detects_small_box() -> None:
    trail = LoopTrail()
    cycle = [_c(0, 0), _c(0, 1), _c(1, 1), _c(1, 0)]
    for position in cycle * 3:
        trail = observe_loop_position(trail, position)
    assert detect_spatial_loop(trail) is True


def test_loop_ignores_wide_movement() -> None:
    trail = LoopTrail()
    for x in range(12):
        trail = observe_loop_position(trail, _c(x, 0))
    assert detect_spatial_loop(trail) is False


def test_loop_ignores_net_progress_to_target() -> None:
    trail = LoopTrail()
    for position in [_c(0, 0), _c(1, 0), _c(0, 0), _c(1, 0), _c(2, 0)]:
        trail = observe_loop_position(trail, position)
    assert detect_spatial_loop(trail, target=_c(10, 0)) is False


def test_loop_cooldown_suppresses_detection() -> None:
    trail = LoopTrail(cooldown=2, static_ticks=4)
    assert detect_spatial_loop(trail) is False


def test_soft_obstacles_exclude_origin() -> None:
    trail = LoopTrail(history=(_c(0, 1), _c(0, 2), _c(0, 3)))
    soft = soft_obstacles_from_trail(trail, _c(0, 2))
    assert _c(0, 2) not in soft
    assert _c(0, 1) in soft
    assert _c(0, 3) in soft


def test_soft_obstacles_ban_visited_neighbors() -> None:
    trail = LoopTrail(history=(_c(0, 0), _c(0, 1)))
    soft = soft_obstacles_from_trail(trail, _c(0, 0))
    assert _c(0, 1) in soft


def test_mark_loop_repath_flips_side_and_cooldown() -> None:
    trail = LoopTrail(history=(_c(0, 0),), static_ticks=3, repath_side=0)
    marked = mark_loop_repath(trail, tick=7)
    assert marked.repath_side == 1
    assert marked.static_ticks == 0
    assert marked.cooldown == 5
    assert marked.last_repath_tick == 7


# --- Mechanism 3: return-to-core progress -------------------------------------


def test_deposit_progress_improvement_resets_streak() -> None:
    state = refresh_deposit_progress(None, 10, tick=0)
    assert state == DepositProgress(best_manhattan=10, last_improve_tick=0, repath_streak=0)
    state = record_deposit_repath(state, repathed=True)
    state = record_deposit_repath(state, repathed=True)
    assert state.repath_streak == 2
    state = refresh_deposit_progress(state, 8, tick=5)
    assert state == DepositProgress(best_manhattan=8, last_improve_tick=5, repath_streak=0)


def test_deposit_progress_rebaselines_when_farther() -> None:
    state = refresh_deposit_progress(None, 5, tick=0)
    state = refresh_deposit_progress(state, 10, tick=3)
    assert state == DepositProgress(best_manhattan=10, last_improve_tick=3, repath_streak=0)


def test_deposit_escape_on_stall() -> None:
    state = refresh_deposit_progress(None, 10, tick=0)
    assert deposit_escape_needed(state, tick=9) is False
    assert deposit_escape_needed(state, tick=10) is True


def test_deposit_escape_on_repath_streak() -> None:
    state = refresh_deposit_progress(None, 10, tick=0)
    for _ in range(3):
        state = record_deposit_repath(state, repathed=True)
    assert deposit_escape_needed(state, tick=1) is True


def test_record_deposit_repath_decays() -> None:
    state = DepositProgress(best_manhattan=10, last_improve_tick=0, repath_streak=2)
    state = record_deposit_repath(state, repathed=False)
    assert state.repath_streak == 1
    state = record_deposit_repath(state, repathed=False)
    state = record_deposit_repath(state, repathed=False)
    assert state.repath_streak == 0


def test_forced_escape_steps_toward_target() -> None:
    assert forced_escape_step(_c(0, 0), _c(3, 0), frozenset()) is Direction.EAST


def test_forced_escape_switches_side_when_primary_blocked() -> None:
    direction = forced_escape_step(_c(0, 0), _c(3, 0), frozenset({_c(1, 0)}))
    assert direction in (Direction.SOUTH, Direction.NORTH)


def test_forced_escape_none_at_target() -> None:
    assert forced_escape_step(_c(2, 2), _c(2, 2), frozenset()) is None


# --- Mechanism 4: cargo-spin Core self-heal -----------------------------------


def test_cargo_spin_heal_empty_cargo_false() -> None:
    positions = [_c(0, 0)] * 16
    assert cargo_spin_self_heal(positions, cargo=0, core_position=_c(10, 10)) is False


def test_cargo_spin_heal_insufficient_history_false() -> None:
    positions = [_c(0, 0)] * 15
    assert cargo_spin_self_heal(positions, cargo=1, core_position=_c(10, 10)) is False


def test_cargo_spin_heal_far_spinner_true() -> None:
    positions = [_c(0, 0), _c(0, 1), _c(1, 1), _c(1, 0)] * 4
    assert cargo_spin_self_heal(positions, cargo=1, core_position=_c(10, 10)) is True


def test_cargo_spin_heal_near_spinner_false() -> None:
    positions = [_c(0, 0), _c(0, 1), _c(1, 1), _c(1, 0)] * 4
    assert cargo_spin_self_heal(positions, cargo=1, core_position=_c(0, 0)) is False
