"""Merged map read model and weak-ETag signature (P5-5).

``/api/map`` returns the merged alliance map (terrain + dynamic units/cores +
exploration chunks + beacons + enemy core trails) with a weak HTTP ETag
(``W/<map-sig>``) and ``cache-control: public, max-age=2``; when
``If-None-Match`` equals the current tag the server replies 304 with zero
payload. The signature is the same artifact signature the legacy TypeScript
oracle computes (per-tenant latest run + case file names + survey-db
mtime/size), so the tag is stable within a tick and changes when data moves.

The loader reuses the P5-3 data base (``latest_run_dir`` / ``list_cases`` /
``parse_tick`` / survey db paths) and the P5-4 ``map_lod`` chunk aggregation.
Registered ALLOWED gaps (not ported yet; fail-closed, never guessed):

- beacon trail history (TS ``loadBeaconTrail``) is not ported: the beacon layer
  carries the current position with ``trail: []``;
- core trails use the survey-db ``core_hunts`` ledger only (TS
  ``loadCoreTrailsFromSurveyDb`` semantics); the recent-case scan supplement is
  not ported.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import latest_run_dir, list_cases, parse_tick
from ..paths import TENANTS, survey_db_path, validate_data_root
from ..projections._common import current_epoch_ms, num
from ..projections.map_lod import aggregate_map_lod

CORE_TRAIL_MAX_POINTS = 48
CORE_TRAIL_MIN_POINTS = 2

__all__ = [
    "CORE_TRAIL_MAX_POINTS",
    "CORE_TRAIL_MIN_POINTS",
    "load_core_trails_from_survey_db",
    "load_merged_map",
    "map_signature",
]


def _survey_rows(path: Path, table: str) -> list[dict[str, Any]]:
    """Read a survey table into dicts; missing/corrupt db degrades to empty."""
    if not path.exists():
        return []
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        cursor = connection.execute(f"SELECT * FROM {table}")
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _survey_terrain(path: Path) -> dict[str, dict[str, Any]]:
    """Terrain cells from the survey db: obstacles permanent, resources non-empty.

    One kind per cell (obstacle wins), matching the TS merged-map priority.
    """
    terrain: dict[str, dict[str, Any]] = {}
    for row in _survey_rows(path, "obstacles"):
        x, y = int(num(row.get("x"))), int(num(row.get("y")))
        terrain[f"{x},{y}"] = {
            "x": x,
            "y": y,
            "type": "obstacle",
            "tick": int(num(row.get("last_seen_tick"))),
        }
    for row in _survey_rows(path, "resources"):
        if str(row.get("state") or "") == "empty":
            continue
        x, y = int(num(row.get("x"))), int(num(row.get("y")))
        entry: dict[str, Any] = {
            "x": x,
            "y": y,
            "type": "resource",
            "tick": int(num(row.get("last_seen_tick"))),
        }
        for key, field in (
            ("state", "state"),
            ("seenCount", "seen_count"),
            ("harvestCount", "harvest_count"),
            ("ageTicks", "age_ticks"),
        ):
            value = row.get(field)
            if value is not None:
                entry[key] = value
        terrain[f"{x},{y}"] = entry
    return terrain


def _dynamic_entries(
    state: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Units and cores from a calibration case state (latest tick per object id)."""
    units: dict[str, dict[str, Any]] = {}
    cores: dict[str, dict[str, Any]] = {}
    objects = state.get("objects")
    if not isinstance(objects, list):
        return units, cores
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        position = obj.get("position")
        if not isinstance(position, list) or len(position) != 2:
            continue
        tick = int(num(obj.get("tick")))
        obj_id = obj.get("id")
        kind = obj.get("kind")
        if kind == "UNIT" and obj_id is not None:
            previous = units.get(str(obj_id))
            if previous is None or tick >= int(previous["tick"]):
                units[str(obj_id)] = {
                    "x": int(num(position[0])),
                    "y": int(num(position[1])),
                    "type": "unit",
                    "tick": tick,
                    "hp": num(obj.get("hp")),
                    "unitType": str(obj.get("unit_type") or "WORKER"),
                    "cargo": num(obj.get("cargo")),
                    "controlled": bool(obj.get("controlled")),
                    "id": obj_id,
                }
        elif kind == "CORE" and obj_id is not None:
            previous = cores.get(str(obj_id))
            if previous is None or tick >= int(previous["tick"]):
                cores[str(obj_id)] = {
                    "x": int(num(position[0])),
                    "y": int(num(position[1])),
                    "type": "core",
                    "tick": tick,
                    "hp": num(obj.get("hp")),
                    "shield": num(obj.get("shield")),
                    "controlled": bool(obj.get("controlled")),
                    "owner": obj.get("owner"),
                    "id": obj_id,
                }
    return units, cores


def _read_case(root: Path, tenant: str, run_dir: str, file_name: str) -> dict[str, Any]:
    path = root / "runtime" / tenant / "calibration" / run_dir / "cases" / file_name
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_core_trails_from_survey_db(path: Path) -> list[dict[str, Any]]:
    """Enemy core trails from the survey-db ``core_hunts`` ledger (TS port).

    Rows are grouped by owner ordered by ``last_seen_tick``; consecutive
    same-cell points are deduped; only owners with at least
    ``CORE_TRAIL_MIN_POINTS`` points are returned, capped at
    ``CORE_TRAIL_MAX_POINTS``, sorted by trail length descending.
    """
    if not path.exists():
        return []
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(
            "SELECT owner, x, y, last_seen_tick FROM core_hunts "
            "WHERE owner IS NOT NULL AND owner != '' ORDER BY last_seen_tick ASC"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    by_user: dict[str, list[dict[str, Any]]] = {}
    for owner, x, y, tick in rows:
        points = by_user.setdefault(str(owner), [])
        if points and points[-1]["x"] == x and points[-1]["y"] == y:
            continue
        points.append({"x": x, "y": y, "tick": tick})
    out: list[dict[str, Any]] = []
    for username, points in by_user.items():
        if len(points) < CORE_TRAIL_MIN_POINTS:
            continue
        trail = points[-CORE_TRAIL_MAX_POINTS:]
        out.append({"username": username, "trail": trail})
    out.sort(key=lambda item: -len(item["trail"]))
    return out


def map_signature(data_root: str | os.PathLike[str]) -> str:
    """Deterministic artifact signature backing the weak ETag (TS port).

    Per-tenant latest run + case file names, then the survey-db mtime/size of
    every runtime tenant. Stable within a tick; changes when data moves.
    """
    root = validate_data_root(data_root)
    parts: list[str] = []
    for tenant in TENANTS:
        run_dir = latest_run_dir(root, tenant)
        if run_dir is None:
            parts.append(f"{tenant}:none")
            continue
        files = list_cases(root, tenant, run_dir)
        parts.append(f"{tenant}:{run_dir}:{len(files)}:{files[-1] if files else ''}")
    db_parts: list[str] = []
    for tenant in TENANTS:
        path = survey_db_path(root, tenant)
        if path.is_file():
            stat = path.stat()
            db_parts.append(f"{tenant}:{stat.st_mtime_ns // 1_000_000}:{stat.st_size}")
        else:
            db_parts.append(f"{tenant}:0:0")
    return f"{'|'.join(parts)}#{'|'.join(db_parts)}"


def load_merged_map(
    data_root: str | os.PathLike[str],
) -> tuple[dict[str, Any], str]:
    """Build the merged map payload and its weak-ETag signature.

    Returns ``(payload, signature)`` so the API layer can serve 200 with the
    ETag or 304 when ``If-None-Match`` matches.
    """
    root = validate_data_root(data_root)
    signature = map_signature(root)
    cells: dict[str, dict[str, Any]] = {}
    chunk_by_key: dict[str, dict[str, Any]] = {}
    per_tenant: list[dict[str, Any]] = []
    for tenant in TENANTS:
        run_dir = latest_run_dir(root, tenant)
        if run_dir is None:
            per_tenant.append(
                {
                    "tenant": tenant,
                    "runId": None,
                    "caseCount": 0,
                    "latestTick": None,
                    "beacon": None,
                }
            )
            continue
        files = list_cases(root, tenant, run_dir)
        latest_tick = 0
        beacon: dict[str, Any] | None = None
        if files:
            last_file = files[-1]
            latest_tick = max(latest_tick, parse_tick(last_file) or 0)
            raw = _read_case(root, tenant, run_dir, last_file)
            state = raw.get("after") or {}
            state = state.get("state") if isinstance(state, dict) else None
            if not isinstance(state, dict):
                state = raw.get("before") or {}
                state = state.get("state") if isinstance(state, dict) else None
            if isinstance(state, dict):
                units, cores = _dynamic_entries(state)
                for entry in units.values():
                    cells[f"{tenant}:unit:{entry['id']}"] = {
                        **entry,
                        "tenant": tenant,
                        "fresh": int(entry["tick"]) == latest_tick,
                    }
                for entry in cores.values():
                    cells[f"{tenant}:core:{entry['id']}"] = {
                        **entry,
                        "tenant": tenant,
                        "fresh": int(entry["tick"]) == latest_tick,
                    }
                champion = state.get("champion_beacon")
                if isinstance(champion, dict) and isinstance(champion.get("position"), list):
                    position = champion["position"]
                    if len(position) == 2:
                        beacon = {
                            "x": num(position[0]),
                            "y": num(position[1]),
                            "status": champion.get("status"),
                            "carrier_id": champion.get("carrier_id"),
                            "trail": [],
                        }
        path = survey_db_path(root, tenant)
        for entry in _survey_terrain(path).values():
            key = f"{entry['type']}:{entry['x']},{entry['y']}"
            current = cells.get(key)
            if current is None or int(entry["tick"]) >= int(current["tick"]):
                cells[key] = {**entry, "tenant": tenant, "fresh": int(entry["tick"]) == latest_tick}
        for chunk in aggregate_map_lod(
            tenant,
            _survey_rows(path, "resources"),
            _survey_rows(path, "obstacles"),
            _survey_rows(path, "core_hunts"),
        ):
            key = f"{chunk['cx']},{chunk['cy']}"
            current = chunk_by_key.get(key)
            if current is None or int(chunk["lastTick"]) >= int(current["lastTick"]):
                chunk_by_key[key] = chunk
        per_tenant.append(
            {
                "tenant": tenant,
                "runId": run_dir,
                "caseCount": len(files),
                "latestTick": latest_tick or None,
                "beacon": beacon,
            }
        )
    cell_list = list(cells.values())
    xs = [int(entry["x"]) for entry in cell_list]
    ys = [int(entry["y"]) for entry in cell_list]
    bounds: dict[str, int] = (
        {"minX": min(xs), "maxX": max(xs), "minY": min(ys), "maxY": max(ys)}
        if cell_list
        else {"minX": 0, "maxX": 0, "minY": 0, "maxY": 0}
    )
    chunks = sorted(chunk_by_key.values(), key=lambda c: (-int(c["lastTick"]), c["cx"], c["cy"]))
    beacons = [
        {"tenant": tenant["tenant"], **tenant["beacon"]}
        for tenant in per_tenant
        if tenant["beacon"] is not None
    ]
    core_trail_by_user: dict[str, dict[str, Any]] = {}
    for tenant in TENANTS:
        for trail in load_core_trails_from_survey_db(survey_db_path(root, tenant)):
            current = core_trail_by_user.get(str(trail["username"]))
            if current is None or len(trail["trail"]) > len(current["trail"]):
                core_trail_by_user[str(trail["username"])] = {
                    **trail,
                    "tenant": tenant,
                }
    payload: dict[str, Any] = {
        "generatedAt": iso_utc(current_epoch_ms()),
        "tenants": per_tenant,
        "bounds": bounds,
        "cellCount": len(cell_list),
        "cells": cell_list,
        "chunks": chunks,
        "beacons": beacons,
        "coreTrails": list(core_trail_by_user.values()),
    }
    return payload, signature
