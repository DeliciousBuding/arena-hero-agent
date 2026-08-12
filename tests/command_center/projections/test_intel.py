"""Intel + leaderboard projection tests (W44 wave 7).

Pins ``loadAllianceIntel`` / ``buildEncounteredIndex`` / ``loadBeaconTrail`` /
``loadOurUsernames`` / ``buildLeaderboardPayload`` against a materialized
multi-run calibration data root (enemy core scans + raid-risk + beacon-carrier
inference + enemy-unit memory + survey core_hunts merge) and the empty-root
fail-open behavior (200, never 500). Node golden parity lives in
``test_golden_parity.py`` (intel_basic / leaderboard_basic, both MATCH).
"""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.api import ApiRequest, CommandCenterApp
from arena_hero_agent.command_center.projections import (
    build_encountered_index,
    build_encountered_index_from_enemies,
    build_leaderboard_payload,
    load_alliance_intel,
    load_beacon_trail,
    load_leaderboard_intel,
    load_our_usernames,
)
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

NOW_MS = 1_752_000_000_000
FIXTURES = Path(__file__).parent / "fixtures"


def _intel_fixture() -> dict:
    return json.loads((FIXTURES / "intel_basic.json").read_text(encoding="utf-8"))


def test_load_alliance_intel_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_intel_fixture(), tmp_path)
    payload = load_alliance_intel(root, now_ms=NOW_MS)
    assert set(payload) == {"generatedAt", "tenants", "enemies", "totalEnemyCores", "beacons"}
    assert payload["totalEnemyCores"] == 11
    tenants = {t["tenant"]: t for t in payload["tenants"]}
    t1 = tenants["t1"]
    assert t1["runId"] == "run-new"
    assert t1["combatUnitsNearCore"] == 3
    assert t1["enemyUnits"] == 3
    assert t1["enemyUnitSightings"] == 3  # u1/u2/u3 naive sightings; WORKER ignored
    assert t1["ourCore"] == [0, 0]
    by_name = {e["username"]: e for e in t1["enemyCores"]}
    assert by_name["alpha"]["raidRisk"] == "CRITICAL"  # raid_party >= 3 units
    assert by_name["alpha"]["tier"] == "ELITE_AGGRESSOR"
    assert by_name["alpha"]["damageRank"] == 1
    assert by_name["oldcore"]["raidRisk"] == "HIGH"  # stale downgrade CRITICAL -> HIGH
    assert "stale sighting" in by_name["oldcore"]["raidReason"]
    assert by_name["gamma"]["lastSeenTick"] == 5190  # survey core_hunts memory merge
    t2 = tenants["t2"]
    assert t2["runId"] == "run-1"
    assert t2["combatUnitsNearCore"] == 0
    t2_by_name = {e["username"]: e for e in t2["enemyCores"]}
    assert t2_by_name["delta"]["raidRisk"] == "HIGH"  # core_close 10
    assert t2_by_name["aggr"]["raidRisk"] == "MEDIUM"  # aggressor_medium 40
    assert t2_by_name["aggrfar"]["raidRisk"] == "LOW"  # aggressor_far 90
    assert t2_by_name["farfar"]["raidRisk"] == "LOW"  # core_far 60
    assert t2_by_name["out"]["raidRisk"] == "NONE"  # out_of_range 150
    assert t2_by_name["zeta"]["lastSeenTick"] == 5990  # survey memory merge
    assert tenants["t3"]["runId"] is None
    assert tenants["t4"]["enemyCores"] == []


def test_load_alliance_intel_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_alliance_intel(tmp_path, now_ms=NOW_MS)
    assert payload["enemies"] == []
    assert payload["beacons"] == []
    assert payload["totalEnemyCores"] == 0
    assert [t["tenant"] for t in payload["tenants"]] == ["t1", "t2", "t3", "t4"]
    assert all(t["runId"] is None for t in payload["tenants"])


def test_load_beacon_trail_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_intel_fixture(), tmp_path)
    trail = load_beacon_trail(root, "t1")
    # [3,3]@5000, [4,4]@5100, [5,5]@5195, [6,6]@5200 (consecutive dedupe)
    assert [(p["x"], p["y"], p["tick"]) for p in trail] == [
        (3, 3, 5000),
        (4, 4, 5100),
        (5, 5, 5195),
        (6, 6, 5200),
    ]
    assert load_beacon_trail(root, "t3") == []


def test_build_encountered_index_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_intel_fixture(), tmp_path)
    index = build_encountered_index(root, now_ms=NOW_MS)
    assert index["alpha"] == [
        {"tenant": "t1", "lastSeenTick": 5200, "distanceToFriendlyCore": 10, "raidRisk": "CRITICAL"}
    ]
    assert index["delta"] == [
        {"tenant": "t2", "lastSeenTick": 6000, "distanceToFriendlyCore": 10, "raidRisk": "HIGH"}
    ]
    assert "stranger" not in index
    assert build_encountered_index_from_enemies([]) == {}


def test_load_our_usernames_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_intel_fixture(), tmp_path)
    ours = {item["tenant"]: item["username"] for item in load_our_usernames(root)}
    assert ours == {"t1": "we-t1", "t2": "we-t2"}
    assert load_our_usernames(tmp_path / "empty") == []


def test_build_leaderboard_payload_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_intel_fixture(), tmp_path)
    payload = build_leaderboard_payload(root, now_ms=NOW_MS)
    assert payload["snapshot"] == "leaderboard-2026-07-08-00-00-00.json"
    assert payload["snapshotAtMs"] == NOW_MS
    profiles = {p["username"]: p for p in payload["profiles"]}
    assert profiles["we-t1"]["ours"] == "t1"
    assert profiles["alpha"]["ours"] is None
    assert profiles["alpha"]["encountered"][0]["tenant"] == "t1"
    assert profiles["stranger"]["encountered"] is None
    assert payload["encounteredCount"] > 0
    assert isinstance(payload["encountered"], dict)
    assert payload["ours"] == [
        {"tenant": "t1", "username": "we-t1"},
        {"tenant": "t2", "username": "we-t2"},
    ]


def test_build_leaderboard_payload_empty_root_fails_open(tmp_path: Path) -> None:
    payload = build_leaderboard_payload(tmp_path, now_ms=NOW_MS)
    assert payload["profiles"] == []
    assert payload["ours"] == []
    assert payload["encounteredCount"] == 0
    assert payload["encountered"] == {}
    assert "error" in payload
    assert load_leaderboard_intel(tmp_path, now_ms=NOW_MS) is None


def test_intel_and_leaderboard_routes_wired_200(tmp_path: Path) -> None:
    app = CommandCenterApp(data_root=tmp_path, now_ms=lambda: NOW_MS)
    for path in ("/api/intel", "/api/leaderboard"):
        response = app.handle(ApiRequest("GET", path))
        assert response.status == 200, path
        body = json.loads(response.body.decode("utf-8"))
        assert isinstance(body, dict)
    intel_body = json.loads(app.handle(ApiRequest("GET", "/api/intel")).body.decode("utf-8"))
    assert intel_body["totalEnemyCores"] == 0
