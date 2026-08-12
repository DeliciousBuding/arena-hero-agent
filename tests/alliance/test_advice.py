"""W25 alliance advice: JS-parity number formatting, core-movement/threats, and
the standalone resurvey / gold-mine builders (TS ``alliance-advice.ts`` port).

The end-to-end composition (all 11 sections) is anchored field-for-field by
the loader golden parity suite (``test_golden_parity.py`` alliance_advice_*);
these unit tests pin the JS semantics and the pure builder contracts.
"""

from __future__ import annotations

from arena_hero_agent.alliance.advice import (
    AdviceCategory,
    build_alliance_advice_payload,
    build_gold_mine_advice,
    build_resurvey_advice,
    collect_core_threats,
    compute_core_movement,
)

NOW_MS = 1_752_000_000_000
ISO_AT = "2025-07-08T18:40:00.000Z"


def test_js_number_matches_string_number() -> None:
    from arena_hero_agent.alliance.advice import _js_number

    assert _js_number(3) == "3"
    assert _js_number(-12) == "-12"
    assert _js_number(3.5) == "3.5"
    assert _js_number(0) == "0"
    assert _js_number(100) == "100"


def test_js_round_half_up_toward_infinity() -> None:
    from arena_hero_agent.alliance.advice import _js_round

    assert _js_round(2.5) == 3
    assert _js_round(-2.5) == -2
    assert _js_round(0.4) == 0
    assert _js_round(1.0) == 1


def test_to_fixed_matches_number_prototype() -> None:
    from arena_hero_agent.alliance.advice import _to_fixed

    assert _to_fixed(1.005, 2) == "1.00"  # binary-double semantics like JS toFixed
    assert _to_fixed(1.81, 1) == "1.8"
    assert _to_fixed(2.9, 1) == "2.9"
    assert _to_fixed(-12.5, 0) == "-13"  # half away from zero via ROUND_HALF_UP on exact -12.5
    assert _to_fixed(0, 2) == "0.00"


def test_compute_core_movement_approaching() -> None:
    trail = [
        {"x": 50, "y": 0, "tick": 100},
        {"x": 30, "y": 0, "tick": 200},
        {"x": 10, "y": 0, "tick": 300},
    ]
    moved = compute_core_movement(trail, [0, 0])
    assert moved["direction"] == "approaching"
    assert moved["distToCoreCells"] == 10
    assert moved["speedCellsPerTick"] == 0.2


def test_compute_core_movement_retreating_and_unknown() -> None:
    retreat = [
        {"x": 20, "y": 20, "tick": 100},
        {"x": 40, "y": 20, "tick": 200},
    ]
    assert compute_core_movement(retreat, [0, 0])["direction"] == "retreating"
    assert compute_core_movement([{"x": 1, "y": 1, "tick": 100}], [0, 0])["direction"] == "unknown"
    assert compute_core_movement(retreat, None)["direction"] == "unknown"


def test_collect_core_threats_approaching_proximity_stale() -> None:
    trails = [
        {
            "username": "raider",
            "trail": [
                {"x": 80, "y": 0, "tick": 100},
                {"x": 60, "y": 0, "tick": 200},
                {"x": 40, "y": 0, "tick": 300},
            ],
        },
        {"username": "lurker", "trail": [{"x": 30, "y": 0, "tick": 100}]},
        {
            "username": "ghost",
            "trail": [{"x": 10, "y": 10, "tick": 100}],
        },
    ]
    threats = collect_core_threats(trails, [0, 0], current_tick=500)
    kinds = {item["username"]: item["kind"] for item in threats}
    assert kinds == {"raider": "approaching", "lurker": "proximity", "ghost": "proximity"}
    by_name = {item["username"]: item for item in threats}
    assert by_name["raider"]["distCells"] == 40
    assert by_name["raider"]["stale"] is False
    # ghost is 400 ticks old (> 5000 default? no: age 400 < 5000) so not stale by default
    assert by_name["ghost"]["stale"] is False


def test_collect_core_threats_stale_flag_and_radius_limits() -> None:
    trails = [{"username": "old", "trail": [{"x": 5, "y": 5, "tick": 100}]}]
    stale = collect_core_threats(trails, [0, 0], current_tick=6000)
    assert stale[0]["stale"] is True
    far = collect_core_threats(
        [{"username": "far", "trail": [{"x": 500, "y": 500, "tick": 100}]}],
        [0, 0],
        current_tick=500,
    )
    assert far == []


def test_build_resurvey_advice_medium_when_stale() -> None:
    targets = [
        {
            "key": "5,5",
            "nearCoreOf": "t1",
            "distChunks": 2,
            "lastSeenTick": 0,
            "stalenessTicks": 5000,
        },
        {
            "key": "6,5",
            "nearCoreOf": "t1",
            "distChunks": 3,
            "lastSeenTick": 100,
            "stalenessTicks": 4900,
        },
    ]
    advice = build_resurvey_advice(targets, now_ms=NOW_MS)
    assert len(advice) == 1
    item = advice[0]
    assert item["severity"] == "MEDIUM"
    assert item["category"] == AdviceCategory.INTEL.value
    assert item["tenant"] == "t1"
    assert "5000" in item["title"]
    assert item["confidence"] == 0.7
    assert item["at"] == ISO_AT
    assert item["evidence"] == [
        {"type": "survey", "tenant": "t1", "ref": "resurvey=5,5 stale=5000"}
    ]


def test_build_resurvey_advice_info_when_fresh() -> None:
    targets = [
        {
            "key": "3,3",
            "nearCoreOf": "t2",
            "distChunks": 1,
            "lastSeenTick": 4000,
            "stalenessTicks": 1000,
        }
    ]
    advice = build_resurvey_advice(targets, now_ms=NOW_MS)
    assert advice[0]["severity"] == "INFO"


def test_build_gold_mine_advice_skips_zero_and_empty() -> None:
    tenants = {
        "t1": {"topMines": {"byAmount": [{"cell": "5,5", "harvestAmount": 0, "harvestOk": 2}]}},
        "t2": {"topMines": {"byAmount": []}},
    }
    assert build_gold_mine_advice(tenants, now_ms=NOW_MS) == []


def test_build_gold_mine_advice_medium_weighting() -> None:
    tenants = {
        "t3": {
            "topMines": {
                "byAmount": [{"cell": "60,60", "harvestAmount": 40, "harvestOk": 2, "activity": 1}]
            }
        }
    }
    advice = build_gold_mine_advice(tenants, now_ms=NOW_MS)
    assert len(advice) == 1
    item = advice[0]
    assert item["severity"] == "MEDIUM"
    assert item["category"] == AdviceCategory.INTEL.value
    assert item["tenant"] == "t3"
    assert "60,60" in item["title"]
    assert "40" in item["title"]
    assert item["weight"] == -(40 * 100 + 1000)
    assert item["confidence"] == 0.75
    assert item["evidence"] == [{"type": "survey", "tenant": "t3", "ref": "gold=60,60 amount=40"}]


def _empty_inputs() -> dict:
    return {
        "snapshot": {},
        "survey": {},
        "leaderboard": None,
        "enemy_heat": {},
        "mine_patterns": {},
        "mine_utilization": {},
        "decision_trends": {},
        "human_conflict": {},
        "mining_effectiveness": {},
        "exploration": {},
        "core_trails": {},
    }


def test_payload_empty_data_is_fail_open() -> None:
    payload = build_alliance_advice_payload(now_ms=NOW_MS, **_empty_inputs())
    assert payload["generatedAt"] == ISO_AT
    assert payload["cachedAt"] == ISO_AT
    assert payload["advice"] == []
    assert payload["dedupCount"] == 0
    assert payload["avgConfidence"] == 0
    assert payload["summary"] == {"critical": 0, "high": 0, "medium": 0, "info": 0}


def test_payload_sort_dedup_and_limit() -> None:
    inputs = _empty_inputs()
    inputs["snapshot"] = {
        "members": {
            "t1": {
                "tenantId": "t1",
                "tick": 5000,
                "resources": 8,
                "resourceCapacity": 100,
                "population": 5,
                "workers": 2,
                "vanguards": 2,
                "rangers": 1,
                "carriedResources": 8,
                "core": {
                    "id": "core-t1",
                    "position": [0, 0],
                    "hp": 500,
                    "shield": 100,
                    "moving": False,
                },
            }
        },
        "sightings": [],
        "threatSummaries": [],
        "currentTick": 5000,
    }
    payload = build_alliance_advice_payload(now_ms=NOW_MS, **inputs)
    assert payload["advice"] == [
        {
            "severity": "HIGH",
            "category": AdviceCategory.ECONOMY.value,
            "tenant": "t1",
            "title": "t1 核心资源 8 濒危",
            "detail": "人口 5（工2/锋2/射1），携带 8",
            "action": "安排采集优先，暂停非必要 spawn",
            "weight": -8,
            "confidence": 0.9,
            "evidence": [{"type": "world", "tenant": "t1", "ref": "res=8 pop=5"}],
            "at": ISO_AT,
        }
    ]
    assert payload["summary"] == {"critical": 0, "high": 1, "medium": 0, "info": 0}
    assert payload["dedupCount"] == 0
    assert payload["avgConfidence"] == 0.9


def test_payload_zero_combat_emits_one_item_per_member() -> None:
    """Section 2 folds every adjacent enemy core into a single per-member item
    (the (category, tenant, title) dedup key therefore stays collision-free for
    duplicate-free inputs; the dedup pass is exercised end-to-end by the golden
    parity suite where the TS oracle agrees on dedupCount=0)."""
    inputs = _empty_inputs()
    inputs["snapshot"] = {
        "members": {
            "t1": {
                "tenantId": "t1",
                "tick": 5000,
                "resources": 40,
                "resourceCapacity": 100,
                "population": 3,
                "workers": 3,
                "vanguards": 0,
                "rangers": 0,
                "carriedResources": 0,
                "core": {
                    "id": "core-t1",
                    "position": [0, 0],
                    "hp": 500,
                    "shield": 100,
                    "moving": False,
                },
            }
        },
        "sightings": [
            {
                "key": "CORE:a",
                "kind": "CORE",
                "position": [10, 0],
                "lastSeenTick": 4999,
                "ownerUsername": "alpha",
                "sourceTenant": "t1",
            },
            {
                "key": "CORE:b",
                "kind": "CORE",
                "position": [0, 12],
                "lastSeenTick": 4998,
                "ownerUsername": "beta",
                "sourceTenant": "t1",
            },
        ],
        "threatSummaries": [],
        "currentTick": 5000,
    }
    payload = build_alliance_advice_payload(now_ms=NOW_MS, **inputs)
    military = [a for a in payload["advice"] if a["category"] == AdviceCategory.MILITARY.value]
    assert len(military) == 1
    assert payload["dedupCount"] == 0
    assert military[0]["detail"].startswith("2 个敌核")
