"""Mine-pattern refill predictions (W44 wave 4).

Pins the pure-function ports of the TS refill model (A15/A16): appearance-
window refill estimates, absence-segment -> re-seen cycles, absent-length
distribution, suspected dead mines, and prediction hit evaluation, plus the
loader composition on a fixture that carries ``resource_seen_history`` and
``resource_absences``.
"""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.projections import load_mine_patterns
from arena_hero_agent.command_center.projections.mine_patterns import (
    compute_absent_stats,
    compute_dead_mines,
    compute_prediction_accuracy,
    compute_refill_predictions,
    compute_refill_predictions_from_absences,
    compute_refill_stats,
    compute_refill_stats_from_absences,
)
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

NOW_MS = 1_752_000_000_000
ISO_AT = "2025-07-08T18:40:00.000Z"


def _load_fixture(name: str) -> dict:
    path = Path(__file__).parent / "fixtures" / "cc_wiring" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_compute_refill_stats_from_windows() -> None:
    rows = [
        {"cell": "10,10", "tick": 1000},
        {"cell": "10,10", "tick": 1001},
        {"cell": "10,10", "tick": 3000},
        {"cell": "10,10", "tick": 3001},
    ]
    stats = compute_refill_stats(rows)
    assert stats == {
        "samples": 1,
        "avgRefillTicks": None,
        "recent": [{"cell": "10,10", "gapTicks": 2000, "lastSeenTick": 3000}],
    }


def test_compute_refill_stats_empty() -> None:
    assert compute_refill_stats([]) is None


def test_compute_refill_stats_from_absences() -> None:
    absences = [
        {"cell": "50,50", "tick": 1000},
        {"cell": "50,50", "tick": 1001},
        {"cell": "50,50", "tick": 3000},
        {"cell": "50,50", "tick": 3001},
    ]
    seen = [{"cell": "50,50", "tick": 1500}, {"cell": "50,50", "tick": 4000}]
    stats = compute_refill_stats_from_absences(absences, seen)
    assert stats == {
        "samples": 2,
        "avgRefillTicks": 749,  # round((499 + 999) / 2)
        "recent": [
            {"cell": "50,50", "gapTicks": 999, "lastSeenTick": 4000},
            {"cell": "50,50", "gapTicks": 499, "lastSeenTick": 1500},
        ],
    }


def test_compute_refill_predictions_history() -> None:
    rows = [
        {"cell": "10,10", "tick": 1000},
        {"cell": "10,10", "tick": 1001},
        {"cell": "10,10", "tick": 3000},
        {"cell": "10,10", "tick": 3001},
    ]
    resources = [{"cell": "10,10", "x": 10, "y": 10}]
    predictions = compute_refill_predictions(rows, resources, 5000)
    assert predictions == [
        {
            "cell": "10,10",
            "x": 10,
            "y": 10,
            "windows": 2,
            "avgGapTicks": 2000,
            "lastSeenTick": 3001,
            "predictedNextTick": 5000,  # 3001 + max(1, round(1999))
            "dueInTicks": 0,
        }
    ]


def test_compute_refill_predictions_from_absences() -> None:
    absences = [
        {"cell": "50,50", "tick": 1000},
        {"cell": "50,50", "tick": 1001},
        {"cell": "50,50", "tick": 3000},
        {"cell": "50,50", "tick": 3001},
    ]
    seen = [{"cell": "50,50", "tick": 1500}, {"cell": "50,50", "tick": 4000}]
    resources = [{"cell": "50,50", "x": 50, "y": 50}]
    predictions = compute_refill_predictions_from_absences(absences, seen, resources, 5000)
    assert predictions == [
        {
            "cell": "50,50",
            "x": 50,
            "y": 50,
            "windows": 2,
            "avgGapTicks": 499,  # only the non-last segment counts a cycle (TS parity)
            "lastSeenTick": 3001,
            "predictedNextTick": 3500,  # 3001 + 499
            "dueInTicks": -1500,
        }
    ]


def test_compute_absent_stats_and_dead_mines() -> None:
    absences = [{"cell": "60,60", "tick": tick} for tick in range(1000, 1201)]
    stats = compute_absent_stats(absences)
    assert stats == {"segCount": 1, "medianLen": 200, "p90Len": 200, "p99Len": 200}
    dead = compute_dead_mines(absences, [{"cell": "60,60", "x": 60, "y": 60}])
    assert dead == [
        {"cell": "60,60", "x": 60, "y": 60, "maxAbsentLen": 200, "lastAbsentTick": 1200}
    ]


def test_compute_prediction_accuracy() -> None:
    predictions = [
        {
            "cell": "50,50",
            "x": 50,
            "y": 50,
            "windows": 2,
            "avgGapTicks": 498,  # only seg0's re-seen cycle counts (TS parity)
            "lastSeenTick": 3002,
            "predictedNextTick": 3500,  # 3002 + 498
            "dueInTicks": -1500,
        }
    ]
    seen = [{"cell": "50,50", "tick": 4002}]
    accuracy = compute_prediction_accuracy(predictions, seen, 5000)
    assert accuracy == {
        "evaluated": 1,
        "hits": 1,
        "misses": 0,
        "hitRate": 1.0,
        "avgMissOverdue": None,
    }
    assert compute_prediction_accuracy([], seen, 5000) is None


def test_load_mine_patterns_predictions_fixture(tmp_path: Path) -> None:
    fixture = _load_fixture("mine_patterns_predictions_basic")
    materialize_advice_data_root(fixture, tmp_path)

    payload = load_mine_patterns(tmp_path, "all", now_ms=NOW_MS)
    assert payload["generatedAt"] == ISO_AT
    assert payload["tenant"] == "all"

    t1 = payload["tenants"]["t1"]
    assert t1["refillSource"] == "history"
    assert t1["refill"] == {
        "samples": 1,
        "avgRefillTicks": None,
        "recent": [{"cell": "10,10", "gapTicks": 2000, "lastSeenTick": 3000}],
    }
    assert t1["predictions"] == [
        {
            "cell": "10,10",
            "x": 10,
            "y": 10,
            "windows": 2,
            "avgGapTicks": 2000,
            "lastSeenTick": 3003,
            "predictedNextTick": 5001,
            "dueInTicks": 1,
        }
    ]
    assert t1["absentStats"] is None
    assert t1["deadMines"] == []
    assert t1["predictionAccuracy"] is None

    t2 = payload["tenants"]["t2"]
    assert t2["refillSource"] == "absences"
    assert t2["refill"]["samples"] == 2
    assert t2["refill"]["avgRefillTicks"] == 748
    assert t2["predictions"] == [
        {
            "cell": "50,50",
            "x": 50,
            "y": 50,
            "windows": 2,
            "avgGapTicks": 498,  # only seg0's re-seen cycle counts (TS parity)
            "lastSeenTick": 3002,
            "predictedNextTick": 3500,  # 3002 + 498
            "dueInTicks": -1500,
        }
    ]
    assert t2["absentStats"] == {"segCount": 3, "medianLen": 2, "p90Len": 200, "p99Len": 200}
    assert t2["deadMines"] == [
        {"cell": "60,60", "x": 60, "y": 60, "maxAbsentLen": 200, "lastAbsentTick": 1200}
    ]
    assert t2["predictionAccuracy"] == {
        "evaluated": 1,
        "hits": 1,
        "misses": 0,
        "hitRate": 1.0,
        "avgMissOverdue": None,
    }
    assert "缺席段实证" in payload["modelCaveat"]


def test_load_mine_patterns_predictions_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_mine_patterns(tmp_path, "all", now_ms=NOW_MS)
    for t in ("t1", "t2", "t3", "t4"):
        assert payload["tenants"][t]["predictions"] == []
        assert payload["tenants"][t]["refillSource"] == "none"
        assert payload["tenants"][t]["deadMines"] == []
