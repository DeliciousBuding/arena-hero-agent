"""Deterministic movement-escape guards (research layer; not wired in yet).

Four self-contained, pure functions plus frozen state records that absorb the
third-party "movement robustness" tactics. They are intentionally **not**
imported by :mod:`arena_hero_agent.strategies` or registered in
``variant_registry``; the main session will later wire them into
``ComposedDecider``. Nothing in this module depends on the planning/strategy
wiring layers: it only uses the pure domain primitives ``Coordinate``,
``Direction`` and ``manhattan``.

Public API by mechanism
-----------------------

1. **MOVE_BLOCKED short-stop backoff** (ref ``arena-evolve/strategies/heuristic.py``)
   - :class:`MoveBackoffState` -- frozen per-unit streak/pause record.
   - :func:`update_move_backoff` -- fold one tick of move events into the record.
   - :func:`should_pause_move` -- whether the unit should WAIT this tick.

2. **Spatial-loop detection + forced repath** (ref ``arena-hero-tactic/bot/pathing.py``)
   - :class:`LoopTrail` -- frozen per-unit footprint/cooldown/side record.
   - :func:`observe_loop_position` -- append a position and advance counters.
   - :func:`detect_spatial_loop` -- true when recent cells stay inside a small box.
   - :func:`soft_obstacles_from_trail` -- turn recent footprint into soft obstacles.
   - :func:`mark_loop_repath` -- flip side, clear direction stickiness, start cooldown.
   - :func:`footprint_diameter` -- Manhattan bounding-box diameter of a trace.

3. **Return-to-core progress + forced escape** (ref ``arena-hero-tactic/bot/economy.py``)
   - :class:`DepositProgress` -- frozen best-distance/progress record.
   - :func:`refresh_deposit_progress` -- fold in the current distance to Core.
   - :func:`deposit_escape_needed` -- whether to force an escape step.
   - :func:`record_deposit_repath` -- update the consecutive-repath streak.
   - :func:`forced_escape_step` -- deterministic one-step side switch.

4. **Cargo spin Core self-heal** (ref ``arena-hero-clone-waaiging/arena_hero_strategy.py``)
   - :func:`cargo_spin_self_heal` -- whether Core should migrate toward a loaded,
     spinning, distant worker.

All functions are deterministic (no random, no wall clock, no I/O). Every
threshold is a keyword argument with a default aligned to the reference
constants, so the integrator can pass the exact values observed in production.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from arena_hero_agent.domain import Coordinate, Direction, manhattan

# --- Mechanism 1: MOVE_BLOCKED short-stop backoff -----------------------------

DEFAULT_BLOCKED_STREAK: Final = 3
DEFAULT_BLOCKED_PAUSE_TICKS: Final = 2

# --- Mechanism 2: spatial-loop detection --------------------------------------

DEFAULT_LOOP_WINDOW: Final = 12
DEFAULT_LOOP_MIN_UNIQUE: Final = 4
DEFAULT_LOOP_BBOX_MAX: Final = 3
DEFAULT_LOOP_STATIC_TICKS: Final = 4
DEFAULT_LOOP_REPATH_COOLDOWN: Final = 5
DEFAULT_TRAIL_KEEP_LAST: Final = 6

# --- Mechanism 3: return-to-core progress -------------------------------------

DEFAULT_DEPOSIT_STALL_TICKS: Final = 10
DEFAULT_DEPOSIT_REPATH_STREAK: Final = 3

# --- Mechanism 4: cargo-spin Core self-heal -----------------------------------

DEFAULT_CARGO_SPIN_TICKS: Final = 16
DEFAULT_CARGO_SPIN_BUDGET: Final = 6
DEFAULT_CARGO_CORE_DISTANCE: Final = 6

_CARDINAL_DIRECTIONS: Final = (Direction.EAST, Direction.WEST, Direction.SOUTH, Direction.NORTH)


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _non_negative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


@dataclass(frozen=True, slots=True)
class MoveBackoffState:
    """Per-unit short-stop state for consecutive ``MOVE_BLOCKED`` events.

    ``pause_until_tick`` is an exclusive upper bound: the unit pauses while
    ``tick < pause_until_tick``. ``-1`` means no active pause.
    """

    fail_streak: int = 0
    pause_until_tick: int = -1


def update_move_backoff(
    state: MoveBackoffState | None,
    *,
    tick: int,
    moved: bool = False,
    blocked: bool = False,
    blocked_streak: int = DEFAULT_BLOCKED_STREAK,
    pause_ticks: int = DEFAULT_BLOCKED_PAUSE_TICKS,
) -> MoveBackoffState:
    """Return the next backoff state after one tick of movement events.

    A successful ``moved`` clears the streak and any pause. A ``blocked`` tick
    advances the consecutive-failure streak; reaching ``blocked_streak`` starts
    a ``pause_ticks``-tick short stop to break synchronous mutual blocking.
    """

    current = state if state is not None else MoveBackoffState()
    blocked_streak = _positive_int("blocked_streak", blocked_streak)
    pause_ticks = _positive_int("pause_ticks", pause_ticks)
    _non_negative_int("tick", tick)

    if moved:
        return MoveBackoffState()
    if blocked:
        fail_streak = current.fail_streak + 1
        if fail_streak >= blocked_streak:
            return MoveBackoffState(
                fail_streak=fail_streak,
                pause_until_tick=tick + pause_ticks,
            )
        return MoveBackoffState(
            fail_streak=fail_streak,
            pause_until_tick=current.pause_until_tick,
        )
    return current


def should_pause_move(state: MoveBackoffState | None, *, tick: int) -> bool:
    """Return whether the unit should WAIT this tick under the short-stop rule."""

    _non_negative_int("tick", tick)
    return state is not None and tick < state.pause_until_tick


@dataclass(frozen=True, slots=True)
class LoopTrail:
    """Frozen per-unit spatial footprint for loop detection and repath side.

    ``history`` holds the most recent positions (oldest first). ``cooldown``
    suppresses loop detection after a forced repath. ``repath_side`` is ``0``
    or ``1`` and alternates to bias the escape direction.
    """

    history: tuple[Coordinate, ...] = ()
    static_ticks: int = 0
    last_pos: Coordinate | None = None
    cooldown: int = 0
    repath_side: int = 0
    last_repath_tick: int = -1


def observe_loop_position(
    trail: LoopTrail,
    position: Coordinate,
    *,
    window: int = DEFAULT_LOOP_WINDOW,
) -> LoopTrail:
    """Return ``trail`` advanced by one observed position.

    Appends ``position`` (trimming to the last ``window`` cells), updates the
    same-cell ``static_ticks`` streak, and decays ``cooldown`` by one.
    """

    window = _positive_int("window", window)
    history = trail.history + (position,)
    if len(history) > window:
        history = history[-window:]
    if trail.last_pos is not None and trail.last_pos == position:
        static_ticks = trail.static_ticks + 1
    else:
        static_ticks = 1
    cooldown = trail.cooldown - 1 if trail.cooldown > 0 else 0
    return LoopTrail(
        history=history,
        static_ticks=static_ticks,
        last_pos=position,
        cooldown=cooldown,
        repath_side=trail.repath_side,
        last_repath_tick=trail.last_repath_tick,
    )


def footprint_diameter(positions: Sequence[Coordinate]) -> int:
    """Return the Manhattan bounding-box diameter (``span_x + span_y``)."""

    if not positions:
        return 0
    xs = [position.x for position in positions]
    ys = [position.y for position in positions]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def detect_spatial_loop(
    trail: LoopTrail,
    *,
    target: Coordinate | None = None,
    window: int = DEFAULT_LOOP_WINDOW,
    min_unique: int = DEFAULT_LOOP_MIN_UNIQUE,
    bbox_diameter_max: int = DEFAULT_LOOP_BBOX_MAX,
    static_ticks: int = DEFAULT_LOOP_STATIC_TICKS,
) -> bool:
    """Return whether the trail is confined to a small repeated area.

    Triggers when the same cell is held for ``static_ticks`` ticks, or when the
    recent ``window`` covers no more than ``min_unique`` distinct cells within a
    ``bbox_diameter_max`` diameter and (with a ``target``) shows no net progress.
    """

    window = _positive_int("window", window)
    min_unique = _positive_int("min_unique", min_unique)
    bbox_diameter_max = _non_negative_int("bbox_diameter_max", bbox_diameter_max)
    static_ticks = _positive_int("static_ticks", static_ticks)

    if trail.cooldown > 0:
        return False
    if trail.static_ticks >= max(1, static_ticks):
        return True

    history = list(trail.history)
    if len(history) < max(4, window // 2):
        return False
    recent = history[-window:] if len(history) >= window else history
    if len(set(recent)) > min_unique:
        return False
    if footprint_diameter(recent) > bbox_diameter_max:
        return False

    if target is not None and len(recent) >= 3:
        first_distance = manhattan(recent[0], target)
        last_distance = manhattan(recent[-1], target)
        if first_distance - last_distance >= 2:
            return False
    return True


def soft_obstacles_from_trail(
    trail: LoopTrail,
    origin: Coordinate,
    *,
    keep_last: int = DEFAULT_TRAIL_KEEP_LAST,
    ban_origin_neighbors: bool = True,
) -> frozenset[Coordinate]:
    """Return recent footprint cells to treat as soft obstacles.

    The origin itself is never blocked. When ``ban_origin_neighbors`` is set,
    previously visited cardinal neighbors of ``origin`` are also blocked to stop
    wall-side oscillation.
    """

    keep_last = _positive_int("keep_last", keep_last)
    soft: set[Coordinate] = set()
    for position in trail.history[-keep_last:]:
        if position != origin:
            soft.add(position)
    if ban_origin_neighbors:
        for neighbor in origin.neighbors():
            if neighbor in trail.history and neighbor != origin:
                soft.add(neighbor)
    return frozenset(soft)


def mark_loop_repath(
    trail: LoopTrail,
    tick: int,
    *,
    cooldown_ticks: int = DEFAULT_LOOP_REPATH_COOLDOWN,
) -> LoopTrail:
    """Return ``trail`` after a forced repath: flip side, reset static streak.

    The direction stickiness is implicitly cleared because ``repath_side``
    alternates and ``static_ticks`` returns to zero; ``cooldown`` suppresses
    re-triggering for ``cooldown_ticks``.
    """

    cooldown_ticks = _positive_int("cooldown_ticks", cooldown_ticks)
    _non_negative_int("tick", tick)
    return LoopTrail(
        history=trail.history,
        static_ticks=0,
        last_pos=trail.last_pos,
        cooldown=max(1, cooldown_ticks),
        repath_side=1 - trail.repath_side,
        last_repath_tick=tick,
    )


@dataclass(frozen=True, slots=True)
class DepositProgress:
    """Frozen return-to-core progress record.

    ``best_manhattan`` is the closest observed distance to Core so far,
    ``last_improve_tick`` is when it last strictly improved (or was reset), and
    ``repath_streak`` counts consecutive loop-repaths without an improvement.
    """

    best_manhattan: int
    last_improve_tick: int
    repath_streak: int = 0


def refresh_deposit_progress(
    state: DepositProgress | None,
    manhattan_to_core: int,
    tick: int,
) -> DepositProgress:
    """Fold this tick's distance to Core into the progress record.

    Strict improvement resets the repath streak; moving clearly farther than the
    best (by more than two cells) re-baselines the best distance so a stale close
    point cannot lock the escape.
    """

    distance = _non_negative_int("manhattan_to_core", manhattan_to_core)
    _non_negative_int("tick", tick)
    if state is None:
        return DepositProgress(best_manhattan=distance, last_improve_tick=tick)

    best = state.best_manhattan
    last_improve = state.last_improve_tick
    streak = state.repath_streak
    if distance < best:
        best = distance
        last_improve = tick
        streak = 0
    elif distance > best + 2:
        best = distance
        last_improve = tick
    return DepositProgress(
        best_manhattan=best,
        last_improve_tick=last_improve,
        repath_streak=streak,
    )


def deposit_escape_needed(
    state: DepositProgress,
    tick: int,
    *,
    stall_ticks: int = DEFAULT_DEPOSIT_STALL_TICKS,
    repath_streak_limit: int = DEFAULT_DEPOSIT_REPATH_STREAK,
) -> bool:
    """Return whether the return-to-core path is stuck enough to force an escape."""

    stall_ticks = _positive_int("stall_ticks", stall_ticks)
    repath_streak_limit = _positive_int("repath_streak_limit", repath_streak_limit)
    _non_negative_int("tick", tick)
    stalled = tick - state.last_improve_tick if tick >= state.last_improve_tick else 0
    return stalled >= stall_ticks or state.repath_streak >= repath_streak_limit


def record_deposit_repath(state: DepositProgress, *, repathed: bool) -> DepositProgress:
    """Return the progress record with its consecutive-repath streak updated."""

    streak = state.repath_streak + 1 if repathed else max(0, state.repath_streak - 1)
    return DepositProgress(
        best_manhattan=state.best_manhattan,
        last_improve_tick=state.last_improve_tick,
        repath_streak=streak,
    )


def forced_escape_step(
    origin: Coordinate,
    target: Coordinate,
    obstacles: frozenset[Coordinate],
    *,
    repath_side: int = 0,
) -> Direction | None:
    """Return one deterministic escape step that switches off the blocked axis.

    The primary axis is stepped only after the perpendicular escape candidates;
    among candidates the first step that does not move farther, is perpendicular,
    and minimizes distance to ``target`` wins. Returns ``None`` only when already
    at the target or fully walled in.
    """

    if origin == target:
        return None

    dx = target.x - origin.x
    dy = target.y - origin.y
    if dx != 0 and abs(dx) >= abs(dy):
        primary = Direction.EAST if dx > 0 else Direction.WEST
    elif dy != 0:
        primary = Direction.SOUTH if dy > 0 else Direction.NORTH
    else:
        primary = None

    if primary in (Direction.NORTH, Direction.SOUTH):
        perps: tuple[Direction, ...] = (
            (Direction.EAST, Direction.WEST)
            if repath_side % 2 == 0
            else (Direction.WEST, Direction.EAST)
        )
    elif primary in (Direction.EAST, Direction.WEST):
        perps = (
            (Direction.SOUTH, Direction.NORTH)
            if repath_side % 2 == 0
            else (Direction.NORTH, Direction.SOUTH)
        )
    else:
        perps = (Direction.EAST, Direction.WEST, Direction.SOUTH, Direction.NORTH)

    prefer: list[Direction] = list(perps)
    if primary is not None and primary not in prefer:
        prefer.append(primary)
    opposite = primary.opposite if primary is not None else None
    if opposite is not None and opposite not in prefer:
        prefer.append(opposite)
    for direction in _CARDINAL_DIRECTIONS:
        if direction not in prefer:
            prefer.append(direction)

    current_distance = manhattan(origin, target)
    best_direction: Direction | None = None
    best_key: tuple[int, int, int] | None = None
    for direction in prefer:
        nxt = origin.step(direction)
        if nxt in obstacles:
            continue
        next_distance = manhattan(nxt, target)
        farther = 1 if next_distance > current_distance else 0
        is_perpendicular = 0 if direction in perps else 1
        key = (farther, is_perpendicular, next_distance)
        if best_key is None or key < best_key:
            best_key = key
            best_direction = direction
    return best_direction


def cargo_spin_self_heal(
    positions: Sequence[Coordinate],
    cargo: int,
    core_position: Coordinate,
    *,
    spin_ticks: int = DEFAULT_CARGO_SPIN_TICKS,
    spin_budget: int = DEFAULT_CARGO_SPIN_BUDGET,
    core_distance_threshold: int = DEFAULT_CARGO_CORE_DISTANCE,
) -> bool:
    """Return whether Core should migrate toward a loaded spinning worker.

    A worker that is carrying cargo, whose recent ``spin_ticks`` positions cover
    no more than ``spin_budget`` distinct cells, and that is farther than
    ``core_distance_threshold`` from Core triggers a self-heal hint.
    """

    spin_ticks = _positive_int("spin_ticks", spin_ticks)
    spin_budget = _positive_int("spin_budget", spin_budget)
    core_distance_threshold = _positive_int("core_distance_threshold", core_distance_threshold)
    _non_negative_int("cargo", cargo)

    if cargo <= 0:
        return False
    recent = tuple(positions)[-spin_ticks:]
    if len(recent) < spin_ticks:
        return False
    if len(set(recent)) > spin_budget:
        return False
    return manhattan(core_position, recent[-1]) > core_distance_threshold


__all__ = [
    "DEFAULT_BLOCKED_PAUSE_TICKS",
    "DEFAULT_BLOCKED_STREAK",
    "DEFAULT_CARGO_CORE_DISTANCE",
    "DEFAULT_CARGO_SPIN_BUDGET",
    "DEFAULT_CARGO_SPIN_TICKS",
    "DEFAULT_DEPOSIT_REPATH_STREAK",
    "DEFAULT_DEPOSIT_STALL_TICKS",
    "DEFAULT_LOOP_BBOX_MAX",
    "DEFAULT_LOOP_MIN_UNIQUE",
    "DEFAULT_LOOP_REPATH_COOLDOWN",
    "DEFAULT_LOOP_STATIC_TICKS",
    "DEFAULT_LOOP_WINDOW",
    "DEFAULT_TRAIL_KEEP_LAST",
    "DepositProgress",
    "LoopTrail",
    "MoveBackoffState",
    "cargo_spin_self_heal",
    "deposit_escape_needed",
    "detect_spatial_loop",
    "footprint_diameter",
    "forced_escape_step",
    "mark_loop_repath",
    "observe_loop_position",
    "record_deposit_repath",
    "refresh_deposit_progress",
    "should_pause_move",
    "soft_obstacles_from_trail",
    "update_move_backoff",
]
