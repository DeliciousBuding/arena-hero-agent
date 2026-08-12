"""Lifecycle audit projection tests (W44 wave 6).

Pins the pure ``aggregate_lifecycle`` semantics (synthetic normalized events,
no live data), the ``load_lifecycle_audit`` loader against a materialized
Command Center data root (calibration cases + survey-db unit_lifecycle /
core_spends / notable_events), and the wired ``/api/audit/lifecycle`` route
with empty-root fail-open (200, never 500).
"""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.api import ApiRequest, CommandCenterApp
from arena_hero_agent.command_center.projections import (
    aggregate_lifecycle,
    load_lifecycle_audit,
)
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

NOW_MS = 1_752_000_000_000


def _ev(
    kind: str,
    tick: int,
    *,
    actor: str | None = None,
    position: list[int] | None = None,
    amount: int | None = None,
    values: dict | None = None,
) -> dict:
    merged = dict(values or {})
    if amount is not None:
        merged.setdefault("amount", amount)
    return {
        "tick": tick,
        "kind": kind,
        "actor": actor,
        "target": None,
        "reason": None,
        "position": position,
        "amount": amount,
        "hp": None,
        "source": None,
        "capacity": None,
        "destroyedBy": merged.get("destroyed_by"),
        "destination": None,
        "values": merged,
    }


def test_aggregate_worker_role_and_consumption() -> None:
    events = [
        _ev("UNIT_MOVE_SUCCEEDED", 10, actor="u1", position=[1, 1]),
        _ev("HARVEST_SUCCEEDED", 11, actor="u1", position=[5, 5], amount=4),
        _ev("HARVEST_FAILED", 12, actor="u1", position=[5, 5]),
        _ev("DEPOSIT_SUCCEEDED", 13, actor="u1", amount=4),
        _ev("UNIT_DESTROYED", 14, actor="u1", values={"destroyed_by": "enemy-1"}),
    ]
    payload = aggregate_lifecycle("t1", "run-1", events, now_ms=NOW_MS)
    assert payload["tenant"] == "t1"
    assert payload["runId"] == "run-1"
    assert payload["window"] == {
        "fromTick": 10,
        "toTick": 14,
        "cases": 0,
        "events": 5,
    }
    unit = payload["units"][0]
    assert unit["actor"] == "u1"
    assert unit["role"] == "worker"
    assert unit["alive"] is False
    assert unit["destroyedAtTick"] == 14
    assert unit["destroyedBy"] == "enemy-1"
    assert unit["harvest"] == {"ok": 1, "fail": 1, "amount": 4}
    assert unit["deposit"] == {"ok": 1, "fail": 0, "amount": 4}
    assert unit["moves"] == {"ok": 1, "fail": 0}
    assert unit["lastPosition"] == [5, 5]  # last position-carrying event
    assert payload["consumption"]["harvestOk"] == 1
    assert payload["consumption"]["harvestFail"] == 1
    assert payload["consumption"]["harvestAmount"] == 4
    assert payload["consumption"]["depositAmount"] == 4
    assert payload["consumption"]["unitDestroyed"] == 1
    assert payload["consumption"]["destroyedByEnemy"] == 1
    mine = payload["mines"][0]
    assert mine["cell"] == "5,5"
    assert mine["harvestCount"] == 1
    assert mine["harvestFailCount"] == 1
    assert mine["harvestAmount"] == 4
    assert mine["active"] is True


def test_aggregate_combat_role_and_core() -> None:
    events = [
        _ev("SHOT_HIT", 20, actor="c1", amount=3),
        _ev("SHOT_MISSED", 21, actor="c1"),
        _ev("CORE_DAMAGED", 22, actor="core-1", amount=5),
        _ev("CORE_HEAL_SUCCEEDED", 23, actor="core-1"),
        _ev("CORE_MOVE_SUCCEEDED", 24, actor="core-1", position=[3, 3]),
    ]
    payload = aggregate_lifecycle("t2", "run-2", events, now_ms=NOW_MS)
    combat = [u for u in payload["units"] if u["actor"] == "c1"][0]
    assert combat["role"] == "combat"
    assert combat["combat"] == {
        "shotsHit": 1,
        "shotsMissed": 1,
        "blocked": 0,
        "sweepsResolved": 0,
        "damageDealt": 3,
    }
    core = payload["core"]
    assert core is not None
    assert core["actor"] == "core-1"
    assert core["damageTaken"] == 5
    assert core["damageEvents"] == 1
    assert core["healOk"] == 1
    assert core["moveOk"] == 1
    assert core["lastPosition"] == [3, 3]
    assert payload["consumption"]["coreDamageTaken"] == 5


def test_aggregate_refill_gap_and_stale_mine() -> None:
    events = [
        _ev("HARVEST_SUCCEEDED", 100, actor="u1", position=[9, 9], amount=1),
        _ev("HARVEST_SUCCEEDED", 140, actor="u1", position=[9, 9], amount=1),
        _ev("HARVEST_SUCCEEDED", 180, actor="u1", position=[9, 9], amount=1),
        _ev("HARVEST_SUCCEEDED", 300, actor="u1", position=[8, 8], amount=1),
    ]
    payload = aggregate_lifecycle("t3", None, events, now_ms=NOW_MS)
    by_cell = {m["cell"]: m for m in payload["mines"]}
    mine = by_cell["9,9"]
    assert mine["refillGapTicks"] == 40
    # lastSeen 180 vs toTick 300: 180 < 295 -> stale (inactive).
    assert mine["active"] is False
    other = by_cell["8,8"]
    # lastSeen 300 vs toTick 300: 300 >= 300 - 5 -> still active.
    assert other["active"] is True
    assert other["harvestCount"] == 1
    assert other["refillGapTicks"] is None


def _loader_fixture() -> dict:
    """Calibration events + survey-db backfill rows for one tenant."""
    return {
        "worlds": {
            "t1": {
                "cases": [
                    {
                        "tick": 100,
                        "state": {
                            "events": [
                                {
                                    "event_type": "UNIT_MOVE_SUCCEEDED",
                                    "actor_id": "u1",
                                    "tick": 100,
                                    "position": [1, 1],
                                },
                                {
                                    "event_type": "HARVEST_SUCCEEDED",
                                    "actor_id": "u1",
                                    "tick": 101,
                                    "position": [5, 5],
                                    "values": {"amount": 4},
                                },
                                {
                                    "event_type": "UNIT_DESTROYED",
                                    "actor_id": "u1",
                                    "tick": 102,
                                    "values": {"destroyed_by": "enemy-1"},
                                },
                            ]
                        },
                    },
                    {
                        "tick": 200,
                        "state": {
                            "events": [
                                {
                                    "event_type": "CORE_DAMAGED",
                                    "actor_id": "core-1",
                                    "tick": 200,
                                    "values": {"damage": 7},
                                }
                            ]
                        },
                    },
                ]
            }
        },
        "survey": {
            "t1": {
                "unitLifecycle": [
                    {
                        "unit_id": "u1",
                        "unit_type": "WORKER",
                        "birth_tick": 50,
                        "birth_pos": "1,1",
                        "death_tick": 102,
                        "death_pos": "2,2",
                        "death_reason": "enemy-1",
                        "last_seen_tick": 102,
                        "last_seen_pos": "2,2",
                        "current_state": "dead",
                    }
                ],
                "coreSpends": [
                    {
                        "kind": "spawn",
                        "tick": 10,
                        "amount": 5,
                        "unit_type": "WORKER",
                        "unit_id": "u1",
                    },
                    {
                        "kind": "spawn",
                        "tick": 20,
                        "amount": 8,
                        "unit_type": "RANGER",
                        "unit_id": "u2",
                    },
                ],
                "notableEvents": [
                    {
                        "tenant": "t1",
                        "tick": 300,
                        "event_type": "CORE_RESOURCES_CAPTURED",
                        "amount": 12,
                    },
                    {"tenant": "t1", "tick": 400, "event_type": "CORE_DESTROYED"},
                ],
            }
        },
    }


def test_load_lifecycle_audit_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_loader_fixture(), tmp_path)
    payload = load_lifecycle_audit(root, "t1", now_ms=NOW_MS)
    assert payload["tenant"] == "t1"
    assert payload["window"]["cases"] == 2
    unit = payload["units"][0]
    assert unit["unitType"] == "WORKER"
    assert unit["alive"] is False
    assert unit["destroyedAtTick"] == 102
    assert unit["destroyedBy"] == "enemy-1"
    assert payload["consumption"]["spends"] == {
        "byKind": {"spawn": 13},
        "byType": {"WORKER": 5, "RANGER": 8},
        "total": 13,
    }
    core = payload["core"]
    assert core is not None
    assert core["captures"] == {"count": 1, "amount": 12}
    assert core["capturedResources"] == 12
    assert core["destroyed"] is True
    assert core["damageTaken"] == 7  # event-window CORE_DAMAGED plus no notable damage
    assert payload["generatedAt"] == "2025-07-08T18:40:00.000Z"


def test_load_lifecycle_audit_all_tenants(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_loader_fixture(), tmp_path)
    payload = load_lifecycle_audit(root, "all", now_ms=NOW_MS)
    assert set(payload) == {"t1", "t2", "t3", "t4"}
    assert payload["t1"]["units"][0]["actor"] == "u1"
    assert payload["t2"]["units"] == []


def test_load_lifecycle_audit_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_lifecycle_audit(tmp_path, "t1", now_ms=NOW_MS)
    assert payload["window"] == {"fromTick": None, "toTick": None, "cases": 0, "events": 0}
    assert payload["units"] == []
    assert payload["mines"] == []
    assert payload["core"] is None
    assert payload["consumption"]["harvestOk"] == 0
    assert payload["consumption"]["spends"] == {"byKind": {}, "byType": {}, "total": 0}


def test_lifecycle_route_returns_200(tmp_path: Path) -> None:
    app = CommandCenterApp(data_root=tmp_path, now_ms=lambda: NOW_MS)
    response = app.handle(ApiRequest("GET", "/api/audit/lifecycle"))
    assert response.status == 200
    body = json.loads(response.body.decode("utf-8"))
    assert set(body) == {"t1", "t2", "t3", "t4"}
    assert body["t1"]["units"] == []
    single = app.handle(ApiRequest("GET", "/api/audit/lifecycle", query="tenant=t1"))
    assert single.status == 200
    single_body = json.loads(single.body.decode("utf-8"))
    assert single_body["tenant"] == "t1"


def test_lifecycle_route_invalid_tenant_400(tmp_path: Path) -> None:
    app = CommandCenterApp(data_root=tmp_path, now_ms=lambda: NOW_MS)
    response = app.handle(ApiRequest("GET", "/api/audit/lifecycle", query="tenant=t9"))
    assert response.status == 400
