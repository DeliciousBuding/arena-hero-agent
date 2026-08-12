"""Survey-db read model + per-tenant survey cache (port of legacy ``survey.ts``).

Ports the survey-db loaders (``loadSurveyDb`` / ``loadChunksDb`` /
``loadLifecycleDb`` / ``loadSpendTrend`` / ``loadUnitLifecycleDb``) and the
route compositions ``loadSurvey`` (``/api/survey``) and ``loadExploration``
(``/api/exploration``, TS ``loadTenantSurveyCached`` + ``loadWorld`` subset)
from the TypeScript oracle. Pure read; every loader is fail-open and returns
the same shape as the TS counterpart when the database is missing (``null`` /
empty array / ``error`` entry) — never 500.

Registered differences from the TS oracle:

- ``generatedAt`` / ``cachedAt`` are injectable via ``now_ms`` (TS
  ``new Date().toISOString()``) and there is no in-memory TTL cache: every
  call reads fresh (cache behavior only, not data).
- The calibration-scan fallback (TS ``loadSurvey``) runs only when the
  survey database is missing; JSON ``undefined`` object keys (missing
  ``hp``/``shield``/``controlled``/``owner_username``) are omitted exactly
  like ``JSON.stringify`` does.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import calibration_dir, latest_run_dir, list_cases, parse_tick
from ..paths import TENANTS, survey_db_path, validate_data_root
from ._common import current_epoch_ms, finite_number, num
from .alliance_survey import TENANT_COLORS
from .mines import RESOURCE_FRESH_WINDOW_TICKS

__all__ = [
    "CHUNK_DEFAULT_MAX_AGE_TICKS",
    "SPEND_TREND_DEFAULT_BUCKET_TICKS",
    "UNIT_DETAIL_DEFAULT_LIMIT",
    "load_chunks_db",
    "load_lifecycle_db",
    "load_spend_trend",
    "load_survey",
    "load_survey_db",
    "load_tenant_survey_cached",
    "load_unit_lifecycle_db",
]

CHUNK_DEFAULT_MAX_AGE_TICKS = 20_000
SPEND_TREND_DEFAULT_BUCKET_TICKS = 1000
UNIT_DETAIL_DEFAULT_LIMIT = 200


def _open_readonly(path: Path) -> sqlite3.Connection | None:
    """Read-only sqlite handle; None when the database cannot be opened."""
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    return connection


def _close(connection: sqlite3.Connection | None) -> None:
    if connection is not None:
        connection.close()


def load_survey_db(data_root: str | os.PathLike[str], tenant: str) -> dict[str, Any] | None:
    """Survey database in the TS ``loadSurveyDb`` shape (null when missing)."""
    root = validate_data_root(data_root)
    path = survey_db_path(root, tenant)
    if not path.is_file():
        return None
    connection = _open_readonly(path)
    if connection is None:
        return None
    try:
        resources_raw = connection.execute(
            "SELECT x, y, last_seen_tick AS tick, first_seen_tick AS firstSeenTick,"
            " state, seen_count AS seenCount FROM resources ORDER BY last_seen_tick DESC"
        ).fetchall()
        harvest_rows = connection.execute(
            "SELECT cell, COUNT(*) AS n, MAX(tick) AS lastTick FROM resource_events"
            " WHERE event_type = 'HARVEST_SUCCEEDED' GROUP BY cell"
        ).fetchall()
        harvest_by_cell = {str(row[0]): (int(num(row[1])), row[2]) for row in harvest_rows}
        obstacles = connection.execute(
            "SELECT x, y, last_seen_tick AS tick FROM obstacles ORDER BY last_seen_tick DESC"
        ).fetchall()
        core_rows = connection.execute(
            "SELECT x, y, last_seen_tick AS tick, owner, source FROM core_hunts"
            " ORDER BY last_seen_tick DESC"
        ).fetchall()
        # Same-owner cores keep the newest location (rows already newest-first).
        seen_owners: set[str] = set()
        cores: list[dict[str, Any]] = []
        for row in core_rows:
            owner = str(row[3]) if row[3] is not None and str(row[3]) != "" else None
            if owner is not None:
                if owner in seen_owners:
                    continue
                seen_owners.add(owner)
            cores.append(
                {"x": row[0], "y": row[1], "tick": row[2], "owner": row[3], "source": row[4]}
            )
        meta = connection.execute(
            "SELECT MAX(last_tick) AS m, SUM(cases_synced) AS c FROM sync_meta"
        ).fetchone()
        tick_max = int(num(meta[0]))
        resources: list[dict[str, Any]] = []
        for row in resources_raw:
            x = int(num(row[0]))
            y = int(num(row[1]))
            last_seen = int(num(row[2]))
            first_seen = int(num(row[3]))
            db_state = str(row[4] or "")
            seen_count = int(num(row[5]))
            age_ticks = max(0, tick_max - last_seen) if tick_max > 0 else 0
            fresh = age_ticks <= RESOURCE_FRESH_WINDOW_TICKS
            harvest = harvest_by_cell.get(f"{x},{y}")
            state = (
                db_state if db_state in ("harvested", "empty") else "visible" if fresh else "stale"
            )
            resources.append(
                {
                    "x": x,
                    "y": y,
                    "tick": last_seen,
                    "firstSeenTick": first_seen,
                    "state": state,
                    "seenCount": seen_count,
                    "ageTicks": age_ticks,
                    "fresh": fresh,
                    "harvestCount": harvest[0] if harvest else 0,
                    "lastHarvestTick": harvest[1] if harvest else None,
                }
            )
        chunk_rows = connection.execute(
            "SELECT chunk_key AS key, last_seen_tick AS lastSeenTick FROM chunks"
            " ORDER BY last_seen_tick DESC"
        ).fetchall()
        chunks: list[dict[str, Any]] = []
        for row in chunk_rows:
            cx, cy = _split_chunk_key(str(row[0]))
            # TS maps Number over the split key; NaN serializes to JSON null.
            chunks.append({"key": row[0], "lastSeenTick": row[1], "cx": cx, "cy": cy})
        return {
            "obstacleCells": [{"x": row[0], "y": row[1], "tick": row[2]} for row in obstacles],
            "resourceCells": resources,
            "coreCells": cores,
            "caseCount": int(num(meta[1])),
            "tickMax": tick_max,
            "fromDb": True,
            "chunks": chunks,
        }
    except sqlite3.Error:
        return None
    finally:
        _close(connection)


def _split_chunk_key(key: str) -> tuple[int | None, int | None]:
    """``cx, cy`` from a ``x,y`` chunk key (None when not finite, TS parity)."""
    parts = key.split(",")
    if len(parts) < 2:
        return None, None
    cx = finite_number(parts[0].strip())
    cy = finite_number(parts[1].strip())
    if cx is None or cy is None:
        return None, None
    return int(cx), int(cy)


def load_chunks_db(
    data_root: str | os.PathLike[str],
    tenant: str,
    max_age_ticks: int = CHUNK_DEFAULT_MAX_AGE_TICKS,
) -> list[dict[str, Any]]:
    """Chunk exploration timestamps, filtered to the ``max_age_ticks`` window."""
    root = validate_data_root(data_root)
    path = survey_db_path(root, tenant)
    if not path.is_file():
        return []
    connection = _open_readonly(path)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT chunk_key AS key, last_seen_tick AS lastSeenTick FROM chunks"
            " ORDER BY last_seen_tick DESC"
        ).fetchall()
        if not rows:
            return []
        max_tick = 0
        for row in rows:
            if num(row[1]) > max_tick:
                max_tick = int(num(row[1]))
        cutoff = max_tick - max_age_ticks
        out: list[dict[str, Any]] = []
        for row in rows:
            if num(row[1]) < cutoff:
                continue
            cx, cy = _split_chunk_key(str(row[0]))
            if cx is None or cy is None:
                continue
            out.append(
                {
                    "key": str(row[0]),
                    "cx": cx,
                    "cy": cy,
                    "lastSeenTick": int(num(row[1])),
                }
            )
        return out
    except sqlite3.Error:
        return []
    finally:
        _close(connection)


def load_lifecycle_db(data_root: str | os.PathLike[str], tenant: str) -> dict[str, Any] | None:
    """Lifecycle summary (units/spends/harvest/recent deaths); null when missing."""
    root = validate_data_root(data_root)
    path = survey_db_path(root, tenant)
    if not path.is_file():
        return None
    connection = _open_readonly(path)
    if connection is None:
        return None
    try:
        units = [
            {"state": row[0], "type": row[1], "count": int(num(row[2]))}
            for row in connection.execute(
                "SELECT current_state AS state, unit_type AS type, COUNT(*) AS count"
                " FROM unit_lifecycle GROUP BY state, unit_type"
            ).fetchall()
        ]
        spends = [
            {"kind": row[0], "count": int(num(row[1])), "total": row[2]}
            for row in connection.execute(
                "SELECT kind, COUNT(*) AS count, SUM(amount) AS total FROM core_spends"
                " GROUP BY kind ORDER BY total DESC"
            ).fetchall()
        ]
        harvests = connection.execute(
            "SELECT COUNT(*) AS count, MAX(tick) AS last_tick FROM resource_events"
            " WHERE event_type = 'HARVEST_SUCCEEDED'"
        ).fetchone()
        fails = connection.execute(
            "SELECT COUNT(*) AS count FROM resource_events WHERE event_type = 'HARVEST_FAILED'"
        ).fetchone()
        recent_deaths = [
            {
                "type": row[0],
                "birthTick": row[1],
                "deathTick": row[2],
                "deathPos": row[3],
                "deathReason": row[4],
            }
            for row in connection.execute(
                "SELECT unit_type AS type, birth_tick AS birthTick,"
                " death_tick AS deathTick, death_pos AS deathPos,"
                " death_reason AS deathReason FROM unit_lifecycle"
                " WHERE death_tick IS NOT NULL ORDER BY death_tick DESC LIMIT 8"
            ).fetchall()
        ]
        last_harvest = harvests[1]
        return {
            "units": units,
            "spends": spends,
            "harvestCount": int(num(harvests[0])),
            "lastHarvestTick": None if last_harvest is None else int(num(last_harvest)),
            "harvestFailCount": int(num(fails[0])),
            "recentDeaths": recent_deaths,
        }
    except sqlite3.Error:
        return None
    finally:
        _close(connection)


def load_spend_trend(
    data_root: str | os.PathLike[str],
    tenant: str,
    bucket_ticks: int = SPEND_TREND_DEFAULT_BUCKET_TICKS,
) -> list[dict[str, Any]]:
    """Core spends bucketed by ``kind x tick`` (empty when database missing)."""
    root = validate_data_root(data_root)
    path = survey_db_path(root, tenant)
    if not path.is_file():
        return []
    connection = _open_readonly(path)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT (tick / ?) * ? AS bucketStart, kind, COUNT(*) AS count,"
            " SUM(amount) AS total FROM core_spends GROUP BY bucketStart, kind"
            " ORDER BY bucketStart ASC, kind",
            (bucket_ticks, bucket_ticks),
        ).fetchall()
        return [
            {
                "bucketStart": int(num(row[0])),
                "kind": row[1],
                "count": int(num(row[2])),
                "total": row[3],
            }
            for row in rows
        ]
    except sqlite3.Error:
        return []
    finally:
        _close(connection)


def load_unit_lifecycle_db(
    data_root: str | os.PathLike[str],
    tenant: str,
    limit: int = UNIT_DETAIL_DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Unit lifecycle detail rows, newest-last-seen first (empty when missing)."""
    root = validate_data_root(data_root)
    path = survey_db_path(root, tenant)
    if not path.is_file():
        return []
    connection = _open_readonly(path)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT unit_id AS unitId, unit_type AS unitType, birth_tick AS birthTick,"
            " birth_pos AS birthPos, death_tick AS deathTick, death_pos AS deathPos,"
            " death_reason AS deathReason, last_seen_tick AS lastSeenTick,"
            " last_seen_pos AS lastSeenPos, current_state AS state FROM unit_lifecycle"
            " ORDER BY last_seen_tick DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "unitId": row[0],
                "unitType": row[1],
                "birthTick": row[2],
                "birthPos": row[3],
                "deathTick": row[4],
                "deathPos": row[5],
                "deathReason": row[6],
                "lastSeenTick": row[7],
                "lastSeenPos": row[8],
                "state": row[9],
            }
            for row in rows
        ]
    except sqlite3.Error:
        return []
    finally:
        _close(connection)


def load_tenant_survey_cached(
    data_root: str | os.PathLike[str],
    tenant: str,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Per-tenant survey cache payload (TS ``loadTenantSurveyCached``)."""
    now = now_ms if now_ms is not None else current_epoch_ms()
    at = iso_utc(now)
    return {
        "survey": load_survey_db(data_root, tenant),
        "lifecycle": load_lifecycle_db(data_root, tenant),
        "spendsTrend": load_spend_trend(data_root, tenant),
        "unitsDetail": load_unit_lifecycle_db(data_root, tenant, 500),
        "chunks": load_chunks_db(data_root, tenant),
        "cachedAt": at,
    }


def _load_survey_from_cases(
    data_root: str | os.PathLike[str], tenant: str
) -> dict[str, Any] | None:
    """Cumulative calibration scan (TS ``loadSurvey``); null when no run/cases."""
    root = validate_data_root(data_root)
    run_dir = latest_run_dir(root, tenant)
    if run_dir is None:
        return None
    case_files = list_cases(root, tenant, run_dir)
    if not case_files:
        return None
    obstacle: dict[str, dict[str, Any]] = {}
    resource: dict[str, dict[str, Any]] = {}
    cores: dict[str, dict[str, Any]] = {}
    units: dict[str, dict[str, Any]] = {}
    tick_max = 0
    case_count = 0
    base = calibration_dir(root, tenant) / run_dir / "cases"
    for case_file in case_files:
        tick = parse_tick(case_file)
        if tick > tick_max:
            tick_max = tick
        try:
            raw = json.loads((base / case_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        before = raw.get("before")
        state = before.get("state") if isinstance(before, dict) else None
        if not isinstance(state, dict):
            continue
        objects = state.get("objects")
        if not isinstance(objects, list):
            continue
        case_count += 1
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            kind = obj.get("kind")
            if kind == "OBSTACLE":
                for x, y in obj.get("positions") or ():
                    key = f"{x},{y}"
                    obstacle[key] = {"x": x, "y": y, "tick": tick}
            elif kind == "RESOURCE":
                for x, y in obj.get("positions") or ():
                    key = f"{x},{y}"
                    resource[key] = {"x": x, "y": y, "tick": tick}
            elif kind == "CORE":
                position = obj.get("position") or [0, 0]
                x, y = (
                    position
                    if isinstance(position, (list, tuple)) and len(position) >= 2
                    else [0, 0]
                )[:2]
                key = f"{x},{y}"
                current = cores.get(key)
                if current is None or tick > current["tick"]:
                    item: dict[str, Any] = {"x": x, "y": y, "tick": tick}
                    for field in ("hp", "shield", "controlled"):
                        if field in obj:
                            item[field] = obj[field]
                    if "owner_username" in obj and isinstance(obj["owner_username"], str):
                        item["owner"] = obj["owner_username"]
                    cores[key] = item
            elif kind == "UNIT":
                position = obj.get("position") or [0, 0]
                x, y = (
                    position
                    if isinstance(position, (list, tuple)) and len(position) >= 2
                    else [0, 0]
                )[:2]
                key = f"{x},{y}"
                current = units.get(key)
                if current is None or tick > current["tick"]:
                    item: dict[str, Any] = {
                        "x": x,
                        "y": y,
                        "tick": tick,
                        "unitType": obj.get("unit_type") or "WORKER",
                    }
                    if "controlled" in obj:
                        item["controlled"] = obj["controlled"]
                    if "hp" in obj:
                        item["hp"] = obj["hp"]
                    units[key] = item
    return {
        "tenant": tenant,
        "runId": run_dir,
        "caseCount": case_count,
        "tickMax": tick_max,
        "obstacleCells": list(obstacle.values()),
        "resourceCells": list(resource.values()),
        "coreCells": list(cores.values()),
        "unitCells": list(units.values()),
    }


def load_survey(
    data_root: str | os.PathLike[str],
    tenant: str = "all",
    states: list[str] | None = None,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """``/api/survey`` route payload (per-tenant survey + lifecycle + spends)."""
    now = now_ms if now_ms is not None else current_epoch_ms()
    at = iso_utc(now)
    if states is None:
        states = ["visible", "stale"]
    tenants = TENANTS if tenant == "all" else (tenant,)
    out: dict[str, Any] = {"generatedAt": at, "tenants": {}, "colors": TENANT_COLORS}
    for t in tenants:
        cached = load_tenant_survey_cached(data_root, t, now_ms=now)
        survey = cached["survey"]
        if survey is None:
            out["tenants"][t] = {"error": "survey db missing"}
            continue
        out["tenants"][t] = {
            "resources": [r for r in survey["resourceCells"] if str(r.get("state")) in states],
            "obstacles": survey["obstacleCells"],
            "coreHunts": survey["coreCells"],
            "caseCount": survey["caseCount"],
            "tickMax": survey["tickMax"],
            "lifecycle": cached["lifecycle"],
            "spendsTrend": cached["spendsTrend"],
            "unitsDetail": cached["unitsDetail"],
            "chunks": cached["chunks"],
            "cachedAt": cached["cachedAt"],
        }
    return out
