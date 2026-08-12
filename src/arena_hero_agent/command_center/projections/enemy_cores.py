"""Enemy-core state view (port of legacy ``enemy-core-state.ts`` + ``server.ts``).

Ports the ``/api/survey/enemy-cores`` route: aggregate the shared-survey
``core_hunts`` ledger (across all tenants, cross-run enemy-core memory) into
per-owner lifecycle states — ACTIVE (last seen within the active window),
RELOCATED (same owner at multiple locations, still active), STALE (older than
the stale window) — plus a threat level from Chebyshev distance to the
nearest friendly core (high <= 60, medium <= 200, else low; STALE never high).
``currentTick`` is the maximum ``last_seen_tick`` across hunts. Pure read.
``/api/survey/enemy-cores``.

Registered difference from the TS oracle: ``generatedAt`` is injectable via
``now_ms``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from ..goal_store import iso_utc
from ..paths import TENANTS, survey_db_path, validate_data_root
from ._common import current_epoch_ms, num
from .alliance_snapshot import load_alliance_snapshot

__all__ = [
    "DEFAULT_ENEMY_CORE_OPTS",
    "build_enemy_core_states",
    "load_enemy_cores",
]

DEFAULT_ENEMY_CORE_OPTS: dict[str, int] = {
    "activeWindow": 1000,
    "staleWindow": 5000,
    "highThreatRadius": 60,
    "mediumThreatRadius": 200,
}

_THREAT_ORDER = {"high": 0, "medium": 1, "low": 2}


def _chebyshev(
    a: tuple[int | float, int | float], b: tuple[int | float, int | float]
) -> int | float:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def build_enemy_core_states(
    hunts: list[dict[str, Any]],
    current_tick: int,
    friendly_cores: list[tuple[int | float, int | float]] | None = None,
    opts: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Enemy-core state aggregation (pure; TS ``buildEnemyCoreStates``)."""
    options = {**DEFAULT_ENEMY_CORE_OPTS, **(opts or {})}
    stale_window = options["staleWindow"]
    high_radius = options["highThreatRadius"]
    medium_radius = options["mediumThreatRadius"]
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for hunt in hunts or ():
        owner = str(hunt.get("owner") or "")
        if not owner:
            continue
        by_owner.setdefault(owner, []).append(hunt)
    friendly_cores = friendly_cores or []
    out: list[dict[str, Any]] = []
    for owner, rows in by_owner.items():
        latest = max(rows, key=lambda item: num(item.get("lastSeenTick")))
        last_seen = int(num(latest.get("lastSeenTick")))
        first_seen = int(num(latest.get("firstSeenTick")))
        x = int(num(latest.get("x")))
        y = int(num(latest.get("y")))
        age = max(0, current_tick - last_seen) if current_tick > 0 else 0
        location_count = len({f"{int(num(r.get('x')))},{int(num(r.get('y')))}" for r in rows})
        if age > stale_window:
            status = "STALE"
        elif location_count > 1:
            status = "RELOCATED"
        else:
            status = "ACTIVE"
        dist: int | float | None = None
        for core in friendly_cores:
            if len(core) < 2:
                continue
            d = _chebyshev((x, y), (core[0], core[1]))
            if dist is None or d < dist:
                dist = d
        threat = "low"
        if status != "STALE" and dist is not None:
            if dist <= high_radius:
                threat = "high"
            elif dist <= medium_radius:
                threat = "medium"
        out.append(
            {
                "owner": owner,
                "status": status,
                "x": x,
                "y": y,
                "firstSeenTick": first_seen,
                "lastSeenTick": last_seen,
                "locationCount": location_count,
                "distToFriendly": dist,
                "threat": threat,
            }
        )
    out.sort(
        key=lambda item: (
            _THREAT_ORDER[item["threat"]],
            1 if item["status"] == "STALE" else 0,
            item["owner"],
        )
    )
    return out


def _read_hunts(path: Path) -> list[dict[str, Any]]:
    """Survey-db core_hunts rows for one tenant (missing table -> empty)."""
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(
            "SELECT owner, x, y, first_seen_tick, last_seen_tick, source FROM core_hunts"
            " WHERE owner IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    return [
        {
            "owner": str(row[0]),
            "x": int(num(row[1])),
            "y": int(num(row[2])),
            "firstSeenTick": int(num(row[3])),
            "lastSeenTick": int(num(row[4])),
            "source": "WORKER_INFER" if str(row[5]) == "WORKER_INFER" else "CORE",
        }
        for row in rows
    ]


def load_enemy_cores(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load the enemy-core state view (``/api/survey/enemy-cores``)."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    root = validate_data_root(data_root)
    hunts: list[dict[str, Any]] = []
    max_tick = 0
    for tenant in TENANTS:
        for hunt in _read_hunts(survey_db_path(root, tenant)):
            last_seen = int(num(hunt.get("lastSeenTick")))
            if last_seen > max_tick:
                max_tick = last_seen
            hunts.append(hunt)
    snapshot = load_alliance_snapshot(root, now_ms=now_ms)
    friendly_cores: list[tuple[int | float, int | float]] = []
    for member in (snapshot.get("members") or {}).values():
        position = (member.get("core") or {}).get("position")
        if isinstance(position, (list, tuple)) and len(position) >= 2:
            friendly_cores.append((num(position[0]), num(position[1])))
    return {
        "generatedAt": at,
        "currentTick": max_tick,
        "cores": build_enemy_core_states(hunts, max_tick, friendly_cores),
    }
