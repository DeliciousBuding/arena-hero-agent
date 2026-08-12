"""Alliance chief-of-staff advice read model (W25).

Port of the legacy TypeScript ``packages/command-center/lib/alliance-advice.ts``
(2026-08-08 参谋建议层): pure, deterministic, I/O-free decision support that
turns the alliance snapshot + shared survey + leaderboard + enemy heat + mine
patterns/utilization + decision audit + human conflict + exploration coverage +
enemy-core trails into a severity-ranked "what to do next" checklist for the
manual operator. Pure functions only; no I/O, no API imports — the Command
Center projection layer (``command_center/projections/alliance_advice.py``)
stays a thin loader that composes the P5-3 data base.

Eleven advice sources (TS ``loadAllianceAdvice`` sections 1-11):

  1. ECONOMY — member core resources below ``LOW_RESOURCE_WARN``;
  2. MILITARY — zero combat units with enemy cores within
     ``NO_COMBAT_CORE_RADIUS``;
  2.5 THREAT — enemy-heat high-density chunk within ``HEAT_NEAR_CHUNKS`` of a
     friendly core;
  3. THREAT — per-tenant high-threat sectors (threat summary);
  4. INTEL — recent enemy-core sightings by leaderboard aggressor tiers;
  5. CONFLICT — cross-tenant same-cell mine overlaps;
  6. INTEL — leaderboard elite-aggressor baseline;
  7. ECONOMY — mine-pattern active-mine collection opportunities;
  8. ECONOMY/CONFLICT — audit signals (visible-never mines, negative core
     growth, decision stall, human-conflict rejection, mining effectiveness);
  9. INTEL — resurvey targets (stale exploration chunks);
  10. INTEL — gold mines (top by harvest amount);
  11. THREAT — enemy-core approaching/proximity from core-hunt trails.

Determinism notes (parity with the TS oracle):

- ``now_ms`` is injectable (TS ``Date.now()`` / ``new Date().toISOString()``
  are wall clock and not oracle-comparable); callers pass an explicit
  epoch-ms value and every ``at`` / ``generatedAt`` / ``cachedAt`` field is
  derived from it.
- Numbers interpolated into advice strings follow JS ``String(number)``
  semantics (integral floats render without a trailing ``.0``) and JS
  ``toFixed`` semantics (exact-double rounding, half up) via ``_to_fixed``.
- Confidence values use JS ``Math.round`` (round-half-up) semantics; arrays
  rendered into strings follow JS ``String(array)`` (comma-joined).
- Enemy-core movement and threat extraction are ports of ``trails.ts``
  (``computeCoreMovement``) and ``core-threats.ts`` (``collectCoreThreats``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

# --- TS constants ---------------------------------------------------------

LOW_RESOURCE_WARN = 10
NO_COMBAT_CORE_RADIUS = 24
MINE_OPPORTUNITY_RESOURCE = 15
HEAT_COMBAT_THRESHOLD = 50
HEAT_NEAR_CHUNKS = 3
PER_TENANT_THREAT_CAP = 3
ADVICE_LIMIT = 15

DEFAULT_APPROACH_RADIUS = 60
DEFAULT_PROXIMITY_RADIUS = 40
DEFAULT_STALE_AFTER_TICKS = 5000
APPROACH_EPS_CELLS = 0.5

_SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "INFO": 3}


class AdviceSeverity(StrEnum):
    """Advice severity (TS ``AdviceSeverity``)."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    INFO = "INFO"


class AdviceCategory(StrEnum):
    """Advice category (TS ``AdviceCategory``)."""

    ECONOMY = "ECONOMY"
    MILITARY = "MILITARY"
    THREAT = "THREAT"
    CONFLICT = "CONFLICT"
    INTEL = "INTEL"


# --- JS number/string semantics ------------------------------------------


def _iso_utc(now_ms: int) -> str:
    """JS ``new Date(ms).toISOString()`` (``YYYY-MM-DDTHH:MM:SS.mmmZ``)."""
    dt = datetime.fromtimestamp(now_ms / 1000.0, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _js_number(value: int | float) -> str:
    """Render a number the way JS ``String(number)`` does (integral -> no .0)."""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return str(number)


def _js_string(value: object) -> str:
    """JS ``String(value)`` for advice-string interpolation."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _js_number(value)
    return str(value)


def _js_array_string(value: object) -> str:
    """JS ``String(array)`` — comma-joined, ``null``/``undefined`` items empty."""
    if isinstance(value, (list, tuple)):
        return ",".join("" if item is None else _js_string(item) for item in value)
    return _js_string(value)


def _js_round(value: float) -> int:
    """JS ``Math.round`` (round half up toward +infinity)."""
    return math.floor(value + 0.5)


def _to_fixed(value: int | float, digits: int) -> str:
    """JS ``Number.prototype.toFixed`` (exact-double rounding, half up)."""
    quant = Decimal(1).scaleb(-digits)
    return format(Decimal(value).quantize(quant, rounding=ROUND_HALF_UP), "f")


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """JS ``clamp`` (bounded to ``[lo, hi]``)."""
    return min(hi, max(lo, value))


def _num(value: object) -> int | float:
    """Coerce a runtime value to a finite number (TS ``num`` helper)."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else 0
    if isinstance(value, str) and value.strip():
        try:
            parsed = float(value)
        except ValueError:
            return 0
        if not math.isfinite(parsed):
            return 0
        return int(parsed) if parsed.is_integer() else parsed
    return 0


def _confidence(value: float) -> float:
    """JS ``Math.round(clamp(v) * 100) / 100`` (0-1 confidence, half up)."""
    return _js_round(_clamp(value) * 100) / 100


def _chebyshev(first: Sequence[object], second: Sequence[object]) -> int | float:
    """King-move grid distance (TS ``cheb``)."""
    return max(abs(_num(first[0]) - _num(second[0])), abs(_num(first[1]) - _num(second[1])))


def _manhattan(first: Sequence[object], second: Sequence[object]) -> int | float:
    """Manhattan grid distance (TS ``manhattan`` in alliance-advice)."""
    return abs(_num(first[0]) - _num(second[0])) + abs(_num(first[1]) - _num(second[1]))


def _position(value: object) -> tuple[int, int] | None:
    """A ``[x, y]`` position tuple, or ``None`` when malformed."""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 2:
        return (int(_num(value[0])), int(_num(value[1])))
    return None


def _evidence(type_: str, **fields: object) -> dict[str, Any]:
    """One evidence entry; absent fields are omitted (JS ``JSON.stringify``)."""
    item: dict[str, Any] = {"type": type_}
    for key, value in fields.items():
        if value is not None:
            item[key] = value
    return item


def _sub_mapping(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Nested payload sub-document as a mapping (empty when absent)."""
    value: object = parent.get(key)
    if isinstance(value, Mapping):
        return value
    return {}


# --- enemy-core movement and threat extraction (trails.ts / core-threats.ts) --


def compute_core_movement(
    trail: Sequence[Mapping[str, Any]],
    friendly_core: Sequence[object] | None,
) -> dict[str, Any]:
    """Enemy-core movement relative to a friendly core (TS ``computeCoreMovement``).

    - approaching: distance to the friendly core shrank (>= ``APPROACH_EPS_CELLS``);
    - retreating: distance grew; stationary: within epsilon;
    - unknown: trail < 2 points or no friendly core.
    ``speedCellsPerTick`` is the last-two-points displacement / tick delta.
    """
    if not trail or len(trail) < 2 or not friendly_core or len(friendly_core) < 2:
        return {"direction": "unknown", "distToCoreCells": None, "speedCellsPerTick": None}
    a = trail[-2]
    b = trail[-1]
    dist_a = _chebyshev((_num(a.get("x")), _num(a.get("y"))), (friendly_core[0], friendly_core[1]))
    dist_b = _chebyshev((_num(b.get("x")), _num(b.get("y"))), (friendly_core[0], friendly_core[1]))
    d_tick = _num(b.get("tick")) - _num(a.get("tick"))
    speed = abs(_num(b.get("x")) - _num(a.get("x"))) / d_tick if d_tick > 0 else 0
    delta = dist_b - dist_a
    if delta < -APPROACH_EPS_CELLS:
        direction = "approaching"
    elif delta > APPROACH_EPS_CELLS:
        direction = "retreating"
    else:
        direction = "stationary"
    return {"direction": direction, "distToCoreCells": dist_b, "speedCellsPerTick": speed}


def collect_core_threats(
    trails: Sequence[Mapping[str, Any]] | None,
    friendly_core: Sequence[object] | None,
    current_tick: int,
    opts: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Extract approaching / proximity enemy-core threats (TS ``collectCoreThreats``).

    ``trails`` is the ``[{username, trail: [{x, y, tick}]}]`` shape produced by
    the survey-db core-trail loader. Pure; deterministic for a fixed input.
    """
    approach_radius = _num((opts or {}).get("approachRadius", DEFAULT_APPROACH_RADIUS))
    proximity_radius = _num((opts or {}).get("proximityRadius", DEFAULT_PROXIMITY_RADIUS))
    stale_after_ticks = _num((opts or {}).get("staleAfterTicks", DEFAULT_STALE_AFTER_TICKS))
    out: list[dict[str, Any]] = []
    if not friendly_core or len(friendly_core) < 2:
        return out
    for tr in trails or ():
        if not isinstance(tr, Mapping):
            continue
        trail = tr.get("trail")
        if not trail:
            continue
        movement = compute_core_movement(trail, friendly_core)
        last = trail[-1]
        dist = movement.get("distToCoreCells")
        if dist is None:
            dist = _chebyshev(
                (_num(last.get("x")), _num(last.get("y"))), (friendly_core[0], friendly_core[1])
            )
        if not math.isfinite(float(dist)):
            continue
        age = max(0, current_tick - _num(last.get("tick"))) if current_tick > 0 else 0
        stale = age > stale_after_ticks
        if movement.get("direction") == "approaching":
            if dist > approach_radius:
                continue
            out.append(
                {
                    "username": str(tr.get("username") or ""),
                    "kind": "approaching",
                    "distCells": dist,
                    "speedCellsPerTick": movement.get("speedCellsPerTick"),
                    "lastSeenTick": _num(last.get("tick")),
                    "x": _num(last.get("x")),
                    "y": _num(last.get("y")),
                    "stale": stale,
                }
            )
        else:
            if dist > proximity_radius:
                continue
            out.append(
                {
                    "username": str(tr.get("username") or ""),
                    "kind": "proximity",
                    "distCells": dist,
                    "speedCellsPerTick": None,
                    "lastSeenTick": _num(last.get("tick")),
                    "x": _num(last.get("x")),
                    "y": _num(last.get("y")),
                    "stale": stale,
                }
            )
    return sorted(
        out,
        key=lambda item: (_num(item.get("distCells")), -int(bool(item.get("stale")))),
    )


# --- standalone advice builders (TS buildResurveyAdvice / buildGoldMineAdvice) --


def build_resurvey_advice(
    resurvey_targets: Sequence[Mapping[str, Any]],
    *,
    now_ms: int,
) -> list[dict[str, Any]]:
    """Stale exploration chunks -> actionable resurvey advice (TS port).

    Per tenant, keep the 3 stalest targets; staleness >= 5000 ticks is
    MEDIUM (so it survives the 15-item panel cap), otherwise INFO.
    """
    out: list[dict[str, Any]] = []
    by_tenant: dict[str, list[Mapping[str, Any]]] = {}
    for target in resurvey_targets:
        if not isinstance(target, Mapping):
            continue
        near = str(target.get("nearCoreOf") or "")
        by_tenant.setdefault(near, []).append(target)
    for tenant, all_targets in by_tenant.items():
        top3 = sorted(all_targets, key=lambda item: _num(item.get("stalenessTicks")), reverse=True)[
            :3
        ]
        if not top3:
            continue
        top = top3[0]
        stale_max = _num(top.get("stalenessTicks"))
        key = _js_string(top.get("key"))
        dist_chunks = _js_number(_num(top.get("distChunks")))
        last_seen = _js_number(_num(top.get("lastSeenTick")))
        out.append(
            {
                "severity": "MEDIUM" if stale_max >= 5000 else "INFO",
                "category": AdviceCategory.INTEL.value,
                "tenant": tenant,
                "title": f"{tenant} {_js_number(len(top3))} 块旧观测区待补测（陈旧 {_js_number(stale_max)} tick）",  # noqa: E501
                "detail": (
                    f"最旧 {key}（距核 {dist_chunks} chunk，t{last_seen}）"
                    "——refill 模型证伪后按陈旧度重测"
                ),
                "action": "派 EXPLORE worker 定向补测旧观测区（地图记忆刷新，资源可能已变）",
                "weight": -stale_max,
                "confidence": 0.7,
                "evidence": [
                    _evidence(
                        "survey", tenant=tenant, ref=f"resurvey={key} stale={_js_number(stale_max)}"
                    )
                ],
                "at": _iso_utc(now_ms),
            }
        )
    return out


def build_gold_mine_advice(
    tenants: Mapping[str, Any],
    *,
    now_ms: int,
) -> list[dict[str, Any]]:
    """Top-harvest-amount mine per tenant -> "gold mine" advice (TS port).

    Only tenants whose ``topMines.byAmount[0]`` has a positive finite amount
    and a non-empty cell get an entry (MEDIUM / INTEL so it survives the cap).
    """
    out: list[dict[str, Any]] = []
    for tenant, value in tenants.items():
        if not isinstance(value, Mapping):
            continue
        top_mines = value.get("topMines")
        by_amount = top_mines.get("byAmount") if isinstance(top_mines, Mapping) else None
        top = (
            by_amount[0]
            if isinstance(by_amount, Sequence) and by_amount and isinstance(by_amount[0], Mapping)
            else None
        )
        if top is None:
            continue
        amount = _num(top.get("harvestAmount"))
        if amount <= 0:
            continue
        cell = _js_string(top.get("cell"))
        if not cell:
            continue
        harvest_ok = _num(top.get("harvestOk"))
        out.append(
            {
                "severity": "MEDIUM",
                "category": AdviceCategory.INTEL.value,
                "tenant": tenant,
                "title": f"{tenant} 金牌矿 {cell}（累计收益 {_js_number(amount)}）",
                "detail": f"该矿累计采集 {_js_number(harvest_ok)} 次——高价值矿脉，值得守/抢",
                "action": "优先派 worker 守护并持续采集；观察敌人是否觊觎（高价值目标）",
                "weight": -(amount * 100 + 1000),
                "confidence": 0.75,
                "evidence": [
                    _evidence(
                        "survey", tenant=tenant, ref=f"gold={cell} amount={_js_number(amount)}"
                    )
                ],
                "at": _iso_utc(now_ms),
            }
        )
    return out


# --- main composition (TS loadAllianceAdvice) -----------------------------


def build_alliance_advice_payload(
    *,
    now_ms: int,
    snapshot: Mapping[str, Any],
    survey: Mapping[str, Any],
    leaderboard: Mapping[str, Any] | None,
    enemy_heat: Mapping[str, Any],
    mine_patterns: Mapping[str, Any],
    mine_utilization: Mapping[str, Any],
    decision_trends: Mapping[str, Mapping[str, Any]],
    human_conflict: Mapping[str, Mapping[str, Any]],
    mining_effectiveness: Mapping[str, Any],
    exploration: Mapping[str, Any],
    core_trails: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Compose the ``/api/alliance/advice`` payload (TS ``loadAllianceAdvice``).

    All inputs are the loader payloads the Command Center projections produce
    (snapshot members/sightings/threatSummaries, survey conflicts, leaderboard
    intel, enemy heat buckets, mine patterns/utilization, per-tenant decision
    trends, human-conflict rates, mining effectiveness, exploration
    resurvey targets, and per-tenant enemy-core trails). Pure and
    deterministic; ``now_ms`` drives every timestamp.
    """
    raw_members = snapshot.get("members")
    members: Mapping[str, Any] = raw_members if isinstance(raw_members, Mapping) else {}
    sightings = list(snapshot.get("sightings") or ())
    threat_summaries = list(snapshot.get("threatSummaries") or ())
    current_tick = _num(snapshot.get("currentTick"))
    survey_conflicts = (
        list(survey.get("conflicts", {}).get("resourceOverlaps") or ())
        if isinstance(survey.get("conflicts"), Mapping)
        else []
    )
    heat = enemy_heat if isinstance(enemy_heat, Mapping) else {}
    heat_buckets = list(heat.get("buckets") or ())
    heat_tick = _num(heat.get("currentTick"))
    mp_tenants = _sub_mapping(mine_patterns, "tenants")
    mu_tenants = _sub_mapping(mine_utilization, "tenants")
    me_per_tenant = _sub_mapping(mining_effectiveness, "perTenant")
    resurvey_targets = list(exploration.get("resurveyTargets") or ())
    lb = leaderboard if isinstance(leaderboard, Mapping) else None

    out: list[dict[str, Any]] = []

    # 1) economy: member core resources endangered (world state, freshest)
    for m in members.values():
        if not isinstance(m, Mapping):
            continue
        resources = _num(m.get("resources"))
        if resources >= LOW_RESOURCE_WARN:
            continue
        tenant = str(m.get("tenantId") or "")
        population = _js_number(_num(m.get("population")))
        workers = _js_number(_num(m.get("workers")))
        vanguards = _js_number(_num(m.get("vanguards")))
        rangers = _js_number(_num(m.get("rangers")))
        carried = _js_number(_num(m.get("carriedResources")))
        out.append(
            {
                "severity": "CRITICAL" if resources < 5 else "HIGH",
                "category": AdviceCategory.ECONOMY.value,
                "tenant": tenant,
                "title": f"{tenant} 核心资源 {_js_number(resources)} 濒危",
                "detail": f"人口 {population}（工{workers}/锋{vanguards}/射{rangers}），携带 {carried}",  # noqa: E501
                "action": (
                    "立即清点满载 worker 卸货/迁移路线；资源低于 5 无法产兵"
                    if resources < 5
                    else "安排采集优先，暂停非必要 spawn"
                ),
                "weight": -resources,
                "confidence": 0.98 if resources < 3 else (0.95 if resources < 5 else 0.9),
                "evidence": [
                    _evidence(
                        "world", tenant=tenant, ref=f"res={_js_number(resources)} pop={population}"
                    )
                ],
                "at": _iso_utc(now_ms),
            }
        )

    # 2) military: zero combat units with enemy cores adjacent (pure snapshot)
    for m in members.values():
        if not isinstance(m, Mapping):
            continue
        if _num(m.get("vanguards")) + _num(m.get("rangers")) > 0:
            continue
        core = m.get("core")
        if not isinstance(core, Mapping):
            continue
        core_position = _position(core.get("position"))
        if core_position is None:
            continue
        near = [
            s
            for s in sightings
            if isinstance(s, Mapping)
            and str(s.get("kind")) == "CORE"
            and _manhattan(_position(s.get("position")) or (0, 0), core_position)
            <= NO_COMBAT_CORE_RADIUS
        ]
        if not near:
            continue
        max_age = max(current_tick - _num(s.get("lastSeenTick")) for s in near)
        tenant = str(m.get("tenantId") or "")
        names = "/".join(str(s.get("ownerUsername") or s.get("entityId") or "?") for s in near)
        out.append(
            {
                "severity": "CRITICAL",
                "category": AdviceCategory.MILITARY.value,
                "tenant": tenant,
                "title": f"{tenant} 零战斗单位且敌核邻近",
                "detail": f"{len(near)} 个敌核 ≤{NO_COMBAT_CORE_RADIUS} 格（{names}）",
                "action": "守家优先：产 Vanguard 或远端军事回援；worker 召回半径扩大",
                "weight": -len(near),
                "confidence": _confidence(0.85 - max_age / 4000),
                "evidence": [
                    _evidence(
                        "sighting",
                        tenant=str(s.get("sourceTenant") or ""),
                        ref=str(s.get("ownerUsername") or s.get("entityId") or "?"),
                        ageTicks=current_tick - _num(s.get("lastSeenTick")),
                    )
                    for s in near
                ],
                "at": _iso_utc(now_ms),
            }
        )

    # 2.5) threat: high enemy-heat chunk near a friendly core (survey memory)
    for m in members.values():
        if not isinstance(m, Mapping):
            continue
        core = m.get("core")
        if not isinstance(core, Mapping):
            continue
        core_position = _position(core.get("position"))
        if core_position is None:
            continue
        chunk_x = math.floor(core_position[0] / 16)
        chunk_y = math.floor(core_position[1] / 16)
        near = [
            b
            for b in heat_buckets
            if isinstance(b, Mapping)
            and _num(b.get("combatCount")) >= HEAT_COMBAT_THRESHOLD
            and max(abs(_num(b.get("bx")) - chunk_x), abs(_num(b.get("by")) - chunk_y))
            <= HEAT_NEAR_CHUNKS
        ]
        if not near:
            continue
        top = max(near, key=lambda b: _num(b.get("combatCount")))
        age = heat_tick - _num(top.get("lastTick"))
        tenant = str(m.get("tenantId") or "")
        bx = _js_number(_num(top.get("bx")))
        by = _js_number(_num(top.get("by")))
        combat_count = _js_number(_num(top.get("combatCount")))
        out.append(
            {
                "severity": "HIGH",
                "category": AdviceCategory.THREAT.value,
                "tenant": tenant,
                "title": f"{tenant} 核心附近敌情高浓度区",
                "detail": f"(chunk {bx},{by}) 累计 {combat_count} 条敌战斗目击（最近 {_js_number(age)} tick 前）",  # noqa: E501
                "action": "该区域敌方活动密集——守家 + 侦察，避免 worker 裸采经过",
                "weight": -_num(top.get("combatCount")),
                "confidence": _confidence(0.7 - age / 6000),
                "evidence": [
                    _evidence("heat", tenant=tenant, ref=f"chunk {bx},{by}", ageTicks=age)
                ],
                "at": _iso_utc(now_ms),
            }
        )

    # 3) threat: per-tenant high-threat sectors (threat summary)
    for ts in threat_summaries:
        if not isinstance(ts, Mapping):
            continue
        high = ts.get("highDirections") or ()
        if not high:
            continue
        total_score = _num(ts.get("totalScore"))
        tenant = str(ts.get("tenantId") or "")
        joined = "/".join(str(item) for item in high)
        multi = bool(ts.get("multiDirectionPressure"))
        out.append(
            {
                "severity": "HIGH" if total_score > 10 else "MEDIUM",
                "category": AdviceCategory.THREAT.value,
                "tenant": tenant,
                "title": f"{tenant} 威胁集中 {joined}（总分 {_to_fixed(total_score, 1)}）",
                "detail": "多方向压力，注意分兵" if multi else "单方向集中威胁",
                "action": "核心迁移/防御需防多面夹击" if multi else "面向高威胁扇区布防或撤离",
                "weight": -total_score,
                "confidence": 0.8,
                "evidence": [
                    _evidence(
                        "sighting",
                        tenant=tenant,
                        ref="dir=" + "+".join(str(item) for item in high),
                    )
                ],
                "at": _iso_utc(now_ms),
            }
        )

    # 4) intel: recent enemy-core sightings by leaderboard aggressor tier
    tier_rank = {"ELITE_AGGRESSOR": 0, "AGGRESSOR": 1}
    tier_by_user: dict[str, str] = {}
    if lb is not None:
        for profile in lb.get("profiles") or ():
            if isinstance(profile, Mapping):
                tier_by_user[str(profile.get("username") or "")] = str(profile.get("tier") or "")
    for s in sightings:
        if not isinstance(s, Mapping):
            continue
        if str(s.get("kind")) != "CORE":
            continue
        owner = s.get("ownerUsername")
        if not isinstance(owner, str) or not owner:
            continue
        tier = tier_by_user.get(owner, "")
        if tier not in tier_rank:
            continue
        age = current_tick - _num(s.get("lastSeenTick"))
        if age > 500:
            continue
        source = str(s.get("sourceTenant") or "")
        position = s.get("position")
        pos_str = (
            ",".join(_js_number(_num(v)) for v in position)
            if isinstance(position, Sequence) and not isinstance(position, (str, bytes))
            else ""
        )
        out.append(
            {
                "severity": "HIGH" if tier == "ELITE_AGGRESSOR" else "MEDIUM",
                "category": AdviceCategory.INTEL.value,
                "tenant": source,
                "title": f"{'猛攻蛆' if tier == 'ELITE_AGGRESSOR' else '攻击者'} {owner} 核心目击（t{_js_number(age)} tick 前）",  # noqa: E501
                "detail": f"由 {source} 目击 @{pos_str}",
                "action": "提升戒备：守家 + 观察其动向",
                "weight": age,
                "confidence": _confidence(0.95 - age / 500),
                "evidence": [_evidence("sighting", tenant=source, ref=owner, ageTicks=age)],
                "at": _iso_utc(now_ms),
            }
        )

    # 5) conflict: cross-tenant same-cell mine overlaps (shared survey)
    for overlap in survey_conflicts:
        if not isinstance(overlap, Mapping):
            continue
        tenants = _js_array_string(overlap.get("tenants"))
        cell = _js_string(overlap.get("cell"))
        states = _js_array_string(overlap.get("states"))
        last_seen = _js_array_string(overlap.get("lastSeenTicks"))
        out.append(
            {
                "severity": "MEDIUM",
                "category": AdviceCategory.CONFLICT.value,
                "tenant": None,
                "title": f"跨租户抢矿 {cell}",
                "detail": f"{tenants} 同格矿重叠（各 {states}，最后目击 {last_seen}）",
                "action": "保留最新目击租户，其余租户该矿记忆标记 stale/仲裁",
                "weight": 0,
                "confidence": 0.6,
                "evidence": [_evidence("survey", tenant=tenants, ref=f"cell {cell}")],
                "at": _iso_utc(now_ms),
            }
        )

    # 6) intel: leaderboard elite-aggressor baseline (only when fresh enough)
    if lb is not None:
        profiles = [p for p in lb.get("profiles") or () if isinstance(p, Mapping)]
        if profiles:
            elites = [p for p in profiles if str(p.get("tier")) == "ELITE_AGGRESSOR"][:5]
            if elites:
                detail = " ".join(
                    f"{p.get('username')}({_js_number(_num(p.get('damage')))})" for p in elites
                )
                ref = "/".join(str(p.get("username") or "") for p in elites)
                out.append(
                    {
                        "severity": "INFO",
                        "category": AdviceCategory.INTEL.value,
                        "tenant": None,
                        "title": f"排行榜猛攻蛆 {len(elites)} 名（伤害 top10）",
                        "detail": detail,
                        "action": "高伤害玩家可能猛攻——联盟威胁场已加先验，注意近期目击",
                        "weight": 0,
                        "confidence": 0.4 if lb.get("stale") else 0.8,
                        "evidence": [
                            _evidence("leaderboard", ref=ref, ageTicks=_num(lb.get("ageSeconds")))
                        ],
                        "at": _iso_utc(now_ms),
                    }
                )

    # 7) economy: mine-pattern active-mine collection opportunities
    for m in members.values():
        if not isinstance(m, Mapping):
            continue
        tenant = str(m.get("tenantId") or "")
        pat = mp_tenants.get(tenant)
        if not isinstance(pat, Mapping):
            continue
        if _num(pat.get("visible")) == 0:
            continue
        if _num(m.get("resources")) >= MINE_OPPORTUNITY_RESOURCE:
            continue
        top_active = pat.get("topActive")
        top = (
            top_active[0]
            if isinstance(top_active, Sequence)
            and top_active
            and isinstance(top_active[0], Mapping)
            else None
        )
        visible = _js_number(_num(pat.get("visible")))
        top_cell = top.get("cell") if top is not None else None
        detail = (
            f"最近活跃 {_js_string(top_cell)}（seen {_js_number(_num(top.get('seenCount')))}，"
            f"t{_js_number(_num(top.get('lastSeenTick')))}）等"
            if top is not None
            else "活跃矿可派 worker 采集"
        )
        out.append(
            {
                "severity": "INFO",
                "category": AdviceCategory.ECONOMY.value,
                "tenant": tenant,
                "title": f"{tenant} {visible} 个活跃矿可采",
                "detail": detail,
                "action": "优先派 worker 采活跃矿（mine-patterns 推荐）；资源低于 15 补采集",
                "weight": -_num(pat.get("visible")),
                "confidence": 0.75,
                "evidence": [
                    _evidence(
                        "survey",
                        tenant=tenant,
                        ref=f"active={visible} top={'-' if top_cell is None else _js_string(top_cell)}",  # noqa: E501
                    )
                ],
                "at": _iso_utc(now_ms),
            }
        )

    # 8) audit: discovery-utilization gap / negative growth / stall / conflicts
    for m in members.values():
        if not isinstance(m, Mapping):
            continue
        tenant = str(m.get("tenantId") or "")
        mu_tenant = mu_tenants.get(tenant)
        vis_never = _num(mu_tenant.get("visibleNever")) if isinstance(mu_tenant, Mapping) else 0
        if vis_never >= 10:
            out.append(
                {
                    "severity": "HIGH",
                    "category": AdviceCategory.ECONOMY.value,
                    "tenant": tenant,
                    "title": f"{tenant} {_js_number(vis_never)} 个可见矿从未开采",
                    "detail": "已发现未开采（分配缺口）——联盟分工已就近分配，见 audit/mines",
                    "action": "优先派 worker 采可见未开采矿（alliance/mining 候选格）",
                    "weight": -vis_never,
                    "confidence": 0.85,
                    "evidence": [
                        _evidence(
                            "audit", tenant=tenant, ref=f"visibleNever={_js_number(vis_never)}"
                        )
                    ],
                    "at": _iso_utc(now_ms),
                }
            )
    for m in members.values():
        if not isinstance(m, Mapping):
            continue
        tenant = str(m.get("tenantId") or "")
        trend_payload = decision_trends.get(tenant)
        trend = (
            trend_payload.get("trend")
            if isinstance(trend_payload, Mapping)
            and isinstance(trend_payload.get("trend"), Sequence)
            else None
        )
        last = trend[-1] if trend else None
        if not isinstance(last, Mapping):
            continue
        core_delta = _num(last.get("coreDelta"))
        if core_delta < 0:
            tick = last.get("tick")
            cargo_eff = last.get("cargoEff")
            cargo_str = "-" if cargo_eff is None else _to_fixed(_num(cargo_eff) * 100, 0)
            out.append(
                {
                    "severity": "HIGH",
                    "category": AdviceCategory.ECONOMY.value,
                    "tenant": tenant,
                    "title": f"{tenant} 最近窗口核心负增长 {_js_number(core_delta)}",
                    "detail": (
                        f"t{_js_string(tick)} 窗口 coreDelta {_js_number(core_delta)}"
                        f"（cargo {cargo_str}%）"
                    ),
                    "action": "检查满载率/交付失败/手操干扰；按 audit/decisions 归因",
                    "weight": -core_delta,
                    "confidence": 0.8,
                    "evidence": [
                        _evidence(
                            "audit",
                            tenant=tenant,
                            ref=f"coreDelta={_js_number(core_delta)} tick={_js_string(tick)}",
                        )
                    ],
                    "at": _iso_utc(now_ms),
                }
            )
        stall_rate = last.get("stallRate")
        if stall_rate is not None and _num(stall_rate) >= 0.9:
            stall_pct = _js_round(_num(stall_rate) * 100)
            out.append(
                {
                    "severity": "MEDIUM",
                    "category": AdviceCategory.ECONOMY.value,
                    "tenant": tenant,
                    "title": f"{tenant} 决策空转 {_js_number(stall_pct)}%",
                    "detail": "最近窗口 wait 主导（停摆 tick 占比）——目标链/搬运需优化",
                    "action": "commit 目标到矿格（修 planChurn）；校验障碍挡路",
                    "weight": stall_pct,
                    "confidence": 0.75,
                    "evidence": [
                        _evidence("audit", tenant=tenant, ref=f"stallRate={_js_string(stall_rate)}")
                    ],
                    "at": _iso_utc(now_ms),
                }
            )
    for m in members.values():
        if not isinstance(m, Mapping):
            continue
        tenant = str(m.get("tenantId") or "")
        hc_tenant = human_conflict.get(tenant)
        rate = hc_tenant.get("rejectedRate") if isinstance(hc_tenant, Mapping) else None
        if rate is None:
            rate = 0
        if _num(rate) >= 0.3:
            pct = _js_round(_num(rate) * 100)
            out.append(
                {
                    "severity": "MEDIUM",
                    "category": AdviceCategory.CONFLICT.value,
                    "tenant": tenant,
                    "title": f"{tenant} 手操拒绝率 {_js_number(pct)}%",
                    "detail": "手操指令被 agent 端拒绝（常见：核心移动中）——UI 应即时反馈",
                    "action": "核心移动中指令已被 guard 拦截（409）；UI 显示拒绝原因",
                    "weight": pct,
                    "confidence": 0.7,
                    "evidence": [
                        _evidence("audit", tenant=tenant, ref=f"rejectedRate={_js_string(rate)}")
                    ],
                    "at": _iso_utc(now_ms),
                }
            )
    for m in members.values():
        if not isinstance(m, Mapping):
            continue
        tenant = str(m.get("tenantId") or "")
        entry = me_per_tenant.get(tenant)
        if not isinstance(entry, Mapping):
            continue
        assigned = _num(entry.get("assigned"))
        if assigned < 5:
            continue
        stale = _num(entry.get("stale"))
        harvested = _num(entry.get("harvested"))
        open_count = _num(entry.get("open"))
        progress = entry.get("progressRate")
        stale_only = stale > 0 and harvested == 0
        open_only = harvested == 0 and open_count > 0
        if stale_only:
            out.append(
                {
                    "severity": "HIGH",
                    "category": AdviceCategory.ECONOMY.value,
                    "tenant": tenant,
                    "title": f"{tenant} 分工 {_js_number(assigned)} 矿兑现失效（{_js_number(stale)} 失效/0 采到）",  # noqa: E501
                    "detail": "已闭环但全失效——分配距离/路径/承载与就近模型不符",
                    "action": "按 alliance/mining 换就近观测者重分配；校验 worker 路径障碍",
                    "weight": assigned,
                    "confidence": 0.8,
                    "evidence": [
                        _evidence(
                            "audit",
                            tenant=tenant,
                            ref=f"assigned={_js_number(assigned)} stale={_js_number(stale)}",
                        )
                    ],
                    "at": _iso_utc(now_ms),
                }
            )
        elif open_only:
            out.append(
                {
                    "severity": "MEDIUM",
                    "category": AdviceCategory.ECONOMY.value,
                    "tenant": tenant,
                    "title": f"{tenant} 分工 {_js_number(assigned)} 矿 0 兑现（{_js_number(open_count)} 在途）",  # noqa: E501
                    "detail": "联盟就近分配尚未被采集——分配未兑现，需真正派 worker",
                    "action": "派 worker 到分工候选格（alliance/mining）；下轮看兑现率",
                    "weight": assigned,
                    "confidence": 0.75,
                    "evidence": [
                        _evidence(
                            "audit",
                            tenant=tenant,
                            ref=(
                                f"assigned={_js_number(assigned)} open={_js_number(open_count)}"
                                f" progress={_js_string(progress)}"
                            ),
                        )
                    ],
                    "at": _iso_utc(now_ms),
                }
            )
        elif harvested > 0:
            out.append(
                {
                    "severity": "INFO",
                    "category": AdviceCategory.ECONOMY.value,
                    "tenant": tenant,
                    "title": f"{tenant} 分工兑现中（{_js_number(harvested)}/{_js_number(assigned)} 采到）",  # noqa: E501
                    "detail": (
                        f"已采 {_js_number(harvested)} / 在途 {_js_number(open_count)}"
                        f" / 失效 {_js_number(stale)}——闭环中"
                    ),
                    "action": "保持派 worker；失效格按联盟重分配",
                    "weight": -harvested,
                    "confidence": 0.7,
                    "evidence": [
                        _evidence(
                            "audit",
                            tenant=tenant,
                            ref=(
                                f"harvested={_js_number(harvested)}/{_js_number(assigned)}"
                                f" progress={_js_string(progress)}"
                            ),
                        )
                    ],
                    "at": _iso_utc(now_ms),
                }
            )

    # 9) intel: stale exploration chunks -> resurvey targets
    out.extend(build_resurvey_advice(resurvey_targets, now_ms=now_ms))

    # 10) intel: gold mines (top harvest amount, worth defending/raiding)
    out.extend(build_gold_mine_advice(mu_tenants, now_ms=now_ms))

    # 11) threat: enemy-core approaching / proximity from core-hunt trails
    for tenant, m in members.items():
        if not isinstance(m, Mapping):
            continue
        core = m.get("core")
        if not isinstance(core, Mapping):
            continue
        friendly_core = _position(core.get("position"))
        if friendly_core is None:
            continue
        member_tick = _num(m.get("tick"))
        threats = collect_core_threats(
            core_trails.get(tenant), list(friendly_core), int(member_tick)
        )
        threat_items: list[dict[str, Any]] = []
        for ct in threats:
            if not isinstance(ct, Mapping):
                continue
            dist = _num(ct.get("distCells"))
            username = str(ct.get("username") or "")
            x = _js_number(_num(ct.get("x")))
            y = _js_number(_num(ct.get("y")))
            last_seen = _num(ct.get("lastSeenTick"))
            approaching = str(ct.get("kind")) == "approaching"
            if approaching:
                severity = "HIGH" if dist < 30 else "MEDIUM"
                speed = _to_fixed(_num(ct.get("speedCellsPerTick") or 0), 2)
                detail = (
                    f"敌核 {username} 正朝友核移动：距 {_js_number(dist)} 格，速度 {speed}"
                    f" 格/tick，最近目击 {x},{y}"
                )
                action = (
                    "高威胁：立即预备拦截/转移核心，别让敌核贴近"
                    if dist < 30
                    else "提高警觉，向逼近方向预部署防守兵力"
                )
                title = f"{tenant} 敌核逼近（{username} 距 {_js_number(dist)} 格）"
                confidence = 0.7
            else:
                stale = bool(ct.get("stale"))
                severity = (
                    "INFO"
                    if stale
                    else ("HIGH" if dist < 15 else ("MEDIUM" if dist < 25 else "INFO"))
                )
                age_note = (
                    f"（{_js_number(max(0, member_tick - last_seen))} tick 前，可能已离开）"
                    if stale
                    else "（方向待确认，建议侦察）"
                )
                detail = f"敌核 {username} 最近目击距友核 {_js_number(dist)} 格{age_note} @{x},{y}"
                action = (
                    "派侦察确认该方向敌核是否仍在；若已离开则移出威胁清单"
                    if stale
                    else "就近侦察 + 预备防御；若再次目击确认逼近则升级拦截"
                )
                title = f"{tenant} 敌核近距目击（{username} 距 {_js_number(dist)} 格）"
                confidence = 0.4 if stale else 0.6
            threat_items.append(
                {
                    "severity": severity,
                    "category": AdviceCategory.THREAT.value,
                    "tenant": tenant,
                    "title": title,
                    "detail": detail,
                    "action": action,
                    "weight": -dist,
                    "confidence": confidence,
                    "evidence": [
                        _evidence(
                            "sighting",
                            tenant=tenant,
                            ref=(f"core_hunts {username} @{x},{y} tick={_js_number(last_seen)}"),
                        )
                    ],
                    "at": _iso_utc(now_ms),
                }
            )
        threat_items.sort(key=lambda item: (_SEV_ORDER[item["severity"]], _num(item["weight"])))
        out.extend(threat_items[:PER_TENANT_THREAT_CAP])

    # sort by severity then weight (stable, TS comparator parity)
    out.sort(key=lambda item: (_SEV_ORDER[item["severity"]], _num(item["weight"])))
    # dedup: one entry per (category, tenant, title); sort order picks the best
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for advice in out:
        key = f"{advice['category']}|{advice['tenant'] if advice['tenant'] is not None else 'all'}|{advice['title']}"  # noqa: E501
        if key in seen:
            continue
        seen.add(key)
        deduped.append(advice)
    dedup_count = len(out) - len(deduped)
    shown = deduped[:ADVICE_LIMIT]
    avg_confidence = (
        _js_round(sum(_num(item["confidence"]) for item in shown) / len(shown) * 100) / 100
        if shown
        else 0
    )
    return {
        "generatedAt": _iso_utc(now_ms),
        "advice": shown,
        "dedupCount": dedup_count,
        "avgConfidence": avg_confidence,
        "summary": {
            "critical": sum(1 for item in deduped if item["severity"] == "CRITICAL"),
            "high": sum(1 for item in deduped if item["severity"] == "HIGH"),
            "medium": sum(1 for item in deduped if item["severity"] == "MEDIUM"),
            "info": sum(1 for item in deduped if item["severity"] == "INFO"),
        },
        "cachedAt": _iso_utc(now_ms),
    }


__all__ = [
    "ADVICE_LIMIT",
    "APPROACH_EPS_CELLS",
    "AdviceCategory",
    "AdviceSeverity",
    "DEFAULT_APPROACH_RADIUS",
    "DEFAULT_PROXIMITY_RADIUS",
    "DEFAULT_STALE_AFTER_TICKS",
    "HEAT_COMBAT_THRESHOLD",
    "HEAT_NEAR_CHUNKS",
    "LOW_RESOURCE_WARN",
    "MINE_OPPORTUNITY_RESOURCE",
    "NO_COMBAT_CORE_RADIUS",
    "PER_TENANT_THREAT_CAP",
    "build_alliance_advice_payload",
    "build_gold_mine_advice",
    "build_resurvey_advice",
    "collect_core_threats",
    "compute_core_movement",
]
