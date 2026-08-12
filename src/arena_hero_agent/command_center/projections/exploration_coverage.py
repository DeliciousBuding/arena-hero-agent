"""Alliance exploration-coverage projection (W25).

Port of the legacy TypeScript ``packages/command-center/lib/exploration-coverage.ts``:
aggregate the per-tenant survey-db ``chunks`` table (16x16 exploration
partitions, cross-run cumulative) with friendly core positions from the latest
calibration world into coverage statistics, gaps, and — the advice input —
``resurveyTargets``: explored chunks whose last observation is stale (older
than ``FRESH_WINDOW_TICKS``) and within ``RESURVEY_RADIUS_CHUNKS`` of a
friendly core, sorted stalest-first.

Registered divergence from the TS oracle: ``now_ms`` is injectable for the
``generatedAt``/``cachedAt`` wall-clock fields; the ``chunks`` table is read
directly (TS ``loadChunksDb``).
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import calibration_dir, latest_run_dir, list_cases
from ..paths import TENANTS, survey_db_path, validate_data_root
from ._common import current_epoch_ms, num

__all__ = [
    "CHUNK_SIZE",
    "RESURVEY_CAP",
    "RESURVEY_RADIUS_CHUNKS",
    "compute_exploration_stats",
    "load_alliance_exploration",
]

CHUNK_SIZE = 16
FRESH_WINDOW_TICKS = 2000
GAP_RADIUS_CHUNKS = 5
GAP_CAP = 40
RESURVEY_RADIUS_CHUNKS = 8
RESURVEY_CAP = 40

_CHUNK_KEY_RE = re.compile(r"^(-?\d+),(-?\d+)$")


def _parse_chunk_key(key: str) -> tuple[int, int] | None:
    match = _CHUNK_KEY_RE.match(key.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _js_round(value: float) -> int:
    return math.floor(value + 0.5)


def _ratio(value: float) -> int | float:
    rounded = _js_round(value)
    if rounded % 10 == 0:
        return rounded // 10
    return rounded / 10


def compute_exploration_stats(
    chunks_by_tenant: dict[str, list[dict[str, Any]]],
    cores_by_tenant: dict[str, tuple[int, int] | None],
    current_tick: int,
) -> dict[str, Any]:
    """Coverage statistics + gaps + resurvey targets (TS ``computeExplorationStats``)."""
    per_tenant: dict[str, Any] = {}
    union: dict[str, dict[str, Any]] = {}
    tenant_sets: dict[str, set[str]] = {}
    for t in TENANTS:
        chunks = chunks_by_tenant.get(t) or []
        chunk_set: set[str] = set()
        min_cx = math.inf
        max_cx = -math.inf
        min_cy = math.inf
        max_cy = -math.inf
        last_seen = 0
        recent = 0
        for chunk in chunks:
            pos = _parse_chunk_key(str(chunk.get("key") or ""))
            if pos is None:
                continue
            cx, cy = pos
            key = f"{cx},{cy}"
            chunk_set.add(key)
            min_cx = min(min_cx, cx)
            max_cx = max(max_cx, cx)
            min_cy = min(min_cy, cy)
            max_cy = max(max_cy, cy)
            lt = num(chunk.get("lastSeenTick"))
            if lt > last_seen:
                last_seen = lt
            if lt >= current_tick - FRESH_WINDOW_TICKS:
                recent += 1
            prev = union.get(key)
            if prev is None or lt > prev["lastSeenTick"]:
                union[key] = {"lastSeenTick": lt, "tenant": t}
        tenant_sets[t] = chunk_set
        per_tenant[t] = {
            "tenant": t,
            "exploredChunks": len(chunk_set),
            "recentChunks": recent,
            "lastSeenTick": last_seen if last_seen > 0 else None,
            "bbox": (
                {"minCx": min_cx, "maxCx": max_cx, "minCy": min_cy, "maxCy": max_cy}
                if chunk_set
                else None
            ),
            "exclusiveChunks": 0,
        }
    union_keys = set(union.keys())
    exclusive_by_tenant: dict[str, int] = {}
    union_recent = 0
    for value in union.values():
        if value["lastSeenTick"] >= current_tick - FRESH_WINDOW_TICKS:
            union_recent += 1
    for t in TENANTS:
        chunk_set = tenant_sets.get(t) or set()
        exclusive = 0
        for key in chunk_set:
            others = any(
                other != t and key in (tenant_sets.get(other) or set()) for other in TENANTS
            )
            if not others:
                exclusive += 1
        per_tenant[t] = {**per_tenant[t], "exclusiveChunks": exclusive}
        exclusive_by_tenant[t] = exclusive
    min_cx = math.inf
    max_cx = -math.inf
    min_cy = math.inf
    max_cy = -math.inf
    for key in union_keys:
        pos = _parse_chunk_key(key) or (0, 0)
        min_cx = min(min_cx, pos[0])
        max_cx = max(max_cx, pos[0])
        min_cy = min(min_cy, pos[1])
        max_cy = max(max_cy, pos[1])
    has_span = math.isfinite(min_cx) and min_cx <= max_cx
    span = (
        {"minCx": min_cx, "maxCx": max_cx, "minCy": min_cy, "maxCy": max_cy} if has_span else None
    )
    span_chunks = (max_cx - min_cx + 1) * (max_cy - min_cy + 1) if span else 0
    coverage_pct = _ratio(len(union_keys) / span_chunks * 1000) if span_chunks > 0 else None
    gaps: list[dict[str, Any]] = []
    if span:
        for t in TENANTS:
            pos = cores_by_tenant.get(t)
            if not pos:
                continue
            core_x, core_y = pos
            ccx = math.floor(core_x / CHUNK_SIZE)
            ccy = math.floor(core_y / CHUNK_SIZE)
            for dx in range(-GAP_RADIUS_CHUNKS, GAP_RADIUS_CHUNKS + 1):
                for dy in range(-GAP_RADIUS_CHUNKS, GAP_RADIUS_CHUNKS + 1):
                    cx = ccx + dx
                    cy = ccy + dy
                    if f"{cx},{cy}" in union_keys:
                        continue
                    gaps.append(
                        {
                            "cx": cx,
                            "cy": cy,
                            "nearCoreOf": t,
                            "distChunks": max(abs(dx), abs(dy)),
                            "corePos": [core_x, core_y],
                        }
                    )
    seen: set[str] = set()
    dedup_gaps: list[dict[str, Any]] = []
    gaps.sort(key=lambda g: (g["distChunks"], g["nearCoreOf"]))
    for g in gaps:
        key = f"{g['cx']},{g['cy']}"
        if key in seen:
            continue
        seen.add(key)
        dedup_gaps.append(g)
        if len(dedup_gaps) >= GAP_CAP:
            break
    rs_seen: set[str] = set()
    resurvey_targets: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for key, value in union.items():
        if value["lastSeenTick"] < current_tick - FRESH_WINDOW_TICKS:
            stale.append({"key": key, "lastSeenTick": value["lastSeenTick"]})
    stale.sort(key=lambda s: s["lastSeenTick"])
    for item in stale:
        if item["key"] in rs_seen:
            continue
        pos = _parse_chunk_key(item["key"])
        if pos is None:
            continue
        cx, cy = pos
        best: dict[str, Any] | None = None
        for t in TENANTS:
            core_pos = cores_by_tenant.get(t)
            if not core_pos:
                continue
            ccx = math.floor(core_pos[0] / CHUNK_SIZE)
            ccy = math.floor(core_pos[1] / CHUNK_SIZE)
            distance = max(abs(cx - ccx), abs(cy - ccy))
            if distance <= RESURVEY_RADIUS_CHUNKS and (best is None or distance < best["d"]):
                best = {"t": t, "d": distance, "pos": core_pos}
        if best is None:
            continue
        rs_seen.add(item["key"])
        resurvey_targets.append(
            {
                "key": item["key"],
                "cx": cx,
                "cy": cy,
                "lastSeenTick": item["lastSeenTick"],
                "stalenessTicks": current_tick - item["lastSeenTick"],
                "nearCoreOf": best["t"],
                "distChunks": best["d"],
                "corePos": best["pos"],
            }
        )
        if len(resurvey_targets) >= RESURVEY_CAP:
            break
    return {
        "world": {
            "chunkSize": CHUNK_SIZE,
            "observedSpan": span,
            "spanChunks": span_chunks,
            "exploredChunks": len(union_keys),
            "coveragePct": coverage_pct,
        },
        "perTenant": per_tenant,
        "alliance": {
            "unionChunks": len(union_keys),
            "unionRecent": union_recent,
            "coveragePct": coverage_pct,
            "exclusiveByTenant": exclusive_by_tenant,
        },
        "gaps": dedup_gaps,
        "resurveyTargets": resurvey_targets,
    }


def _chunks_from_db(path: Path) -> list[dict[str, Any]]:
    """Survey-db chunks (TS ``loadChunksDb``)."""
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(
            "SELECT chunk_key AS key, last_seen_tick AS lastSeenTick FROM chunks"
            " ORDER BY last_seen_tick DESC"
        ).fetchall()
        return [{"key": str(row[0]), "lastSeenTick": row[1]} for row in rows]
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _world_tick_and_core(root: Path, tenant: str) -> tuple[int, tuple[int, int] | None]:
    """Latest calibration world tick + friendly core position (TS ``loadWorld``)."""
    run_dir = latest_run_dir(root, tenant)
    if run_dir is None:
        return 0, None
    files = list_cases(root, tenant, run_dir)
    if not files:
        return 0, None
    path = calibration_dir(root, tenant) / run_dir / "cases" / files[-1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, None
    if not isinstance(raw, dict):
        return 0, None
    after = raw.get("after") if isinstance(raw.get("after"), dict) else {}
    before = raw.get("before") if isinstance(raw.get("before"), dict) else {}
    tick = after.get("tick") if after.get("tick") is not None else before.get("tick")
    state = after.get("state") if isinstance(after.get("state"), dict) else None
    if state is None and isinstance(before.get("state"), dict):
        state = before.get("state")
    core_position: tuple[int, int] | None = None
    objects: list[Any] = []
    if state:
        objects = state.get("objects") or []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if obj.get("kind") == "CORE" and obj.get("controlled") is True:
            position = obj.get("position")
            if isinstance(position, list) and len(position) >= 2:
                core_position = (int(num(position[0])), int(num(position[1])))
            break
    return int(num(tick)), core_position


def load_alliance_exploration(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load the alliance exploration payload (``/api/alliance/exploration``)."""
    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    chunks_by_tenant: dict[str, list[dict[str, Any]]] = {}
    cores_by_tenant: dict[str, tuple[int, int] | None] = {}
    current_tick = 0
    for t in TENANTS:
        chunks_by_tenant[t] = _chunks_from_db(survey_db_path(root, t))
        world_tick, core_pos = _world_tick_and_core(root, t)
        if world_tick > current_tick:
            current_tick = world_tick
        cores_by_tenant[t] = core_pos
    stats = compute_exploration_stats(chunks_by_tenant, cores_by_tenant, current_tick)
    return {"generatedAt": iso_utc(now), **stats, "cachedAt": iso_utc(now)}
