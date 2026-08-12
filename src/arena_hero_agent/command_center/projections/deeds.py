"""Narrative deed stream (port of legacy ``deeds.ts``).

Ports ``loadDeeds`` from the TypeScript oracle: a cross-run narrative deed
aggregator layered as (1) rare ★3-4 events scanned from recent calibration
``after.state.events``, (2) ★2 milestones aggregated from the survey database
(harvest/spend/birth/death integer thresholds located by SQL), and (3) ★1
regular events with per-tenant caps. ``tenant=all`` merges the four tenants.

``/api/deeds?tenant=all|tN&limit=N``. Empty root fail-open (``[]``), never a
500.

Registered differences from the TS oracle: the 45 s memory cache and the
``setImmediate`` time-slice yielding are not ported (Python recomputes
synchronously per request; output shape unchanged). The TS ``num`` helper in
this module returns ``null`` for non-finite values (unlike ``intel.ts`` which
returns ``0``); the port uses ``finite_number`` to match.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..jsonl import calibration_dir, list_cases, parse_tick, runs_by_max_tick
from ..paths import TENANTS, survey_db_path, validate_data_root
from ._common import finite_number

__all__ = [
    "NOTABLE_KINDS",
    "deed_from_event",
    "deed_from_notable_row",
    "load_deeds",
]

RUN_SCAN = 6
CASE_LIMIT = 12
REGULAR_CAP = 20
MOVE_CAP = 2

NOTABLE_KINDS: frozenset[str] = frozenset(
    {
        "CORE_DESTROYED",
        "CORE_RESOURCES_CAPTURED",
        "CORE_RESOURCE_OVERFLOW_DESTROYED",
        "PICKUP_BEACON_SUCCEEDED",
        "DROP_BEACON_SUCCEEDED",
        "SELF_DESTRUCT",
        "UNIT_DESTROYED",
    }
)


def _num(value: object) -> int | float | None:
    """TS ``deeds.ts`` ``num``: finite number or ``null`` (never ``0``)."""
    return finite_number(value)


def _pos(value: object) -> list[int | float] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    x = finite_number(value[0])
    y = finite_number(value[1])
    if x is None or y is None:
        return None
    return [x, y]


def _unit_type_name(value: object) -> str:
    return value if isinstance(value, str) and value.strip() else "单位"


def deed_from_event(
    ev: dict[str, Any],
    tenant: str,
    file_tick: int,
    our_core_ids: frozenset[str] | set[str] | None = None,
) -> dict[str, Any] | None:
    """Raw event -> narrative deed (★1-4); unrecognised noise returns ``None``."""
    core_ids = our_core_ids if our_core_ids is not None else frozenset()
    kind = str(ev.get("event_type") or "").upper()
    tick = _num(ev.get("tick"))
    if tick is None:
        tick = file_tick
    values = ev.get("values")
    if not isinstance(values, dict):
        values = {}
    p = _pos(ev.get("position"))
    actor = ev.get("actor_id") if isinstance(ev.get("actor_id"), str) else None
    target = ev.get("target_id") if isinstance(ev.get("target_id"), str) else None
    id_base = f"{tenant}:{tick}:{ev.get('event_id') or ''}"
    amount = _num(values.get("amount"))
    if amount is None:
        amount = _num(values.get("damage"))

    base: dict[str, Any] = {
        "tick": tick,
        "tenant": tenant,
        "position": p,
        "actor": actor,
        "target": target,
    }

    if kind == "CORE_DESTROYED":
        reason = ev.get("reason_code") if isinstance(ev.get("reason_code"), str) else None
        raw_by = values.get("destroyed_by")
        if isinstance(raw_by, list):
            by_list = [u for u in raw_by if isinstance(u, str)]
        elif isinstance(raw_by, str) and raw_by.strip():
            by_list = [raw_by]
        else:
            by_list = []
        is_self_destruct = reason == "SELF_DESTRUCT"
        is_our = (
            not is_self_destruct
            and isinstance(ev.get("target_id"), str)
            and ev.get("target_id") in core_ids
        )
        raw_target_id = ev.get("target_id")
        tag = raw_target_id[:8] if isinstance(raw_target_id, str) else None
        by_text = "、".join(by_list) if by_list else None
        if is_self_destruct:
            return {
                **base,
                "id": f"{id_base}:core_destroyed",
                "star": 3,
                "kind": kind,
                "title": "核心自爆",
                "detail": f"核心在 ({p[0]},{p[1]}) 自爆放弃" if p else "核心自爆放弃",
            }
        if is_our:
            return {
                **base,
                "id": f"{id_base}:core_destroyed",
                "star": 4,
                "kind": kind,
                "title": "我方核心被摧毁 ⚠",
                "detail": (
                    f"我方核心 {tag} 被 {by_text} 摧毁"
                    if by_text
                    else (
                        f"我方核心 {tag} 在 ({p[0]},{p[1]}) 被摧毁"
                        if p
                        else f"我方核心 {tag} 被摧毁"
                    )
                ),
                "target": ev.get("target_id") if isinstance(ev.get("target_id"), str) else None,
            }
        return {
            **base,
            "id": f"{id_base}:core_destroyed",
            "star": 4,
            "kind": kind,
            "title": "敌方核心被摧毁",
            "detail": (
                f"敌方核心 {tag} 被 {by_text} 摧毁"
                if by_text
                else (f"敌方核心 {tag} 被摧毁" if tag else "敌方核心被摧毁")
            ),
            "target": ev.get("target_id") if isinstance(ev.get("target_id"), str) else None,
        }

    if kind == "CORE_RESOURCES_CAPTURED":
        available = _num(values.get("available"))
        capacity = _num(values.get("capacity"))
        return {
            **base,
            "id": f"{id_base}:captured",
            "star": 3,
            "kind": kind,
            "title": "夺取核心资源",
            "detail": (
                f"夺取敌方核心资源 {amount if amount is not None else '?'}"
                f"（可用 {available if available is not None else '?'}/"
                f"{capacity if capacity is not None else '?'}）"
            ),
        }
    if kind == "CORE_RESOURCE_OVERFLOW_DESTROYED":
        return {
            **base,
            "id": f"{id_base}:overflow",
            "star": 3,
            "kind": kind,
            "title": "核心资源溢出自毁",
            "detail": "资源溢出导致核心自毁",
        }
    if kind == "PICKUP_BEACON_SUCCEEDED":
        return {
            **base,
            "id": f"{id_base}:pickup",
            "star": 3,
            "kind": kind,
            "title": "拾取信标",
            "detail": f"在 ({p[0]},{p[1]}) 拾取信标" if p else "拾取信标",
        }
    if kind == "DROP_BEACON_SUCCEEDED":
        return {
            **base,
            "id": f"{id_base}:drop",
            "star": 3,
            "kind": kind,
            "title": "放置信标",
            "detail": f"在 ({p[0]},{p[1]}) 放置信标" if p else "放置信标",
        }
    if kind == "SELF_DESTRUCT":
        return {
            **base,
            "id": f"{id_base}:self_destruct",
            "star": 3,
            "kind": kind,
            "title": "单位自爆",
            "detail": "单位自爆",
        }
    if kind == "UNIT_DESTROYED":
        return {
            **base,
            "id": f"{id_base}:unit_destroyed",
            "star": 2,
            "kind": kind,
            "title": "单位阵亡",
            "detail": f"单位在 ({p[0]},{p[1]}) 阵亡" if p else "单位阵亡",
        }
    if kind == "CORE_SPAWN_SUCCEEDED":
        cost = _num(values.get("cost"))
        return {
            **base,
            "id": f"{id_base}:spawn",
            "star": 2,
            "kind": kind,
            "title": "核心产兵",
            "detail": (
                f"核心产出 {_unit_type_name(values.get('unit_type'))}"
                f"（消耗 {cost if cost is not None else '?'} 资源）"
            ),
            "target": target,
        }
    if kind == "DEPOSIT_SUCCEEDED":
        capacity = _num(values.get("capacity"))
        remaining = _num(values.get("remaining"))
        return {
            **base,
            "id": f"{id_base}:deposit",
            "star": 2,
            "kind": kind,
            "title": "交付资源",
            "detail": (
                f"交付 {amount if amount is not None else '?'} 资源"
                f"（容量 {capacity if capacity is not None else '?'}，"
                f"剩余 {remaining if remaining is not None else '?'}）"
            ),
        }
    if kind == "HARVEST_SUCCEEDED":
        return {
            **base,
            "id": f"{id_base}:harvest",
            "star": 1,
            "kind": kind,
            "title": "采集资源",
            "detail": (
                f"在 ({p[0]},{p[1]}) 采集 {amount if amount is not None else '?'} 资源"
                if p
                else f"采集 {amount if amount is not None else '?'} 资源"
            ),
        }
    if kind == "UNIT_DAMAGED":
        damage = _num(values.get("damage"))
        hp = _num(values.get("hp"))
        return {
            **base,
            "id": f"{id_base}:damaged",
            "star": 1,
            "kind": kind,
            "title": "单位受击",
            "detail": (
                f"受到 {damage if damage is not None else '?'} 点伤害"
                f"（HP {hp if hp is not None else '?'}）"
            ),
        }
    if kind == "SHOT_HIT":
        return {
            **base,
            "id": f"{id_base}:shot",
            "star": 1,
            "kind": kind,
            "title": "命中敌军",
            "detail": f"造成 {amount if amount is not None else '?'} 点伤害",
        }
    if kind == "CORE_MOVE_SUCCEEDED":
        return {
            **base,
            "id": f"{id_base}:core_move",
            "star": 1,
            "kind": kind,
            "title": "核心移动",
            "detail": f"核心移动至 ({p[0]},{p[1]})" if p else "核心移动",
        }
    if kind in (
        "HEAL_SUCCEEDED",
        "UNIT_HEAL_SUCCEEDED",
        "CORE_HEAL_SUCCEEDED",
        "REPAIR_SHIELD_SUCCEEDED",
    ):
        return {
            **base,
            "id": f"{id_base}:heal",
            "star": 1,
            "kind": kind,
            "title": "治疗/维修",
            "detail": f"恢复 {amount if amount is not None else '?'} 点",
        }
    return None


def _recent_runs(data_root: str | os.PathLike[str], tenant: str) -> list[str]:
    return [str(item["run"]) for item in runs_by_max_tick(data_root, tenant)[:RUN_SCAN]]


def _dedupe_key(deed: dict[str, Any]) -> str | None:
    if deed["star"] < 2:
        return None
    if deed["kind"] not in NOTABLE_KINDS:
        return None
    return f"{deed['tick']}:{deed['kind']}:{deed['actor'] or ''}:{deed['target'] or ''}"


def _collect_event_deeds(data_root: str | os.PathLike[str], tenant: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    res_samples: list[dict[str, Any]] = []
    root = validate_data_root(data_root)
    for run in _recent_runs(root, tenant):
        files = list_cases(root, tenant, run)[-CASE_LIMIT:]
        for case_file in files:
            file_tick = parse_tick(case_file)
            path = calibration_dir(root, tenant) / run / "cases" / case_file
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            after = raw.get("after") if isinstance(raw.get("after"), dict) else None
            before = raw.get("before") if isinstance(raw.get("before"), dict) else None
            after_state = after.get("state") if after is not None else None
            after_state = after_state if isinstance(after_state, dict) else None
            before_state = before.get("state") if before is not None else None
            before_state = before_state if isinstance(before_state, dict) else None
            resources = after_state.get("resources") if after_state is not None else None
            res = _num(resources)
            if res is not None:
                res_samples.append({"tick": file_tick, "res": res})
            our_core_ids: set[str] = set()
            objects = before_state.get("objects") if before_state is not None else None
            if isinstance(objects, list):
                for obj in objects:
                    if (
                        isinstance(obj, dict)
                        and obj.get("kind") == "CORE"
                        and obj.get("controlled") is True
                        and isinstance(obj.get("id"), str)
                    ):
                        our_core_ids.add(obj["id"])
            raw_events = after_state.get("events") if after_state is not None else None
            if before_state is not None and raw_events is None:
                raw_events = before_state.get("events")
            if not isinstance(raw_events, list):
                continue
            for ev in raw_events:
                if not isinstance(ev, dict):
                    continue
                deed = deed_from_event(ev, tenant, file_tick, our_core_ids)
                if deed is not None:
                    out.append(deed)
    if res_samples:
        res_samples.sort(key=lambda item: item["tick"])
        peak = max(res_samples, key=lambda item: item["res"])
        current = 0
        for sample in res_samples:
            while current + 1000 <= sample["res"]:
                current += 1000
                out.append(
                    {
                        "id": f"{tenant}:milestone:resources:{current}",
                        "tick": sample["tick"],
                        "tenant": tenant,
                        "star": 2,
                        "kind": "MILESTONE_RESOURCES",
                        "title": f"资源突破 · {current}",
                        "detail": f"单 tick 资源达到 {current}（峰值 {peak['res']}）",
                        "position": None,
                        "actor": None,
                        "target": None,
                    }
                )
    return out


def _survey_read(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _collect_milestone_deeds(
    data_root: str | os.PathLike[str], tenant: str
) -> list[dict[str, Any]]:
    path = survey_db_path(data_root, tenant)
    connection = _survey_read(path)
    if connection is None:
        return []
    out: list[dict[str, Any]] = []
    try:

        def cnt(sql: str) -> int:
            try:
                row = connection.execute(sql).fetchone()
                return int(_num(row[0]) or 0) if row else 0
            except sqlite3.Error:
                return 0

        def tick_at(sql: str) -> int | float | None:
            try:
                row = connection.execute(sql).fetchone()
                return _num(row[0]) if row else None
            except sqlite3.Error:
                return None

        harvest_count = cnt(
            "SELECT COUNT(*) AS c FROM resource_events WHERE event_type = 'HARVEST_SUCCEEDED'"
        )
        spend_total = cnt("SELECT COALESCE(SUM(amount),0) AS c FROM core_spends")
        birth_count = cnt("SELECT COUNT(*) AS c FROM unit_lifecycle")
        death_count = cnt("SELECT COUNT(*) AS c FROM unit_lifecycle WHERE death_tick IS NOT NULL")
        for t in range(50, harvest_count + 1, 50):
            tick = tick_at(
                "SELECT tick FROM resource_events WHERE event_type = 'HARVEST_SUCCEEDED' "
                f"ORDER BY tick, id LIMIT 1 OFFSET {t - 1}"
            )
            if tick is not None:
                out.append(
                    {
                        "id": f"{tenant}:milestone:harvest:{t}",
                        "tick": tick,
                        "tenant": tenant,
                        "star": 2,
                        "kind": "MILESTONE_HARVEST",
                        "title": f"采集里程碑 · {t} 次",
                        "detail": f"累计成功采集 {t} 次矿",
                        "position": None,
                        "actor": None,
                        "target": None,
                    }
                )
        for t in range(1000, int(spend_total) + 1, 1000):
            tick = tick_at(f"SELECT tick FROM core_spends ORDER BY tick, id LIMIT 1 OFFSET {t - 1}")
            if tick is not None:
                out.append(
                    {
                        "id": f"{tenant}:milestone:spend:{t}",
                        "tick": tick,
                        "tenant": tenant,
                        "star": 2,
                        "kind": "MILESTONE_SPEND",
                        "title": f"核心消费 · {t}",
                        "detail": f"核心累计消费 {t} 资源",
                        "position": None,
                        "actor": None,
                        "target": None,
                    }
                )
        for t in range(25, birth_count + 1, 25):
            tick = tick_at(
                "SELECT birth_tick AS tick FROM unit_lifecycle ORDER BY birth_tick, "
                f"rowid LIMIT 1 OFFSET {t - 1}"
            )
            if tick is not None:
                out.append(
                    {
                        "id": f"{tenant}:milestone:birth:{t}",
                        "tick": tick,
                        "tenant": tenant,
                        "star": 2,
                        "kind": "MILESTONE_BIRTH",
                        "title": f"累计产兵 · {t}",
                        "detail": f"累计产出 {t} 个单位",
                        "position": None,
                        "actor": None,
                        "target": None,
                    }
                )
        for t in range(10, death_count + 1, 10):
            tick = tick_at(
                "SELECT death_tick AS tick FROM unit_lifecycle WHERE death_tick IS NOT NULL "
                f"ORDER BY death_tick, rowid LIMIT 1 OFFSET {t - 1}"
            )
            if tick is not None:
                out.append(
                    {
                        "id": f"{tenant}:milestone:death:{t}",
                        "tick": tick,
                        "tenant": tenant,
                        "star": 1,
                        "kind": "MILESTONE_DEATH",
                        "title": f"累计阵亡 · {t}",
                        "detail": f"累计阵亡 {t} 个单位",
                        "position": None,
                        "actor": None,
                        "target": None,
                    }
                )
    finally:
        connection.close()
    return out


def deed_from_notable_row(
    row: dict[str, Any],
    tenant: str,
) -> dict[str, Any] | None:
    """survey-db notable_events row -> narrative deed (★2-4)."""
    kind = row["event_type"]
    if kind not in NOTABLE_KINDS:
        return None
    x = _num(row.get("x"))
    y = _num(row.get("y"))
    position: list[int | float] | None = [x, y] if x is not None and y is not None else None
    tick = row["tick"]
    actor = row.get("actor_id")
    target = row.get("target_id")
    deed_id = f"{tenant}:{tick}:{kind}:{actor or ''}:{target or ''}"
    base: dict[str, Any] = {
        "id": deed_id,
        "tick": tick,
        "tenant": tenant,
        "position": position,
        "actor": actor,
        "target": target,
    }
    amount = _num(row.get("amount"))
    if kind == "CORE_DESTROYED":
        reason = row.get("reason_code")
        by_list: list[str] = []
        raw_by = row.get("destroyed_by")
        if raw_by:
            try:
                parsed = json.loads(raw_by)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                by_list = [u for u in parsed if isinstance(u, str)]
        is_self_destruct = reason == "SELF_DESTRUCT"
        is_our = not is_self_destruct and row.get("is_our_core") == 1
        tag = target[:8] if isinstance(target, str) else None
        by_text = "、".join(by_list) if by_list else None
        if is_self_destruct:
            return {
                **base,
                "star": 3,
                "kind": kind,
                "title": "核心自爆",
                "detail": f"核心在 ({position[0]},{position[1]}) 自爆放弃"
                if position
                else "核心自爆放弃",
            }
        if is_our:
            return {
                **base,
                "star": 4,
                "kind": kind,
                "title": "我方核心被摧毁 ⚠",
                "detail": (
                    f"我方核心 {tag} 被 {by_text} 摧毁"
                    if by_text
                    else (
                        f"我方核心 {tag} 在 ({position[0]},{position[1]}) 被摧毁"
                        if position
                        else f"我方核心 {tag} 被摧毁"
                    )
                ),
            }
        return {
            **base,
            "star": 4,
            "kind": kind,
            "title": "敌方核心被摧毁",
            "detail": (
                f"敌方核心 {tag} 被 {by_text} 摧毁"
                if by_text
                else (f"敌方核心 {tag} 被摧毁" if tag else "敌方核心被摧毁")
            ),
        }
    if kind == "CORE_RESOURCES_CAPTURED":
        return {
            **base,
            "star": 3,
            "kind": kind,
            "title": "夺取核心资源",
            "detail": f"夺取敌方核心资源 {amount if amount is not None else '?'}",
        }
    if kind == "CORE_RESOURCE_OVERFLOW_DESTROYED":
        return {
            **base,
            "star": 3,
            "kind": kind,
            "title": "核心资源溢出自毁",
            "detail": "资源溢出导致核心自毁",
        }
    if kind == "PICKUP_BEACON_SUCCEEDED":
        return {
            **base,
            "star": 3,
            "kind": kind,
            "title": "拾取信标",
            "detail": f"在 ({position[0]},{position[1]}) 拾取信标" if position else "拾取信标",
        }
    if kind == "DROP_BEACON_SUCCEEDED":
        return {
            **base,
            "star": 3,
            "kind": kind,
            "title": "放置信标",
            "detail": f"在 ({position[0]},{position[1]}) 放置信标" if position else "放置信标",
        }
    if kind == "SELF_DESTRUCT":
        return {**base, "star": 3, "kind": kind, "title": "单位自爆", "detail": "单位自爆"}
    if kind == "UNIT_DESTROYED":
        return {
            **base,
            "star": 2,
            "kind": kind,
            "title": "单位阵亡",
            "detail": f"单位在 ({position[0]},{position[1]}) 阵亡" if position else "单位阵亡",
        }
    return None


def _collect_notable_deeds(data_root: str | os.PathLike[str], tenant: str) -> list[dict[str, Any]]:
    path = survey_db_path(data_root, tenant)
    connection = _survey_read(path)
    if connection is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        columns = [c[1] for c in connection.execute("PRAGMA table_info(notable_events)").fetchall()]
        has_a11 = "reason_code" in columns
        select = (
            "SELECT tick, event_type, actor_id, target_id, x, y, amount, unit_type, "
            "reason_code, destroyed_by, is_our_core "
            "FROM notable_events ORDER BY tick DESC LIMIT 300"
            if has_a11
            else "SELECT tick, event_type, actor_id, target_id, x, y, amount, unit_type "
            "FROM notable_events ORDER BY tick DESC LIMIT 300"
        )
        rows = connection.execute(select).fetchall()
        seen_rows: set[str] = set()
        for values in rows:
            row: dict[str, Any] = {
                "tick": _num(values[0]) or 0,
                "event_type": str(values[1]),
                "actor_id": values[2] if isinstance(values[2], str) else None,
                "target_id": values[3] if isinstance(values[3], str) else None,
                "x": _num(values[4]),
                "y": _num(values[5]),
                "amount": _num(values[6]),
                "unit_type": values[7] if isinstance(values[7], str) else None,
                "reason_code": None,
                "destroyed_by": None,
                "is_our_core": None,
            }
            if has_a11:
                row["reason_code"] = values[8] if isinstance(values[8], str) else None
                row["destroyed_by"] = values[9] if isinstance(values[9], str) else None
                row["is_our_core"] = values[10] if isinstance(values[10], int) else None
            deed = deed_from_notable_row(row, tenant)
            if deed is None:
                continue
            key = _dedupe_key(deed)
            if key is not None:
                if key in seen_rows:
                    continue
                seen_rows.add(key)
            out.append(deed)
    except sqlite3.Error:
        pass
    finally:
        connection.close()
    return out


def load_deeds(
    data_root: str | os.PathLike[str],
    tenant: str,
    limit: int,
    *,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Deed stream for one tenant or merged across all four (TS ``loadDeeds``)."""
    del now_ms
    key = "all" if tenant == "all" else tenant
    if key == "all":
        parts: list[dict[str, Any]] = []
        for t in TENANTS:
            parts.extend(load_deeds(data_root, t, 500))
        merged = sorted(parts, key=lambda d: (-d["tick"], -d["star"]))
        return merged[: max(1, limit)]

    out: list[dict[str, Any]] = []
    regular = 0
    moves = 0
    seen_notable: set[str] = set()
    for deed in _collect_notable_deeds(data_root, key):
        dedupe = _dedupe_key(deed)
        if dedupe is not None:
            seen_notable.add(dedupe)
        out.append(deed)
    for deed in _collect_event_deeds(data_root, key):
        dedupe = _dedupe_key(deed)
        if dedupe is not None:
            if dedupe in seen_notable:
                continue
            seen_notable.add(dedupe)
        if deed["star"] == 1:
            if deed["kind"] == "CORE_MOVE_SUCCEEDED":
                if moves >= MOVE_CAP:
                    continue
                moves += 1
            else:
                if regular >= REGULAR_CAP:
                    continue
                regular += 1
        out.append(deed)
    out.extend(_collect_milestone_deeds(data_root, key))
    out.sort(key=lambda d: (-d["tick"], -d["star"]))
    return out[: max(1, limit)]
