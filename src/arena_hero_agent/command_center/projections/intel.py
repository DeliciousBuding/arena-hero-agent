"""Alliance intel + raid-risk projection (W44 wave 7).

Port of the legacy TypeScript ``packages/command-center/lib/intel.ts``
(``loadAllianceIntel`` / ``buildEncounteredIndex``) plus the beacon trail
read it depends on (``lib/trails.ts`` ``loadBeaconTrail``): merge the four
tenant calibrations' enemy mapping (enemy core owner/position/last sighting +
enemy activity counts), join the official leaderboard threat profile
(``leaderboard.py``), and emit raid-risk tiers with beacon-carrier inference
and the enemy-unit memory layer for the panel.

Pure read of calibration cases + survey-db ``core_hunts`` + leaderboard
snapshot; fail-open to an empty payload (one ``runId: null`` tenant entry per
tenant) when the data root is empty. Registered differences from the TS
oracle:

- ``now_ms`` is injectable (the TS uses ``Date.now()`` for ``generatedAt``,
  which is not oracle-comparable);
- no 30s module-level cache: the TS serves stale-while-revalidate from memory;
  the Python port recomputes per call (same output, no background refresh);
- the ``buildEncounteredIndex`` 30s cache is likewise omitted; the index is
  rebuilt from the passed intel payload.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import latest_run_dir, list_cases, parse_tick, runs_by_max_tick
from ..paths import TENANTS, calibration_dir, validate_data_root
from ._common import current_epoch_ms, finite_number, num
from .leaderboard import load_leaderboard_intel
from .survey import load_tenant_survey_cached

__all__ = [
    "INTEL_CASE_LIMIT",
    "RAID_ACTIVITY_WINDOW",
    "RAID_CORE_RADIUS",
    "RAID_PARTY_SIZE",
    "RAID_UNIT_WATCH_RADIUS",
    "RUN_SCAN",
    "SURVEY_MEMORY_RADIUS",
    "SURVEY_MEMORY_WINDOW",
    "assess_raid_risk",
    "build_encountered_index",
    "build_encountered_index_from_enemies",
    "load_alliance_intel",
    "load_beacon_trail",
]

# Raid-risk cascade constants (TS intel.ts, mirror of arena-agent raid-risk.ts).
RAID_UNIT_WATCH_RADIUS = 18
RAID_CORE_RADIUS = 24
RAID_PARTY_SIZE = 3
# "近期快攻活动"窗口：我方核心警戒圈内目击到敌军战斗单位距今不超过该窗口
# 才算"活动中的快攻"（防 30-run 扫描把上千 tick 前的旧目击误报为 CRITICAL）。
RAID_ACTIVITY_WINDOW = 300
# 敌核心目击 >2000 tick 视为陈旧（CORE_HUNT_STICKY_TICKS 同口径，stale 降级）。
CORE_HUNT_STICKY_TICKS = 2000
RUN_SCAN = 30  # 联盟情报扫描 run 数（平衡覆盖与性能）
SURVEY_MEMORY_RADIUS = 24  # 贴脸敌核记忆合并半径
SURVEY_MEMORY_WINDOW = 10_000  # 贴脸敌核记忆目击窗口（tick）
INTEL_CASE_LIMIT = 8  # 每个 run 取最近 N 个 case
ENEMY_UNIT_MEMORY_LIMIT = 100  # 面板敌情记忆层上限

# Beacon trail constants (TS trails.ts).
BEACON_TRAIL_RUNS = 6
BEACON_TRAIL_CASE_LIMIT = 300
BEACON_TRAIL_MAX_POINTS = 96
BEACON_TRAIL_RECENT_TICKS = 2000


def _manhattan(a: Sequence[object], b: Sequence[object]) -> int | float:
    return abs(num(a[0]) - num(b[0])) + abs(num(a[1]) - num(b[1]))


def _chebyshev(a: Sequence[object], b: Sequence[object]) -> int | float:
    return max(abs(num(a[0]) - num(b[0])), abs(num(a[1]) - num(b[1])))


def assess_raid_risk(
    enemy_core_distance: int | float,
    combat_units_near: int,
    tier: str,
    fresh_sighting: bool,
) -> dict[str, str]:
    """Raid-risk tier + reason (TS ``assessRaidRisk``, 1:1 cascade)."""
    if combat_units_near >= RAID_PARTY_SIZE:
        tier_risk = "CRITICAL"
        reason = (
            f"raid_party: {combat_units_near} enemy combat units within "
            f"{RAID_UNIT_WATCH_RADIUS} of our core"
        )
    elif combat_units_near >= 1:
        tier_risk = "HIGH"
        reason = (
            f"raid_scout: {combat_units_near} enemy combat unit(s) within "
            f"{RAID_UNIT_WATCH_RADIUS} of our core"
        )
    elif enemy_core_distance <= 8:
        tier_risk = "CRITICAL"
        reason = f"core_adjacent: enemy core {enemy_core_distance} cells away"
    elif enemy_core_distance <= RAID_CORE_RADIUS:
        tier_risk = "HIGH"
        reason = (
            f"core_close: enemy core {enemy_core_distance} cells away (within {RAID_CORE_RADIUS})"
        )
    elif enemy_core_distance <= 32:
        tier_risk = "MEDIUM"
        reason = f"core_medium: enemy core {enemy_core_distance} cells away"
    elif tier != "STANDARD" and enemy_core_distance <= 48:
        tier_risk = "MEDIUM"
        reason = f"aggressor_medium: {tier} core {enemy_core_distance} cells away"
    elif enemy_core_distance <= 64:
        tier_risk = "LOW"
        reason = f"core_far: enemy core {enemy_core_distance} cells away"
    elif tier != "STANDARD" and enemy_core_distance <= 96:
        tier_risk = "LOW"
        reason = f"aggressor_far: {tier} core {enemy_core_distance} cells away"
    else:
        return {"tier": "NONE", "reason": "out_of_range"}
    if not fresh_sighting and tier_risk != "LOW":
        downgraded = (
            "HIGH" if tier_risk == "CRITICAL" else "MEDIUM" if tier_risk == "HIGH" else "LOW"
        )
        return {"tier": downgraded, "reason": f"{reason} (stale sighting)"}
    return {"tier": tier_risk, "reason": reason}


def _position(value: object) -> list[int | float]:
    """Coerce an artifact position to ``[x, y]`` (missing/empty -> ``[0, 0]``)."""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return [num(value[0]), num(value[1])]
    return [0, 0]


def _read_case(path: Any) -> dict[str, Any] | None:
    """Parse one calibration case file (``{before: {state: {objects}}}``)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _case_state(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    before = raw.get("before")
    state = before.get("state") if isinstance(before, dict) else None
    return state if isinstance(state, dict) else None


def load_beacon_trail(data_root: str | os.PathLike[str], tenant: str) -> list[dict[str, Any]]:
    """Beacon position history (TS ``loadBeaconTrail``, no incremental cache).

    Cross-run merge of ``before.state.champion_beacon.position`` points, sorted
    ascending by tick, consecutive same-cell points deduplicated, capped at
    ``BEACON_TRAIL_MAX_POINTS``, then filtered to the recent-tick window.
    """
    root = validate_data_root(data_root)
    runs = runs_by_max_tick(root, tenant)[:BEACON_TRAIL_RUNS]
    if not runs:
        return []
    all_points: list[dict[str, Any]] = []
    for run in runs:
        run_name = str(run["run"])
        cases = list_cases(root, tenant, run_name)[-BEACON_TRAIL_CASE_LIMIT:]
        base = calibration_dir(root, tenant) / run_name / "cases"
        last_key: str | None = None
        for file in cases:
            raw = _read_case(base / file)
            state = _case_state(raw)
            beacon = state.get("champion_beacon") if state is not None else None
            if not isinstance(beacon, dict):
                continue
            position = beacon.get("position")
            if not isinstance(position, (list, tuple)) or len(position) < 2:
                continue
            x = num(position[0])
            y = num(position[1])
            key = f"{x},{y}"
            if key == last_key:
                continue
            last_key = key
            all_points.append({"x": x, "y": y, "tick": parse_tick(file)})
    all_points.sort(key=lambda point: num(point["tick"]))
    trail: list[dict[str, Any]] = []
    last_key: str | None = None
    for point in all_points:
        key = f"{point['x']},{point['y']}"
        if key == last_key:
            continue
        last_key = key
        trail.append(point)
        if len(trail) > BEACON_TRAIL_MAX_POINTS:
            trail.pop(0)
    max_tick = int(num(runs[0]["maxTick"]))
    return [
        point for point in trail if max_tick - int(num(point["tick"])) <= BEACON_TRAIL_RECENT_TICKS
    ]


def _scan_tenant_intel(
    root: Any,
    tenant: str,
    profiles: list[dict[str, Any]] | None,
    now_ms: int,
) -> dict[str, Any] | None:
    """One tenant's intel scan (TS per-tenant block of ``scanAllianceIntelNow``).

    Returns ``None`` when the tenant has no calibration run (TS pushes a
    ``{tenant, runId: null, enemyCores: [], enemyUnits: 0}`` placeholder).
    """
    run_dir = latest_run_dir(root, tenant)
    if run_dir is None:
        return None
    # 扫最近 RUN_SCAN 个 run（历史敌核心目击在旧 run），每个 run 取最近
    # INTEL_CASE_LIMIT 个 case：run 按最高 case tick 降序（runs_by_max_tick）。
    runs = runs_by_max_tick(root, tenant)[:RUN_SCAN]
    seen_cores: dict[str, dict[str, Any]] = {}
    enemy_unit_sightings = 0
    our_core: list[int | float] | None = None
    our_core_tick = -1
    combat_near_core: dict[str, int] = {}
    enemy_unit_by_id: dict[str, dict[str, Any]] = {}
    latest_tick = 0
    for run in runs:
        run_name = str(run["run"])
        case_files = list_cases(root, tenant, run_name)[-INTEL_CASE_LIMIT:]
        base = calibration_dir(root, tenant) / run_name / "cases"
        for file in case_files:
            tick = parse_tick(file)
            raw = _read_case(base / file)
            state = _case_state(raw)
            if state is None:
                continue
            objects = state.get("objects")
            if not isinstance(objects, list):
                continue
            if tick > latest_tick:
                latest_tick = tick
            for obj in objects:
                if not isinstance(obj, dict):
                    continue
                kind = obj.get("kind")
                if kind == "CORE" and obj.get("controlled"):
                    if our_core_tick < tick:
                        our_core = _position(obj.get("position"))
                        our_core_tick = tick
                elif kind == "CORE" and not obj.get("controlled") and obj.get("owner_username"):
                    owner = str(obj["owner_username"])
                    prev = seen_cores.get(owner)
                    if prev is None or tick > int(num(prev["tick"])):
                        seen_cores[owner] = {
                            "position": _position(obj.get("position")),
                            "tick": tick,
                        }
                elif (
                    kind == "UNIT"
                    and not obj.get("controlled")
                    and obj.get("unit_type") != "WORKER"
                ):
                    enemy_unit_sightings += 1
                    unit_id = str(obj.get("id") or "")
                    position = _position(obj.get("position"))
                    if (
                        our_core is not None
                        and _manhattan(position, our_core) <= RAID_UNIT_WATCH_RADIUS
                    ):
                        prev = combat_near_core.get(unit_id)
                        if prev is None or tick > prev:
                            combat_near_core[unit_id] = tick
                    prev_unit = enemy_unit_by_id.get(unit_id)
                    if prev_unit is None or tick > int(num(prev_unit["tick"])):
                        enemy_unit_by_id[unit_id] = {
                            "unitType": str(obj.get("unit_type") or "VANGUARD"),
                            "position": position,
                            "tick": tick,
                        }
    # 近期快攻活动：警戒圈内目击距今 <= RAID_ACTIVITY_WINDOW 才算"活动中的快攻"。
    recent_combat = [
        {"id": unit_id, "age": latest_tick - sighted_tick}
        for unit_id, sighted_tick in combat_near_core.items()
        if latest_tick - sighted_tick <= RAID_ACTIVITY_WINDOW
    ]
    recent_count = len(recent_combat)
    max_recent_age = max((int(num(item["age"])) for item in recent_combat), default=None)
    # 贴脸敌核记忆合并：survey 库 core_hunts 跨 run 累积测绘，距我方核
    # <= SURVEY_MEMORY_RADIUS 且目击距今 <= SURVEY_MEMORY_WINDOW 的记忆补进列表。
    survey = load_tenant_survey_cached(root, tenant, now_ms=now_ms).get("survey")
    if isinstance(survey, dict) and survey.get("coreCells") and our_core is not None:
        for mem in survey["coreCells"]:
            if not isinstance(mem, dict):
                continue
            owner = mem.get("owner")
            owner = str(owner) if isinstance(owner, str) and owner else None
            if owner is None or owner in seen_cores:
                continue
            pos = [finite_number(mem.get("x")), finite_number(mem.get("y"))]
            if pos[0] is None or pos[1] is None:
                continue
            tick = finite_number(mem.get("tick", 0))
            if tick is None or latest_tick - int(tick) > SURVEY_MEMORY_WINDOW:
                continue
            if _chebyshev(pos, our_core) > SURVEY_MEMORY_RADIUS:
                continue
            seen_cores[owner] = {"position": [int(pos[0]), int(pos[1])], "tick": int(tick)}
    enemy_cores: list[dict[str, Any]] = []
    for username, info in seen_cores.items():
        profile = next((p for p in profiles or () if p.get("username") == username), None)
        distance = _chebyshev(info["position"], our_core) if our_core is not None else None
        if distance is None:
            raid = {"tier": "UNKNOWN", "reason": "no_friendly_core"}
        else:
            raid = assess_raid_risk(
                enemy_core_distance=distance,
                combat_units_near=recent_count,
                tier=str(profile.get("tier") or "STANDARD") if profile else "STANDARD",
                fresh_sighting=latest_tick - int(num(info["tick"])) <= CORE_HUNT_STICKY_TICKS,
            )
        enemy_cores.append(
            {
                "username": username,
                "position": info["position"],
                "lastSeenTick": int(num(info["tick"])),
                "tier": str(profile.get("tier") or "STANDARD") if profile else "STANDARD",
                "damageRank": profile.get("rank") if profile else None,
                "distanceToFriendlyCore": distance,
                "raidRisk": raid["tier"],
                "raidReason": raid["reason"],
                "raidActivityAge": max_recent_age,
            }
        )
    enemy_cores.sort(key=lambda item: (-int(num(item["lastSeenTick"])), str(item["username"])))
    enemy_unit_memory = [
        {
            "id": unit_id,
            "unitType": u["unitType"],
            "position": u["position"],
            "lastSeenTick": int(num(u["tick"])),
        }
        for unit_id, u in enemy_unit_by_id.items()
    ]
    enemy_unit_memory.sort(key=lambda item: item["lastSeenTick"], reverse=True)
    enemy_unit_memory = enemy_unit_memory[:ENEMY_UNIT_MEMORY_LIMIT]
    return {
        "tenant": tenant,
        "runId": run_dir,
        "enemyCores": enemy_cores,
        "enemyUnits": len(enemy_unit_by_id),
        "enemyUnitSightings": enemy_unit_sightings,
        "enemyUnitMemory": enemy_unit_memory,
        "ourCore": our_core,
        "combatUnitsNearCore": recent_count,
        "raidActivityAge": max_recent_age,
    }


def load_alliance_intel(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """``/api/intel`` payload (TS ``loadAllianceIntel``, no 30s cache).

    Fail-open: an empty data root yields ``generatedAt`` plus one
    ``{tenant, runId: null, enemyCores: [], enemyUnits: 0}`` entry per tenant.
    """
    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    leaderboard = load_leaderboard_intel(root, now_ms=now)
    profiles: list[dict[str, Any]] = []
    if leaderboard:
        raw_profiles = leaderboard.get("profiles")
        if isinstance(raw_profiles, list):
            profiles = [item for item in raw_profiles if isinstance(item, dict)]
    tenants: list[dict[str, Any]] = []
    enemies: list[dict[str, Any]] = []
    total_enemy_cores = 0
    for tenant in TENANTS:
        scanned = _scan_tenant_intel(root, tenant, profiles, now)
        if scanned is None:
            tenants.append({"tenant": tenant, "runId": None, "enemyCores": [], "enemyUnits": 0})
            continue
        tenants.append(scanned)
        for entry in scanned["enemyCores"]:
            enemies.append({**entry, "tenant": tenant})
        total_enemy_cores += len(scanned["enemyCores"])
    enemies.sort(key=lambda item: (-int(num(item["lastSeenTick"])), str(item["username"])))
    # 信标状态 + 载者推断：轨迹最近点 = 当前位置；近 12 tick 内移动过
    # = 载者活动；距信标 <= 30 的已知敌核心 = 载者猜测。
    beacons: list[dict[str, Any]] = []
    for tenant_view in tenants:
        if not tenant_view.get("runId"):
            continue
        trail = load_beacon_trail(root, str(tenant_view["tenant"]))
        if not trail:
            continue
        last = trail[-1]
        prev = trail[-2] if len(trail) >= 2 else None
        moving = (
            prev is not None
            and int(num(last["tick"])) - int(num(prev["tick"])) <= 12
            and (last["x"] != prev["x"] or last["y"] != prev["y"])
        )
        carrier_guess: str | None = None
        best = 31
        for entry in enemies:
            distance = _chebyshev(entry["position"], [last["x"], last["y"]])
            if distance < best:
                best = distance
                carrier_guess = str(entry["username"])
        carrier_dist = best if best <= 30 else None
        beacons.append(
            {
                "tenant": tenant_view["tenant"],
                "x": last["x"],
                "y": last["y"],
                "tick": last["tick"],
                "moving": moving,
                "carrierGuess": carrier_guess,
                "carrierDist": carrier_dist,
            }
        )
    return {
        "generatedAt": iso_utc(now),
        "tenants": tenants,
        "enemies": enemies,
        "totalEnemyCores": total_enemy_cores,
        "beacons": beacons,
    }


def build_encountered_index_from_enemies(
    enemies: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Username -> per-tenant encounter entries (TS ``buildEncounteredIndex`` body)."""
    index: dict[str, list[dict[str, Any]]] = {}
    for enemy in enemies:
        username = enemy.get("username")
        if not username:
            continue
        tenant = str(enemy.get("tenant"))
        entries = index.get(username)
        if entries is None:
            entries = []
            index[username] = entries
        if not any(entry.get("tenant") == tenant for entry in entries):
            entries.append(
                {
                    "tenant": tenant,
                    "lastSeenTick": enemy.get("lastSeenTick"),
                    "distanceToFriendlyCore": enemy.get("distanceToFriendlyCore"),
                    "raidRisk": enemy.get("raidRisk"),
                }
            )
    return index


def build_encountered_index(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build the encountered-player index from ``load_alliance_intel().enemies``."""
    payload = load_alliance_intel(data_root, now_ms=now_ms)
    return build_encountered_index_from_enemies(payload["enemies"])
