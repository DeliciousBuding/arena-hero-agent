"""Alliance advice projection: loader tests (W25).

The full composition (all 11 advice sections) is golden-tested against the
real Node TS oracle in ``test_golden_parity.py`` (alliance_advice_basic /
alliance_advice_full, both field-for-field MATCH). These tests cover the thin
I/O layer: the loader composes the existing P5-4 projections over the shared
P5-3 data base and fails open exactly like the oracle.
"""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.projections import load_alliance_advice
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

NOW_MS = 1_752_000_000_000
ISO_AT = "2025-07-08T18:40:00.000Z"


def _load_fixture(name: str) -> dict:
    path = Path(__file__).parent / "fixtures" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_load_alliance_advice_builds_payload_from_data_root(tmp_path: Path) -> None:
    fixture = _load_fixture("alliance_advice_basic")
    materialize_advice_data_root(fixture, tmp_path)

    payload = load_alliance_advice(tmp_path, now_ms=NOW_MS)
    assert payload["generatedAt"] == ISO_AT
    assert payload["cachedAt"] == ISO_AT
    assert len(payload["advice"]) == 15
    assert payload["summary"] == {"critical": 1, "high": 7, "medium": 6, "info": 3}
    assert payload["avgConfidence"] == 0.77
    assert all(entry["at"] == ISO_AT for entry in payload["advice"])

    # every section's representative category appears in the payload
    categories = {entry["category"] for entry in payload["advice"]}
    assert categories == {"MILITARY", "THREAT", "ECONOMY", "INTEL", "CONFLICT"}

    # evidence anchors exist for the shown high-severity items; the INFO
    # leaderboard baseline is generated but hidden behind the 15-item slice
    evidence_types = {item["type"] for entry in payload["advice"] for item in entry["evidence"]}
    assert {"world", "sighting", "heat", "survey", "audit"} <= evidence_types


def test_load_alliance_advice_full_covers_audit_signals(tmp_path: Path) -> None:
    fixture = _load_fixture("alliance_advice_full")
    materialize_advice_data_root(fixture, tmp_path)

    payload = load_alliance_advice(tmp_path, now_ms=NOW_MS)
    assert payload["summary"] == {"critical": 1, "high": 9, "medium": 10, "info": 3}
    # the negative-coreDelta audit advice is HIGH, so it is visible in the
    # 15-item slice; the stall / rejected-rate / mining-fulfillment advice is
    # MEDIUM and verified at the signal level (sub-loaders) plus by the golden
    # byte parity (TS oracle agrees on the exact deduped counts).
    audit_items = [
        entry for entry in payload["advice"] if any(e["type"] == "audit" for e in entry["evidence"])
    ]
    assert len(audit_items) >= 1
    assert any("负增长" in entry["title"] for entry in audit_items)

    from arena_hero_agent.command_center.projections.conflicts import (
        load_human_conflict,
    )
    from arena_hero_agent.command_center.projections.decisions import (
        load_decision_trend,
    )

    trend = load_decision_trend(tmp_path, "t1", window=500, steps=4)
    assert trend["trend"][-1]["coreDelta"] < 0
    assert trend["trend"][-1]["stallRate"] == 1.0
    conflict = load_human_conflict(tmp_path, "all", window=3000)
    assert conflict["t1"]["rejectedRate"] == 1.0


def test_load_alliance_advice_empty_data_root_is_fail_open(tmp_path: Path) -> None:
    payload = load_alliance_advice(tmp_path, now_ms=NOW_MS)
    assert payload["generatedAt"] == ISO_AT
    assert payload["advice"] == []
    assert payload["dedupCount"] == 0
    assert payload["avgConfidence"] == 0
    assert payload["summary"] == {"critical": 0, "high": 0, "medium": 0, "info": 0}


def test_load_alliance_advice_missing_survey_db_fails_open(tmp_path: Path) -> None:
    # worlds only (no survey db): snapshot members exist but no survey tables.
    fixture = _load_fixture("alliance_advice_basic")
    worlds = fixture["worlds"]
    bare = {"nowMs": NOW_MS, "worlds": worlds}
    materialize_advice_data_root(bare, tmp_path)

    payload = load_alliance_advice(tmp_path, now_ms=NOW_MS)
    assert payload["generatedAt"] == ISO_AT
    # economy advice still fires from world state; no survey-driven sections
    assert any(entry["category"] == "ECONOMY" for entry in payload["advice"])
    assert all(
        entry["category"] not in {"CONFLICT", "INTEL"} or entry["evidence"] == []
        for entry in payload["advice"]
    )
