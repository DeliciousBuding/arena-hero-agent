"""Map LOD (level-of-detail) aggregation projection (port of legacy ``map-lod.ts``).

Global zoom needs no per-cell map detail (~642 KB full map): this projection
buckets each tenant's survey resources/obstacles/enemy cores into 16x16
chunks with per-chunk counts and the latest observed tick, so the frontend
can draw a lightweight global layer (~12 KB). ``/api/map/lod?tenant=all|tN``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from ..goal_store import iso_utc
from ..paths import TENANTS, survey_db_path, validate_data_root, validate_survey_tenant
from ._common import current_epoch_ms, num

MAP_LOD_CHUNK = 16

__all__ = ["MAP_LOD_CHUNK", "aggregate_map_lod", "load_map_lod"]


def aggregate_map_lod(
    tenant: str,
    resources: list[dict[str, Any]],
    obstacles: list[dict[str, Any]],
    cores: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bucket survey cells into 16x16 chunks (TS parity)."""
    by_key: dict[str, dict[str, Any]] = {}

    def put(x: object, y: object, tick: object, kind: str) -> None:
        cx = int(num(x) // MAP_LOD_CHUNK)
        cy = int(num(y) // MAP_LOD_CHUNK)
        key = f"{cx},{cy}"
        chunk = by_key.get(key)
        if chunk is None:
            fresh: dict[str, Any] = {
                "cx": cx,
                "cy": cy,
                "tenant": tenant,
                "resourceCount": 0,
                "obstacleCount": 0,
                "coreCount": 0,
                "lastTick": 0,
            }
            chunk = fresh
            by_key[key] = chunk
        chunk[kind] = int(chunk[kind]) + 1
        t = num(tick)
        if t > int(chunk["lastTick"]):
            chunk["lastTick"] = t

    for r in resources or []:
        put(r.get("x"), r.get("y"), r.get("tick"), "resourceCount")
    for o in obstacles or []:
        put(o.get("x"), o.get("y"), o.get("tick"), "obstacleCount")
    for c in cores or []:
        put(c.get("x"), c.get("y"), c.get("tick"), "coreCount")
    chunks = list(by_key.values())
    chunks.sort(key=lambda item: (-item["lastTick"], item["cx"], item["cy"]))
    return chunks


def _survey_rows(path: Path, table: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(f"SELECT x, y, last_seen_tick AS tick FROM {table}").fetchall()
        return [{"x": row[0], "y": row[1], "tick": row[2]} for row in rows]
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def load_map_lod(
    data_root: str | os.PathLike[str],
    tenant: str = "all",
) -> dict[str, Any]:
    """Read per-tenant survey tables and aggregate chunks (``/api/map/lod``)."""
    root = validate_data_root(data_root)
    tenants = list(TENANTS) if tenant == "all" else [validate_survey_tenant(tenant)]
    chunks: list[dict[str, Any]] = []
    for t in tenants:
        path = survey_db_path(root, t)
        chunks.extend(
            aggregate_map_lod(
                t,
                _survey_rows(path, "resources"),
                _survey_rows(path, "obstacles"),
                _survey_rows(path, "core_hunts"),
            )
        )
    chunks.sort(key=lambda item: (-item["lastTick"], item["tenant"]))
    at = iso_utc(current_epoch_ms())
    return {
        "generatedAt": at,
        "tenant": tenant,
        "chunkSize": MAP_LOD_CHUNK,
        "chunks": chunks,
        "cachedAt": at,
    }
