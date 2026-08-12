"""Consensus mining projection tests (W44).

Port of the legacy TS ``consensus-mining.ts`` pure join: alliance-survey
consensus mines + mining-effectiveness fulfillment labels + mine-utilization
gapAge + enemy-heat threat. The pure core is tested directly with hand-built
payloads (all status branches, gapAge merge, threat levels, topStale order);
the loader is tested over a synthetic P5-3 data root. Node golden parity is
tracked separately (BLOCKED follow-up).
"""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.projections import (
    enrich_consensus_mining,
    load_consensus_mining,
)
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

NOW_MS = 1_752_000_000_000
ISO_AT = "2025-07-08T18:40:00.000Z"


def _load_fixture(name: str) -> dict:
    path = Path(__file__).parent / "fixtures" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _survey_payload() -> dict:
    return {
        "generatedAt": ISO_AT,
        "colors": {"t1": "#111111", "t2": "#222222"},
        "tenantSummaries": {"t1": {"caseCount": 1}, "t2": {"caseCount": 1}},
        "consensusResources": [
            {"x": 10, "y": 10, "tenant": "t1", "state": "visible", "tick": 4900},
            {"x": 40, "y": 40, "tenant": "t1", "state": "visible", "tick": 4500},
            {"x": 160, "y": 160, "tenant": "t2", "state": "visible", "tick": 5000},
            {"x": 200, "y": 200, "tenant": "t2", "state": "visible", "tick": 1000},
        ],
    }


def test_enrich_consensus_mining_status_classification() -> None:
    effectiveness = {
        "items": [
            {"cell": "10,10", "assignedTenant": "t1", "status": "harvested"},
            {"cell": "40,40", "assignedTenant": "t1", "status": "open"},
            {"cell": "160,160", "assignedTenant": "t2", "status": "stale"},
            {"cell": "200,200", "assignedTenant": "t2", "status": "harvestedByOther"},
        ]
    }
    body = enrich_consensus_mining(_survey_payload(), effectiveness)
    by_cell = {item["cell"]: item for item in body["resources"]}
    assert by_cell["10,10"]["miningStatus"] == "harvested"
    assert by_cell["10,10"]["assignedTenant"] == "t1"
    assert by_cell["40,40"]["miningStatus"] == "open"
    assert by_cell["160,160"]["miningStatus"] == "stale"
    assert by_cell["200,200"]["miningStatus"] == "harvestedByOther"
    summary = body["summary"]
    assert summary["assigned"] == 4
    assert summary["open"] == 1
    assert summary["stale"] == 1
    assert summary["harvested"] == 1
    assert summary["harvestedByOther"] == 1
    assert summary["highThreat"] == 0
    # topStale carries open/stale only, sorted gapAge desc
    assert {item["cell"] for item in summary["topStale"]} == {"40,40", "160,160"}
    assert all(item["assignedTenant"] for item in summary["topStale"])


def test_enrich_consensus_mining_gap_age_and_threat() -> None:
    effectiveness = {
        "items": [
            {"cell": "10,10", "assignedTenant": "t1", "status": "open"},
            {"cell": "40,40", "assignedTenant": "t1", "status": "open"},
        ]
    }
    mines = {
        "tenants": {
            "t1": {
                "candidates": [
                    {"cell": "10,10", "gapAgeTicks": 3000},
                    {"cell": "10,10", "gapAgeTicks": 5000},  # max wins
                    {"cell": "40,40", "gapAgeTicks": None},
                ]
            },
            "t2": {"candidates": []},
        }
    }
    heat = {
        "0,0": {"combatCount": 10, "count": 10, "lastTick": 4900},  # threat 3
        "2,2": {"combatCount": 3, "count": 3, "lastTick": 4500},  # threat 2
    }
    body = enrich_consensus_mining(_survey_payload(), effectiveness, mines, heat)
    by_cell = {item["cell"]: item for item in body["resources"]}
    assert by_cell["10,10"]["gapAgeTicks"] == 5000  # max of candidate gap ages
    assert by_cell["10,10"]["threatLevel"] == 3
    assert by_cell["10,10"]["threatCombat"] == 10
    assert by_cell["40,40"]["threatLevel"] == 2
    assert by_cell["40,40"]["gapAgeTicks"] is None
    # 160,160 / 200,200 have no heat -> threat 0, combat 0
    assert by_cell["160,160"]["threatLevel"] == 0
    assert by_cell["160,160"]["threatCombat"] == 0
    assert body["summary"]["highThreat"] == 2
    # topStale sorted by gapAge desc (4000-unassigned cells excluded from topStale)
    top = body["summary"]["topStale"]
    assert [item["cell"] for item in top] == ["10,10", "40,40"]


def test_enrich_consensus_mining_top_stale_limit_and_passthrough() -> None:
    survey = {
        "colors": {"t1": "#111111"},
        "tenantSummaries": {"t1": {"caseCount": 2}},
        "consensusResources": [
            {"x": i, "y": 0, "tenant": "t1", "state": "visible", "tick": 4000 + i}
            for i in range(15)
        ],
    }
    effectiveness = {
        "items": [{"cell": f"{i},0", "assignedTenant": "t1", "status": "open"} for i in range(15)]
    }
    body = enrich_consensus_mining(survey, effectiveness)
    assert len(body["summary"]["topStale"]) == 10
    assert body["colors"] == {"t1": "#111111"}
    assert body["tenantSummaries"] == {"t1": {"caseCount": 2}}
    assert body["summary"]["assigned"] == 15


def test_enrich_consensus_mining_null_inputs() -> None:
    body = enrich_consensus_mining(None, None)
    assert body["resources"] == []
    assert body["summary"] == {
        "assigned": 0,
        "open": 0,
        "stale": 0,
        "harvested": 0,
        "harvestedByOther": 0,
        "highThreat": 0,
        "topStale": [],
    }
    assert body["colors"] == {}
    assert body["tenantSummaries"] == {}


def test_load_consensus_mining_builds_payload(tmp_path: Path) -> None:
    fixture = _load_fixture("consensus_mining_basic")
    materialize_advice_data_root(fixture, tmp_path)

    payload = load_consensus_mining(tmp_path, now_ms=NOW_MS)
    assert payload["generatedAt"] == ISO_AT
    assert payload["cachedAt"] == ISO_AT
    by_cell = {item["cell"]: item for item in payload["resources"]}
    # both visible-but-unharvested mines are assigned and classified open
    assert set(by_cell) == {"10,10", "170,170"}
    assert by_cell["10,10"]["assignedTenant"] == "t1"
    assert by_cell["10,10"]["miningStatus"] == "open"
    assert by_cell["10,10"]["gapAgeTicks"] == 4000  # 5000 - first_seen 1000
    assert by_cell["10,10"]["threatLevel"] == 3  # 10 combat sightings in bucket 0,0
    assert by_cell["170,170"]["assignedTenant"] == "t2"
    assert by_cell["170,170"]["miningStatus"] == "open"
    assert by_cell["170,170"]["threatLevel"] == 0
    summary = payload["summary"]
    assert summary["assigned"] == 2
    assert summary["open"] == 2
    assert summary["highThreat"] == 1
    assert [item["cell"] for item in summary["topStale"]] == ["10,10", "170,170"]
    assert payload["colors"] == {
        "t1": "#69b3d8",
        "t2": "#57bd84",
        "t3": "#a892d6",
        "t4": "#dd626d",
    }
    assert set(payload["tenantSummaries"]) == {"t1", "t2", "t3", "t4"}


def test_load_consensus_mining_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_consensus_mining(tmp_path, now_ms=NOW_MS)
    assert payload["generatedAt"] == ISO_AT
    assert payload["resources"] == []
    assert payload["summary"]["assigned"] == 0
    assert payload["summary"]["topStale"] == []
    assert set(payload["colors"]) == {"t1", "t2", "t3", "t4"}
