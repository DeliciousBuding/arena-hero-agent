"""Alliance exploration coverage projection tests (W44).

The exploration loader is a thin read over the P5-3 survey databases (``chunks``
table) plus the calibration world cores. The aggregation core
(``compute_exploration_stats``) is ported 1:1 from the TS oracle; these tests
pin the loader composition and the pure-core join semantics with synthetic
fixture data. Node golden parity is tracked separately (BLOCKED follow-up).
"""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.projections import (
    compute_exploration_stats,
    load_alliance_exploration,
)
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

NOW_MS = 1_752_000_000_000
ISO_AT = "2025-07-08T18:40:00.000Z"


def _load_fixture(name: str) -> dict:
    path = Path(__file__).parent / "fixtures" / "cc_wiring" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_compute_exploration_stats_union_and_exclusive() -> None:
    chunks_by_tenant = {
        "t1": [
            {"key": "0,0", "lastSeenTick": 4900},
            {"key": "1,0", "lastSeenTick": 2000},
            {"key": "2,0", "lastSeenTick": 100},
        ],
        "t2": [
            {"key": "0,0", "lastSeenTick": 4000},
            {"key": "10,10", "lastSeenTick": 5000},
        ],
        "t3": [],
        "t4": [],
    }
    cores_by_tenant = {"t1": (0, 0), "t2": (160, 160), "t3": None, "t4": None}
    stats = compute_exploration_stats(chunks_by_tenant, cores_by_tenant, current_tick=5000)

    assert stats["world"]["exploredChunks"] == 4
    assert stats["alliance"]["unionChunks"] == 4
    assert (
        stats["alliance"]["unionRecent"] == 2
    )  # 0,0(4900) + 10,10(5000) >= 3000; union dedupes per cell
    assert stats["alliance"]["exclusiveByTenant"] == {"t1": 2, "t2": 1, "t3": 0, "t4": 0}
    assert stats["perTenant"]["t1"]["exploredChunks"] == 3
    assert stats["perTenant"]["t2"]["exploredChunks"] == 2

    # stale (lastSeenTick < 5000-2000) chunks near a friendly core become targets
    targets = {item["key"] for item in stats["resurveyTargets"]}
    assert targets == {"1,0", "2,0"}
    assert all(item["nearCoreOf"] == "t1" for item in stats["resurveyTargets"])
    assert [item["key"] for item in stats["resurveyTargets"]] == ["2,0", "1,0"]  # stalest-first

    # gaps never include an explored chunk
    union = {"0,0", "1,0", "2,0", "10,10"}
    assert stats["gaps"]
    assert all(f"{g['cx']},{g['cy']}" not in union for g in stats["gaps"])
    assert len(stats["gaps"]) <= 40  # GAP_CAP


def test_compute_exploration_stats_empty_inputs() -> None:
    stats = compute_exploration_stats({t: [] for t in ("t1", "t2", "t3", "t4")}, {}, 0)
    assert stats["world"]["exploredChunks"] == 0
    assert stats["alliance"]["unionChunks"] == 0
    assert stats["alliance"]["exclusiveByTenant"] == {"t1": 0, "t2": 0, "t3": 0, "t4": 0}
    assert stats["gaps"] == []
    assert stats["resurveyTargets"] == []
    assert stats["world"]["coveragePct"] is None


def test_load_alliance_exploration_builds_payload(tmp_path: Path) -> None:
    fixture = _load_fixture("exploration_basic")
    materialize_advice_data_root(fixture, tmp_path)

    payload = load_alliance_exploration(tmp_path, now_ms=NOW_MS)
    assert payload["generatedAt"] == ISO_AT
    assert payload["cachedAt"] == ISO_AT
    assert payload["world"]["exploredChunks"] == 4
    assert payload["alliance"]["unionChunks"] == 4
    assert payload["perTenant"]["t1"]["exploredChunks"] == 3
    assert payload["perTenant"]["t2"]["exploredChunks"] == 2
    assert {item["key"] for item in payload["resurveyTargets"]} == {"1,0", "2,0"}
    assert payload["alliance"]["exclusiveByTenant"] == {"t1": 2, "t2": 1, "t3": 0, "t4": 0}


def test_load_alliance_exploration_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_alliance_exploration(tmp_path, now_ms=NOW_MS)
    assert payload["generatedAt"] == ISO_AT
    assert payload["world"]["exploredChunks"] == 0
    assert payload["resurveyTargets"] == []
    assert payload["gaps"] == []
    assert payload["alliance"]["coveragePct"] is None
