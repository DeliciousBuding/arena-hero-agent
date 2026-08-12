"""Mine-pattern projection (W25).

Port of the legacy TypeScript ``packages/command-center/lib/mine-patterns.ts``
(``tenantPattern`` / ``loadMinePatterns``): analyze the survey-db resource
ledger into per-tenant mine lifecycle patterns — total / visible / stale
mines, age and seen-count statistics, harvest success, the activity-ranked
``topActive`` list, and (W44 wave 4) the refill model: per-mine
``predictions`` (``predictedNextTick`` / ``dueInTicks``), ``refill`` cycle
statistics, ``absentStats``, ``deadMines``, and ``predictionAccuracy``.

Refill semantics follow the TS oracle 1:1 (A15/A16):

- ``resource_seen_history`` appearance windows (gap <= 5 ticks merged) drive
  the history fallback refill estimates;
- ``resource_absences`` (negative observations: observer covered the cell and
  confirmed no mine) take precedence when present — absence-segment -> first
  re-seen is the real refill cycle; dead mines are cells with a strictly
  consecutive absence segment >= 200 ticks;
- ``refillSource`` is ``absences`` / ``history`` / ``none`` accordingly.

Registered differences from the TS oracle:

- The P5-3 Python survey schema lacks the TS ``sync_meta`` / ``resource_events``
  tables; ``currentTick`` falls back to the P5-3 ``agents`` table ``MAX(tick)``
  (matching ``mines.py``) and a missing ``resource_events`` /
  ``resource_seen_history`` / ``resource_absences`` table degrades to an empty
  ledger instead of failing the whole pattern.
- ``generatedAt``/``cachedAt`` are injectable via ``now_ms``.
"""

from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..goal_store import iso_utc
from ..paths import TENANTS, survey_db_path, validate_data_root
from ._common import current_epoch_ms, num
from .mines import RESOURCE_FRESH_WINDOW_TICKS

__all__ = [
    "DEAD_ABSENT_TICKS",
    "REFILL_GAP_TICKS",
    "compute_absent_stats",
    "compute_dead_mines",
    "compute_prediction_accuracy",
    "compute_refill_predictions",
    "compute_refill_predictions_from_absences",
    "compute_refill_stats",
    "compute_refill_stats_from_absences",
    "load_mine_patterns",
]

_TOP_ACTIVE_LIMIT = 20

# TS ``REFILL_GAP_TICKS``: consecutive ticks closer than this are one window.
REFILL_GAP_TICKS = 5
# TS ``DEAD_ABSENT_TICKS``: strictly consecutive absence >= this -> suspected dead mine.
DEAD_ABSENT_TICKS = 200


def _js_round(value: float) -> int:
    return math.floor(value + 0.5)


def _group_by_cell(rows: list[dict[str, Any]], key: str = "tick") -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for row in rows:
        cell = str(row.get("cell") or "")
        if not cell:
            continue
        out.setdefault(cell, []).append(int(num(row.get(key))))
    return out


def compute_refill_stats(
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """History-window refill cycle stats (TS ``computeRefillStats``)."""
    if not rows:
        return None
    gaps: list[dict[str, Any]] = []
    for cell, ticks in _group_by_cell(rows).items():
        ticks.sort()
        prev_start: int | None = None
        prev_end: int | None = None
        for tick in ticks:
            if prev_end is None or tick - prev_end > REFILL_GAP_TICKS:
                if prev_start is not None:
                    gaps.append({"cell": cell, "gapTicks": tick - prev_start, "lastSeenTick": tick})
                prev_start = tick
            prev_end = tick
    gaps.sort(key=lambda item: item["lastSeenTick"], reverse=True)
    if len(gaps) < 2:
        return {"samples": len(gaps), "avgRefillTicks": None, "recent": gaps[:10]}
    avg = _js_round(sum(item["gapTicks"] for item in gaps) / len(gaps))
    return {"samples": len(gaps), "avgRefillTicks": avg, "recent": gaps[:10]}


def compute_refill_stats_from_absences(
    absences: list[dict[str, Any]],
    seen_history: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Absence-segment -> re-seen refill cycle stats (TS A15)."""
    if not absences:
        return None
    by_cell = _group_by_cell(absences)
    seen_by_cell = _group_by_cell(seen_history)
    gaps: list[dict[str, Any]] = []
    for cell, ticks in by_cell.items():
        ticks.sort()
        segments: list[tuple[int, int]] = []
        start = ticks[0]
        prev_end = ticks[0]
        for tick in ticks[1:]:
            if tick - prev_end > REFILL_GAP_TICKS:
                segments.append((start, prev_end))
                start = tick
            prev_end = tick
        segments.append((start, prev_end))
        seen = sorted(seen_by_cell.get(cell, []))
        for _seg_start, seg_end in segments:
            after = next((value for value in seen if value > seg_end + REFILL_GAP_TICKS), None)
            if after is not None:
                gaps.append({"cell": cell, "gapTicks": after - seg_end, "lastSeenTick": after})
    gaps.sort(key=lambda item: item["lastSeenTick"], reverse=True)
    if not gaps:
        return {"samples": 0, "avgRefillTicks": None, "recent": []}
    avg = _js_round(sum(item["gapTicks"] for item in gaps) / len(gaps))
    return {"samples": len(gaps), "avgRefillTicks": avg, "recent": gaps[:10]}


def compute_refill_predictions(
    rows: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    current_tick: int,
) -> list[dict[str, Any]]:
    """Per-mine refill predictions from appearance windows (TS ``computeRefillPredictions``)."""
    pos_of = {
        str(r.get("cell") or ""): (int(num(r.get("x"))), int(num(r.get("y")))) for r in resources
    }
    out: list[dict[str, Any]] = []
    for cell, ticks in _group_by_cell(rows).items():
        ticks.sort()
        windows: list[tuple[int, int]] = []
        start = ticks[0]
        prev_end = ticks[0]
        for tick in ticks[1:]:
            if tick - prev_end > REFILL_GAP_TICKS:
                windows.append((start, prev_end))
                start = tick
            prev_end = tick
        windows.append((start, prev_end))
        if len(windows) < 2:
            continue
        gaps: list[int] = []
        absents: list[int] = []
        for index in range(1, len(windows)):
            gap = windows[index][0] - windows[index - 1][0]
            gaps.append(gap)
            duration = windows[index - 1][1] - windows[index - 1][0]
            absents.append(gap - duration)
        avg_gap = _js_round(sum(gaps) / len(gaps))
        avg_absent = max(1, _js_round(sum(absents) / len(absents)))
        last_end = windows[-1][1]
        predicted_next = last_end + avg_absent
        x, y = pos_of.get(cell, (0, 0))
        out.append(
            {
                "cell": cell,
                "x": x,
                "y": y,
                "windows": len(windows),
                "avgGapTicks": avg_gap,
                "lastSeenTick": prev_end,
                "predictedNextTick": predicted_next,
                "dueInTicks": predicted_next - current_tick,
            }
        )
    out.sort(
        key=lambda item: item["dueInTicks"] if item["dueInTicks"] is not None else 1_000_000_000
    )
    return out


def compute_refill_predictions_from_absences(
    absences: list[dict[str, Any]],
    seen_history: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    current_tick: int,
) -> list[dict[str, Any]]:
    """Per-mine refill predictions from absence cycles (TS A15)."""
    pos_of = {
        str(r.get("cell") or ""): (int(num(r.get("x"))), int(num(r.get("y")))) for r in resources
    }
    abs_by_cell = _group_by_cell(absences)
    seen_by_cell = _group_by_cell(seen_history)
    out: list[dict[str, Any]] = []
    for cell, ticks in abs_by_cell.items():
        ticks.sort()
        segments: list[tuple[int, int]] = []
        start = ticks[0]
        prev_end = ticks[0]
        for tick in ticks[1:]:
            if tick - prev_end > REFILL_GAP_TICKS:
                segments.append((start, prev_end))
                start = tick
            prev_end = tick
        segments.append((start, prev_end))
        seen = sorted(seen_by_cell.get(cell, []))
        cycles: list[int] = []
        for index in range(len(segments) - 1):
            after = next(
                (value for value in seen if value > segments[index][1] + REFILL_GAP_TICKS), None
            )
            if after is not None:
                cycles.append(after - segments[index][1])
        if not cycles:
            continue
        avg_cycle = _js_round(sum(cycles) / len(cycles))
        last_end = segments[-1][1]
        predicted_next = last_end + avg_cycle
        x, y = pos_of.get(cell, (0, 0))
        out.append(
            {
                "cell": cell,
                "x": x,
                "y": y,
                "windows": len(segments),
                "avgGapTicks": avg_cycle,
                "lastSeenTick": last_end,
                "predictedNextTick": predicted_next,
                "dueInTicks": predicted_next - current_tick,
            }
        )
    out.sort(
        key=lambda item: item["dueInTicks"] if item["dueInTicks"] is not None else 1_000_000_000
    )
    return out


def compute_absent_stats(absences: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Strict consecutive absence-segment length distribution (TS ``computeAbsentStats``)."""
    if not absences:
        return None
    lengths: list[int] = []
    for ticks in _group_by_cell(absences).values():
        ticks.sort()
        start = ticks[0]
        prev = ticks[0]
        for tick in ticks[1:]:
            if tick - prev > 1:
                lengths.append(prev - start)
                start = tick
            prev = tick
        lengths.append(prev - start)
    if not lengths:
        return None
    lengths.sort()

    def percentile(q: float) -> int:
        return lengths[min(len(lengths) - 1, math.floor(len(lengths) * q))]

    return {
        "segCount": len(lengths),
        "medianLen": lengths[len(lengths) // 2],
        "p90Len": percentile(0.9),
        "p99Len": percentile(0.99),
    }


def compute_dead_mines(
    absences: list[dict[str, Any]],
    resources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Suspected dead mines: >= DEAD_ABSENT_TICKS strict consecutive absence (TS A16)."""
    if not absences:
        return []
    pos_of = {
        str(r.get("cell") or ""): (int(num(r.get("x"))), int(num(r.get("y")))) for r in resources
    }
    out: list[dict[str, Any]] = []
    for cell, ticks in _group_by_cell(absences).items():
        ticks.sort()
        start = ticks[0]
        prev = ticks[0]
        max_len = 0
        for tick in ticks[1:]:
            if tick - prev > 1:
                max_len = max(max_len, prev - start)
                start = tick
            prev = tick
        max_len = max(max_len, prev - start)
        if max_len >= DEAD_ABSENT_TICKS:
            x, y = pos_of.get(cell, (0, 0))
            out.append(
                {
                    "cell": cell,
                    "x": x,
                    "y": y,
                    "maxAbsentLen": max_len,
                    "lastAbsentTick": ticks[-1],
                }
            )
    out.sort(key=lambda item: item["maxAbsentLen"], reverse=True)
    return out


def compute_prediction_accuracy(
    predictions: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    current_tick: int,
) -> dict[str, Any] | None:
    """Refill-prediction hit evaluation (TS ``computePredictionAccuracy``)."""
    max_by_cell: dict[str, int] = {}
    for row in rows:
        cell = str(row.get("cell") or "")
        if not cell:
            continue
        tick = int(num(row.get("tick")))
        if tick > max_by_cell.get(cell, -1):
            max_by_cell[cell] = tick
    tolerance = REFILL_GAP_TICKS
    evaluated = 0
    hits = 0
    miss_sum = 0
    for prediction in predictions:
        nxt = prediction.get("predictedNextTick")
        if nxt is None:
            continue
        if current_tick - nxt < tolerance:
            continue
        evaluated += 1
        max_seen = max_by_cell.get(prediction.get("cell"), -1)
        if max_seen >= nxt - tolerance:
            hits += 1
        else:
            miss_sum += current_tick - nxt
    if evaluated == 0:
        return None
    misses = evaluated - hits
    return {
        "evaluated": evaluated,
        "hits": hits,
        "misses": misses,
        "hitRate": _js_round((hits / evaluated) * 1000) / 1000,
        "avgMissOverdue": _js_round(miss_sum / misses) if misses > 0 else None,
    }


def _sync_tick_max(connection: sqlite3.Connection) -> int:
    """Survey watermark: ``sync_meta`` max tick, then ``agents`` max tick (P5-3)."""
    try:
        row = connection.execute("SELECT MAX(last_tick) AS m FROM sync_meta").fetchone()
        if row is not None and row[0] is not None:
            return int(num(row[0]))
    except sqlite3.OperationalError:
        pass
    try:
        row = connection.execute("SELECT MAX(tick) FROM agents").fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None or row[0] is None:
        return 0
    return int(num(row[0]))


def _read_history_rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(f"SELECT cell, tick FROM {table} ORDER BY tick").fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"cell": str(row[0]), "tick": int(num(row[1]))} for row in rows]


def _tenant_pattern(path: Path, tenant: str) -> dict[str, Any]:
    """One tenant's mine pattern (TS ``tenantPattern``)."""
    empty: dict[str, Any] = {
        "tenant": tenant,
        "total": 0,
        "visible": 0,
        "stale": 0,
        "avgAgeTicks": 0,
        "medianSeenCount": 0,
        "harvestSuccessRate": None,
        "harvestSucceeded": 0,
        "harvestFailed": 0,
        "topActive": [],
        "refill": None,
        "refillSource": "none",
        "absentStats": None,
        "deadMines": [],
        "predictions": [],
        "predictionAccuracy": None,
    }
    if not path.is_file():
        return empty
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return empty
    try:
        current_tick = _sync_tick_max(connection)
        rows = connection.execute(
            "SELECT x, y, first_seen_tick AS f, last_seen_tick AS l, seen_count AS n, state"
            " FROM resources"
        ).fetchall()
        entries: list[dict[str, Any]] = []
        resource_cells: list[dict[str, Any]] = []
        total = 0
        visible = 0
        stale = 0
        age_sum = 0
        seen_counts: list[int] = []
        for row in rows:
            total += 1
            last_seen = int(num(row[3]))
            first_seen = int(num(row[2]))
            state = (
                "visible" if last_seen >= current_tick - RESOURCE_FRESH_WINDOW_TICKS else "stale"
            )
            if state == "visible":
                visible += 1
            else:
                stale += 1
            age = max(0, last_seen - first_seen)
            age_sum += age
            seen = int(num(row[4]))
            seen_counts.append(seen)
            n = max(1, seen)
            activity = n / max(1, age)
            cell = f"{int(num(row[0]))},{int(num(row[1]))}"
            entries.append(
                {
                    "cell": cell,
                    "x": int(num(row[0])),
                    "y": int(num(row[1])),
                    "seenCount": seen,
                    "ageTicks": age,
                    "activity": activity,
                    "lastSeenTick": last_seen,
                    "state": state,
                }
            )
            resource_cells.append({"cell": cell, "x": int(num(row[0])), "y": int(num(row[1]))})
        cutoff = current_tick - RESOURCE_FRESH_WINDOW_TICKS
        entries.sort(
            key=lambda entry: (
                -(1 if entry["lastSeenTick"] >= cutoff else 0),
                -entry["activity"],
                -entry["lastSeenTick"],
            )
        )
        median_seen_count = sorted(seen_counts)[len(seen_counts) // 2] if seen_counts else 0
        try:
            event_rows = connection.execute(
                "SELECT event_type AS e, COUNT(*) AS c FROM resource_events GROUP BY event_type"
            ).fetchall()
        except sqlite3.OperationalError:
            event_rows = ()
        succeeded = 0
        failed = 0
        for row in event_rows:
            if str(row[0]) == "HARVEST_SUCCEEDED":
                succeeded = int(num(row[1]))
            elif str(row[0]) == "HARVEST_FAILED":
                failed = int(num(row[1]))
        rate = succeeded / (succeeded + failed) if succeeded + failed > 0 else None
        history_rows = _read_history_rows(connection, "resource_seen_history")
        absence_rows = _read_history_rows(connection, "resource_absences")
        absence_stats = compute_refill_stats_from_absences(absence_rows, history_rows)
        has_absence_samples = absence_stats is not None and absence_stats["samples"] > 0
        refill = absence_stats if has_absence_samples else compute_refill_stats(history_rows)
        predictions = (
            compute_refill_predictions_from_absences(
                absence_rows, history_rows, resource_cells, current_tick
            )
            if has_absence_samples
            else compute_refill_predictions(history_rows, resource_cells, current_tick)
        )
        return {
            "tenant": tenant,
            "total": total,
            "visible": visible,
            "stale": stale,
            "avgAgeTicks": _js_round(age_sum / total) if total > 0 else 0,
            "medianSeenCount": median_seen_count,
            "harvestSuccessRate": None if rate is None else _js_round(rate * 1000) / 1000,
            "harvestSucceeded": succeeded,
            "harvestFailed": failed,
            "topActive": entries[:_TOP_ACTIVE_LIMIT],
            "refill": refill,
            "refillSource": (
                "absences" if has_absence_samples else ("history" if history_rows else "none")
            ),
            "absentStats": compute_absent_stats(absence_rows),
            "deadMines": compute_dead_mines(absence_rows, resource_cells),
            "predictions": predictions,
            "predictionAccuracy": compute_prediction_accuracy(
                predictions, history_rows, current_tick
            ),
        }
    except sqlite3.Error:
        return empty
    finally:
        connection.close()


def load_mine_patterns(
    data_root: str | os.PathLike[str],
    tenant: str = "all",
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load mine-pattern payload (``/api/survey/mine-patterns`` source)."""
    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    tenants = list(TENANTS) if tenant == "all" else [tenant]
    per_tenant: dict[str, Any] = {}
    for t in tenants:
        per_tenant[t] = _tenant_pattern(survey_db_path(root, t), t)
    evaluated = sum(
        int(num((per_tenant[t].get("predictionAccuracy") or {}).get("evaluated")))
        for t in per_tenant
    )
    hits = sum(
        int(num((per_tenant[t].get("predictionAccuracy") or {}).get("hits"))) for t in per_tenant
    )
    abs_tenants = sum(1 for t in per_tenant if per_tenant[t].get("refillSource") == "absences")
    dead_total = sum(len(per_tenant[t].get("deadMines") or ()) for t in per_tenant)
    abs_line = ""
    if abs_tenants > 0:
        lines = []
        for t in per_tenant:
            stats = per_tenant[t].get("absentStats")
            if stats:
                lines.append(f"{t} med={stats.get('medianLen')}/p90={stats.get('p90Len')}")
        abs_line = " ".join(lines)
    if abs_tenants > 0:
        caveat = (
            f"缺席段实证：{abs_line}（tick，median/p90——矿消失后快速 refill）；"
            f"「段结束→重见」预测命中率 {hits}/{evaluated} 低 = 观察者离开的观测间隔，"
            f"不作刷新计时；疑似死矿 {dead_total} 格（≥200 tick 长缺席段）。"
            "派工按 lastSeenTick 新鲜度 + deadMines 剔除。"
        )
    elif evaluated > 0 and hits / evaluated < 0.1:
        caveat = (
            f"观测间隔≠资源缺席：refill 预测已过预期命中率 {hits}/{evaluated}"
            f"（{math.floor(hits / evaluated * 100 + 0.5)}%）——resource_seen_history "
            "只记观测 tick（无 resource_absences 负观测），矿格长时间未被测绘即被误判"
            '"失联/死矿"。建议按 lastSeenTick 新鲜度派工，勿按 refill 预测剔除。'
        )
    else:
        caveat = "refill 预测命中率正常（样本不足或命中率高），可作刷新参考。"
    return {
        "generatedAt": iso_utc(now),
        "tenant": tenant,
        "tenants": per_tenant,
        "modelCaveat": caveat,
        "cachedAt": iso_utc(now),
    }
