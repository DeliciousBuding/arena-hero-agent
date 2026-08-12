"""Alliance survey / arbitrations projection wiring tests (W44).

Pins the ``load_alliance_survey`` / ``list_arbitrations`` loaders against a
synthetic cc_wiring fixture (cross-tenant resource overlap + human
arbitration) and the empty-root fail-open behavior; Node golden parity stays a
BLOCKED follow-up for these endpoints.
"""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.projections import (
    list_arbitrations,
    load_alliance_survey,
)
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

NOW_MS = 1_752_000_000_000
ISO_AT = "2025-07-08T18:40:00.000Z"
FIXTURES = Path(__file__).parent / "fixtures" / "cc_wiring"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_load_alliance_survey_fixture_builds_payload(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("alliance_survey_basic"), tmp_path)
    payload = load_alliance_survey(root, now_ms=NOW_MS)
    assert payload["generatedAt"] == ISO_AT
    assert payload["cachedAt"] == ISO_AT
    assert set(payload["colors"]) == {"t1", "t2", "t3", "t4"}
    assert payload["tenantSummaries"]["t1"]["resources"] == 2
    assert payload["tenantSummaries"]["t1"]["obstacles"] == 1
    assert payload["tenantSummaries"]["t1"]["cores"] == 1
    assert payload["tenantSummaries"]["t2"]["resources"] == 1

    # cross-tenant resource overlap -> consensus with arbitration winner t2
    overlap = next(
        item for item in payload["conflicts"]["resourceOverlaps"] if item["cell"] == "1,1"
    )
    assert overlap["tenants"] == ["t1", "t2"]
    assert overlap["arbitration"]["winner"] == "t2"
    assert overlap["arbitration"]["arbitrated"] is True

    by_cell = {(row["x"], row["y"]): row for row in payload["consensusResources"]}
    assert by_cell[(1, 1)]["consensus"] == 2
    assert by_cell[(1, 1)]["observers"] == ["t1", "t2"]
    assert by_cell[(1, 1)]["arbitrated"] is True
    assert by_cell[(5, 5)]["consensus"] == 1

    assert payload["enemyCores"] == [
        {"tenant": "t1", "x": 20, "y": 20, "tick": 450, "owner": "enemy1", "source": "CORE"}
    ]


def test_load_alliance_survey_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_alliance_survey(tmp_path, now_ms=NOW_MS)
    assert payload["generatedAt"] == ISO_AT
    for t in ("t1", "t2", "t3", "t4"):
        assert payload["tenantSummaries"][t]["resources"] == 0
    assert payload["consensusResources"] == []
    assert payload["enemyCores"] == []
    assert payload["conflicts"] == {"resourceOverlaps": [], "obstacleResourceConflicts": []}


def test_list_arbitrations_fixture_returns_effective_rows(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("alliance_survey_basic"), tmp_path)
    assert list_arbitrations(root) == [
        {
            "cell": "1,1",
            "winnerTenant": "t2",
            "createdAt": "2025-07-08T18:30:00Z",
            "note": "t2 latest",
        }
    ]


def test_list_arbitrations_empty_root_fails_open(tmp_path: Path) -> None:
    assert list_arbitrations(tmp_path) == []
