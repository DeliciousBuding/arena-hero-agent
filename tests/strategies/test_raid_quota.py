"""Raid-quota layer tests: pure accounting, formation wiring, and lifecycle."""

from __future__ import annotations

from arena_hero_agent.domain import Coordinate
from arena_hero_agent.strategies.raid_quota import (
    CORE_ASSAULT_RANGERS,
    CORE_ASSAULT_VANGUARDS,
    HOME_DEFENSE_RANGERS,
    HOME_DEFENSE_VANGUARDS,
    RaidState,
    ReplacementQueue,
    StationaryCore,
    StrikeGroup,
    UnitQuota,
    acquire_raid_target,
    clear_raid_target,
    core_assault_quota,
    pick_raid_target,
    raid_active,
    raid_fighters_ready,
    raid_guard_ids,
    recall_raid,
    reconcile_replacement_queue,
    replacement_gap_order,
    select_strike_group,
    strike_group_quota,
)
from arena_hero_agent.strategies.tactical_squads import TacticalSquad


def test_default_quota_constants() -> None:
    assert (HOME_DEFENSE_VANGUARDS, HOME_DEFENSE_RANGERS) == (2, 1)
    assert (CORE_ASSAULT_VANGUARDS, CORE_ASSAULT_RANGERS) == (1, 2)


def test_core_assault_quota_full_formation() -> None:
    assert core_assault_quota(4, 3) == UnitQuota(
        vanguard_count=CORE_ASSAULT_VANGUARDS,
        ranger_count=CORE_ASSAULT_RANGERS,
    )


def test_core_assault_quota_never_borrows_home_defense() -> None:
    assert core_assault_quota(2, 1) == UnitQuota(vanguard_count=0, ranger_count=0)


def test_core_assault_quota_partial_pool_still_reserves_home() -> None:
    assert core_assault_quota(3, 2) == UnitQuota(vanguard_count=1, ranger_count=1)


def test_core_assault_quota_insufficient_pool() -> None:
    assert core_assault_quota(1, 0) == UnitQuota(vanguard_count=0, ranger_count=0)


def test_core_assault_quota_custom_sizes() -> None:
    assert core_assault_quota(5, 4, home_vanguards=3, home_rangers=1) == UnitQuota(
        vanguard_count=1,
        ranger_count=2,
    )


def test_strike_group_quota_ranger_only_low_health() -> None:
    assert strike_group_quota(1, 4, 3) == UnitQuota(vanguard_count=0, ranger_count=1)


def test_strike_group_quota_two_rangers_no_overkill() -> None:
    assert strike_group_quota(2, 4, 4) == UnitQuota(vanguard_count=0, ranger_count=2)


def test_strike_group_quota_backfills_vanguards_from_remaining_hp() -> None:
    assert strike_group_quota(3, 4, 4) == UnitQuota(vanguard_count=1, ranger_count=2)


def test_strike_group_quota_short_pool_undercommits() -> None:
    assert strike_group_quota(3, 3, 3) == UnitQuota(vanguard_count=1, ranger_count=1)


def test_strike_group_quota_large_health_caps_to_available() -> None:
    assert strike_group_quota(5, 6, 4) == UnitQuota(vanguard_count=3, ranger_count=2)


def test_select_strike_group_takes_trailing_sorted_ids() -> None:
    assert select_strike_group(
        ("v2", "v1", "v3"),
        ("r3", "r1", "r2"),
        UnitQuota(vanguard_count=1, ranger_count=2),
    ) == StrikeGroup(vanguard_ids=("v3",), ranger_ids=("r2", "r3"))


def test_raid_guard_ids_returns_home_members() -> None:
    home = TacticalSquad(
        id="home",
        role="HOME_DEFENSE",
        index=0,
        vanguard_ids=("v1", "v2"),
        ranger_ids=("r1",),
    )
    assert raid_guard_ids(home) == frozenset({"v1", "v2", "r1"})


def test_raid_guard_ids_none() -> None:
    assert raid_guard_ids(None) == frozenset()


def test_raid_active_requires_enabled_members_no_recall() -> None:
    state = RaidState(enabled=True, vanguard_ids=frozenset({"v1"}))
    assert raid_active(state) is True


def test_raid_active_false_when_disabled_recalled_or_empty() -> None:
    assert raid_active(RaidState()) is False
    recalled = RaidState(enabled=True, recall=True, vanguard_ids=frozenset({"v1"}))
    assert raid_active(recalled) is False
    assert raid_active(RaidState(enabled=True)) is False


def test_recall_raid_clears_target() -> None:
    state = RaidState(
        enabled=True,
        vanguard_ids=frozenset({"v1"}),
        core_id="enemy-core",
        core_position=Coordinate(5, 5),
        acquired_tick=7,
    )
    recalled = recall_raid(state)
    assert recalled.recall is True
    assert recalled.core_id is None
    assert recalled.core_position is None
    assert recalled.acquired_tick is None
    assert recalled.vanguard_ids == state.vanguard_ids


def test_clear_and_acquire_raid_target() -> None:
    state = RaidState(enabled=True, vanguard_ids=frozenset({"v1"}), recall=True)
    acquired = acquire_raid_target(state, "core-1", Coordinate(9, 9), 12)
    assert acquired.core_id == "core-1"
    assert acquired.core_position == Coordinate(9, 9)
    assert acquired.acquired_tick == 12
    cleared = clear_raid_target(acquired)
    assert cleared.core_id is None
    assert cleared.recall is True
    assert cleared.vanguard_ids == frozenset({"v1"})


def test_pick_raid_target_prefers_nearest_confirmed_core() -> None:
    stationary = {
        "far": StationaryCore("far", Coordinate(20, 0), observations=3),
        "near": StationaryCore("near", Coordinate(3, 0), observations=3),
    }
    assert pick_raid_target(stationary, Coordinate(0, 0)) == Coordinate(3, 0)


def test_pick_raid_target_requires_min_observations() -> None:
    stationary = {
        "young": StationaryCore("young", Coordinate(2, 0), observations=2),
    }
    assert pick_raid_target(stationary, Coordinate(0, 0)) is None


def test_pick_raid_target_rejects_too_far_core() -> None:
    stationary = {
        "far": StationaryCore("far", Coordinate(41, 0), observations=3),
    }
    assert pick_raid_target(stationary, Coordinate(0, 0)) is None


def test_pick_raid_target_custom_thresholds() -> None:
    stationary = {
        "ok": StationaryCore("ok", Coordinate(5, 0), observations=2),
    }
    assert pick_raid_target(
        stationary,
        Coordinate(0, 0),
        min_observations=2,
        max_distance=5,
    ) == Coordinate(5, 0)


def test_raid_fighters_ready() -> None:
    assert raid_fighters_ready(3) is True
    assert raid_fighters_ready(2) is False
    assert raid_fighters_ready(1, min_fighters=1) is True


def test_reconcile_replacement_queue_enqueues_lost_roles() -> None:
    queue = ReplacementQueue()
    next_queue = reconcile_replacement_queue(
        {"v1": "vanguard", "r1": "ranger"},
        {"v1": "vanguard"},
        queue,
    )
    assert next_queue.to_mapping() == {"ranger": 1}


def test_reconcile_replacement_queue_production_fills_gap() -> None:
    next_queue = reconcile_replacement_queue(
        {},
        {"r2": "ranger"},
        ReplacementQueue({"ranger": 2}),
    )
    assert next_queue.to_mapping() == {"ranger": 1}


def test_reconcile_replacement_queue_drops_zero_counts() -> None:
    next_queue = reconcile_replacement_queue(
        {},
        {"r2": "ranger"},
        ReplacementQueue({"ranger": 1}),
    )
    assert next_queue.to_mapping() == {}


def test_replacement_gap_order_prioritizes_largest_gap_then_role_order() -> None:
    queue = ReplacementQueue({"vanguard": 1, "ranger": 3, "worker": 2})
    assert replacement_gap_order(queue, ("vanguard", "ranger", "worker")) == (
        "ranger",
        "worker",
        "vanguard",
    )
