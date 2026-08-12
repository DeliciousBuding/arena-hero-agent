"""W21 alliance-defense: endangered / reinforce / formation / pocket semantics.

Expected values are transcribed from the TS ``packages/command-center/lib/
alliance-defense.ts`` semantics (read as the read-only oracle) and anchored
field-for-field by the golden parity suite (``test_golden_parity.py``
alliance_defense_basic).
"""

from __future__ import annotations

import pytest

from arena_hero_agent.alliance.defense import (
    ENDANGERED_COMBAT_MAX,
    DefenseCategory,
    DefenseMemberInput,
    DefenseSeverity,
    build_defense_coordination,
    build_defense_pocket_advice,
    build_defense_pockets,
    direction_of,
    endangered_of,
    suggested_raid_force,
)
from arena_hero_agent.domain import Coordinate

NOW_MS = 1_752_000_000_000


def member(tenant_id: str, **overrides) -> DefenseMemberInput:
    values: dict = {
        "core": Coordinate(0, 0),
        "military": 3,
        "status": "READY",
        "threat_score": 0,
    }
    values.update(overrides)
    return DefenseMemberInput(tenant_id=tenant_id, **values)


def categories(payload) -> list[str]:
    return [advice.category.value for advice in payload.advice]


def test_no_endangered_yields_only_formation() -> None:
    payload = build_defense_coordination(
        [
            member("t1", core=Coordinate(0, 0), military=4),
            member("t2", core=Coordinate(50, 0), military=3),
            member("t3", core=Coordinate(0, 50), military=2),
        ],
        now_ms=NOW_MS,
    )
    assert payload.endangered == ()
    kinds = categories(payload)
    assert "FORMATION" in kinds
    assert "ENDANGERED" not in kinds
    assert "REINFORCE" not in kinds


def test_weak_military_with_high_threat_endangered_plus_reinforce() -> None:
    payload = build_defense_coordination(
        [
            member("t1", core=Coordinate(0, 0), military=4),
            member("t2", core=Coordinate(300, 0), military=0, threat_score=8),
            member("t3", core=Coordinate(280, 0), military=5),
            member("t4", core=Coordinate(700, 0), military=6),
        ],
        now_ms=NOW_MS,
    )
    assert [entry.tenant_id for entry in payload.endangered] == ["t2"]
    endangered = next(a for a in payload.advice if a.category is DefenseCategory.ENDANGERED)
    assert endangered.severity is DefenseSeverity.CRITICAL
    assert "T2" in endangered.title
    reinforce = next(a for a in payload.advice if a.category is DefenseCategory.REINFORCE)
    assert reinforce.tenant == "t3"
    assert reinforce.related_tenants == ("t2",)
    assert "20" in reinforce.detail
    assert "t4" not in reinforce.detail.lower()


def test_respawning_critical_and_no_neighbor_no_reinforce() -> None:
    payload = build_defense_coordination(
        [
            member("t1", core=Coordinate(0, 0), military=0, status="RESPAWNING"),
            member("t2", core=Coordinate(900, 0), military=0, status="DEGRADED", threat_score=9),
        ],
        now_ms=NOW_MS,
    )
    endangered = next(a for a in payload.advice if a.category is DefenseCategory.ENDANGERED)
    assert endangered.severity is DefenseSeverity.CRITICAL
    assert "重生" in endangered.title
    assert all(a.category is not DefenseCategory.REINFORCE for a in payload.advice)
    assert [entry.tenant_id for entry in payload.endangered] == ["t1", "t2"]


def test_dispersed_formation_is_medium_with_contract_advice() -> None:
    payload = build_defense_coordination(
        [
            member("t1", core=Coordinate(0, 0)),
            member("t2", core=Coordinate(800, 0)),
            member("t3", core=Coordinate(0, 900)),
        ],
        now_ms=NOW_MS,
    )
    formation = next(a for a in payload.advice if a.category is DefenseCategory.FORMATION)
    assert formation.severity is DefenseSeverity.MEDIUM
    assert "收缩" in formation.detail


def test_tight_formation_is_info() -> None:
    payload = build_defense_coordination(
        [
            member("t1", core=Coordinate(0, 0)),
            member("t2", core=Coordinate(50, 0)),
            member("t3", core=Coordinate(0, 60)),
        ],
        now_ms=NOW_MS,
    )
    formation = next(a for a in payload.advice if a.category is DefenseCategory.FORMATION)
    assert formation.severity is DefenseSeverity.INFO
    assert "响应半径" in formation.detail


def test_endangered_boundary_weak_without_threat_is_safe() -> None:
    payload = build_defense_coordination(
        [
            member("t1", core=Coordinate(0, 0), military=ENDANGERED_COMBAT_MAX, threat_score=2),
            member("t2", core=Coordinate(100, 0), military=4),
        ],
        now_ms=NOW_MS,
    )
    assert payload.endangered == ()


def test_zero_military_is_unconditionally_endangered() -> None:
    payload = build_defense_coordination(
        [
            member("t3", core=Coordinate(0, 0), military=0, threat_score=0),
            member("t1", core=Coordinate(50, 0), military=8),
        ],
        now_ms=NOW_MS,
    )
    assert [entry.tenant_id for entry in payload.endangered] == ["t3"]
    endangered = next(a for a in payload.advice if a.category is DefenseCategory.ENDANGERED)
    assert "零军事" in endangered.title
    assert endangered.severity is DefenseSeverity.HIGH
    assert any(a.category is DefenseCategory.REINFORCE for a in payload.advice)


def test_threat_flank_safe_neighbor_preferred_over_flank_side() -> None:
    payload = build_defense_coordination(
        [
            member(
                "t2", core=Coordinate(0, 0), military=0, threat_score=8, threat_directions=("S",)
            ),
            member("t3", core=Coordinate(0, 80), military=6),
            member("t1", core=Coordinate(0, -150), military=6),
        ],
        now_ms=NOW_MS,
    )
    safe = next(a for a in payload.advice if a.id == "defense:reinforce:t3:t2")
    assert safe is not None
    assert "避开威胁锋面" in safe.detail
    assert all(a.id != "defense:reinforce:t1:t2" for a in payload.advice)


def test_flank_side_nearest_still_recommended_with_detour_note() -> None:
    payload = build_defense_coordination(
        [
            member(
                "t2", core=Coordinate(0, 0), military=0, threat_score=8, threat_directions=("S",)
            ),
            member("t1", core=Coordinate(0, -60), military=6),
            member("t3", core=Coordinate(0, 200), military=6),
        ],
        now_ms=NOW_MS,
    )
    reinforce = next(a for a in payload.advice if a.category is DefenseCategory.REINFORCE)
    assert reinforce.tenant == "t1"
    assert "威胁锋面" in reinforce.detail
    assert "绕行" in reinforce.detail


def test_direction_of_eight_sectors_dy_north() -> None:
    assert direction_of(Coordinate(0, 0), Coordinate(0, -100)) == "S"
    assert direction_of(Coordinate(0, 0), Coordinate(0, 100)) == "N"
    assert direction_of(Coordinate(0, 0), Coordinate(100, 0)) == "E"
    assert direction_of(Coordinate(0, 0), Coordinate(-100, 0)) == "W"
    assert direction_of(Coordinate(0, 0), Coordinate(100, 100)) == "NE"
    assert direction_of(Coordinate(0, 0), Coordinate(-100, -100)) == "SW"
    assert direction_of(Coordinate(0, 0), Coordinate(0, 0)) == "C"


def test_suggested_raid_force_quantification() -> None:
    assert suggested_raid_force(0, 5) == (2, 0)
    assert suggested_raid_force(4, 10) == (4, 2)
    assert suggested_raid_force(2, 1) == (1, 0)
    assert suggested_raid_force(2, 0) is None
    assert suggested_raid_force(0, 0) is None


def test_reinforce_advice_carries_force_quantification() -> None:
    payload = build_defense_coordination(
        [
            member("t2", core=Coordinate(0, 0), military=0, threat_score=8, threat_count=4),
            member("t1", core=Coordinate(100, 0), military=10),
        ],
        now_ms=NOW_MS,
    )
    reinforce = next(a for a in payload.advice if a.category is DefenseCategory.REINFORCE)
    assert "建议编成 4 Vanguard + 2 Ranger" in reinforce.detail
    values = dict(reinforce.evidence)
    assert values.get("建议编成", "").startswith("4V")


def test_pocket_cluster_threatens_two_members() -> None:
    members = [
        member("t1", core=Coordinate(0, 0)),
        member("t2", core=Coordinate(150, 0)),
        member("t3", core=Coordinate(800, 0)),
    ]
    enemy_cores = [
        _enemy_core("CORE:alpha", "alpha", Coordinate(60, 0), 100),
        _enemy_core("CORE:beta", "beta", Coordinate(70, 5), 100),
        _enemy_core("CORE:gamma", "gamma", Coordinate(80, -5), 100),
    ]
    pockets = build_defense_pockets(members, enemy_cores)
    assert len(pockets) == 1
    assert sorted(pockets[0].threatened_tenants) == ["t1", "t2"]
    assert len(pockets[0].enemy_cores) == 3
    assert pockets[0].min_distance <= 60
    advice = build_defense_pocket_advice(members, enemy_cores)
    assert len(advice) == 1
    assert advice[0].category is DefenseCategory.POCKET
    assert "T1/T2" in advice[0].title
    assert "协同设防" in advice[0].detail


def test_pocket_single_core_or_single_threat_no_pocket() -> None:
    members = [
        member("t1", core=Coordinate(0, 0)),
        member("t2", core=Coordinate(800, 0)),
    ]
    single = build_defense_pockets(
        members, [_enemy_core("CORE:alpha", "alpha", Coordinate(60, 0), 100)]
    )
    assert single == ()
    far = build_defense_pockets(
        members,
        [
            _enemy_core("CORE:alpha", "alpha", Coordinate(60, 0), 100),
            _enemy_core("CORE:beta", "beta", Coordinate(70, 5), 100),
        ],
    )
    assert far == ()


def test_pocket_scattered_cores_no_cluster() -> None:
    members = [
        member("t1", core=Coordinate(0, 0)),
        member("t2", core=Coordinate(150, 0)),
    ]
    pockets = build_defense_pockets(
        members,
        [
            _enemy_core("CORE:alpha", "alpha", Coordinate(60, 0), 100),
            _enemy_core("CORE:beta", "beta", Coordinate(200, 0), 100),
        ],
    )
    assert pockets == ()


def test_endangered_of_classification() -> None:
    assert endangered_of(member("t1", status="RESPAWNING")) == (True, "respawn")
    assert endangered_of(member("t1", military=0, threat_score=0)) == (True, "zero")
    assert endangered_of(member("t1", military=1, threat_score=6)) == (True, "weak")
    assert endangered_of(member("t1", military=1, threat_score=2)) == (False, "")
    assert endangered_of(member("t1", military=3, threat_score=20)) == (False, "")


def test_advice_sorted_by_severity_then_id() -> None:
    payload = build_defense_coordination(
        [
            member("t2", core=Coordinate(0, 0), military=0, threat_score=8),
            member("t4", core=Coordinate(50, 0), military=0, status="RESPAWNING"),
            member("t1", core=Coordinate(200, 0), military=5),
            member("t3", core=Coordinate(0, 200), military=5),
        ],
        now_ms=NOW_MS,
    )
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "INFO": 3}
    severities = [advice.severity.value for advice in payload.advice]
    assert severities == sorted(severities, key=lambda value: order[value])
    for severity in order:
        ids = [advice.id for advice in payload.advice if advice.severity.value == severity]
        assert ids == sorted(ids)


def _enemy_core(key: str, owner: str, position: Coordinate, tick: int):
    from arena_hero_agent.alliance.defense import PocketEnemyCore

    return PocketEnemyCore(key=key, owner=owner, position=position, last_seen_tick=tick)


def test_validation_rejects_bad_inputs() -> None:
    with pytest.raises(TypeError):
        DefenseMemberInput(tenant_id="", core=None, military=1, status="READY")
    with pytest.raises(ValueError):
        member("t1", military=-1)
