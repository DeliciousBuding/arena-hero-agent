"""Alliance shared-survey aggregation projection (port of legacy ``alliance-survey.ts``).

Aggregates the four tenants' survey databases (enemy cores / resources /
obstacles / exploration chunks + lifecycle summary) into the "alliance" map
layer with per-tenant colors and evidence provenance, then derives the
cross-tenant consensus views:

- consensus resources: one entry per same-cell mine (winner by last-seen
  newest, ties by tenant order, or human arbitration), with observers;
- consensus cores: one entry per owner (latest position + observers);
- consensus chunks: exploration-coverage union (latest tick + observers);
- conflicts: same-cell resource overlaps and resource/obstacle contradictions.

``/api/alliance/survey``.

Registered differences from the TS oracle:

- The pure core takes per-tenant survey snapshots (``resourceCells`` /
  ``obstacleCells`` / ``coreCells`` / ``chunks`` / ``caseCount`` / ``tickMax`` /
  ``lifecycle``) as parsed inputs; the loader builds them from the P5-3 survey
  schema (``resources`` / ``obstacles`` / ``core_hunts``) plus
  ``MAX(agents.tick)`` as the tick watermark because the TS ``sync_meta`` /
  ``chunks`` tables are not part of the P5-3 schema. Missing tables degrade to
  empty lists, never to a raised error.
- ``generatedAt``/``cachedAt`` are injectable via ``now_ms``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import load_jsonl_rows
from ..paths import TENANTS, survey_db_path, validate_data_root
from ._common import current_epoch_ms, num
from .arbitrations import load_arbitrations

TENANT_COLORS: dict[str, str] = {
    "t1": "#69b3d8",
    "t2": "#57bd84",
    "t3": "#a892d6",
    "t4": "#dd626d",
}

__all__ = ["TENANT_COLORS", "aggregate_alliance_survey", "load_alliance_survey"]


def _pick_resource_winner(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(
        rows,
        key=lambda row: (-num(row.get("tick")), str(row.get("tenant"))),
    )[0]


def _arbitrated_winner(
    rows: list[dict[str, Any]],
    arbitration: dict[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    auto = _pick_resource_winner(rows)
    winner_tenant = arbitration.get("winnerTenant") if arbitration else None
    if winner_tenant is not None:
        override = next(
            (row for row in rows if str(row.get("tenant")) == str(winner_tenant)),
            None,
        )
        return (override if override is not None else auto), True
    return auto, False


def aggregate_alliance_survey(
    tenant_surveys: dict[str, dict[str, Any]],
    arbitrations: dict[str, dict[str, Any]],
    *,
    colors: dict[str, str] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Aggregate per-tenant survey snapshots into the alliance survey (TS parity)."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    tenant_colors = dict(colors if colors is not None else TENANT_COLORS)
    tenant_summaries: dict[str, dict[str, Any]] = {}
    resources: list[dict[str, Any]] = []
    obstacles: list[dict[str, Any]] = []
    enemy_cores: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    lifecycle: dict[str, dict[str, Any] | None] = {}
    cached_at = ""
    for t in TENANTS:
        survey = tenant_surveys.get(t)
        if survey is None:
            survey = {}
        cached_at = str(survey.get("cachedAt") or cached_at)
        tenant_summaries[t] = {
            "caseCount": num(survey.get("caseCount")),
            "tickMax": num(survey.get("tickMax")),
            "resources": len(survey.get("resourceCells") or []),
            "obstacles": len(survey.get("obstacleCells") or []),
            "cores": len(survey.get("coreCells") or []),
            "chunks": len(survey.get("chunks") or []),
        }
        for row in survey.get("resourceCells") or []:
            resources.append({"tenant": t, **row})
        for row in survey.get("obstacleCells") or []:
            obstacles.append({"tenant": t, **row})
        for row in survey.get("coreCells") or []:
            enemy_cores.append({"tenant": t, **row})
        for row in survey.get("chunks") or []:
            chunks.append({"tenant": t, **row})
        lifecycle[t] = (
            survey.get("lifecycle") if isinstance(survey.get("lifecycle"), dict) else None
        )

    res_by_cell: dict[str, list[dict[str, Any]]] = {}
    for row in resources:
        key = f"{row.get('x')},{row.get('y')}"
        res_by_cell.setdefault(key, []).append(row)

    consensus_resources: list[dict[str, Any]] = []
    for rows in res_by_cell.values():
        if len(rows) == 1:
            single = rows[0]
            consensus_resources.append(
                {**single, "observers": [single.get("tenant")], "consensus": 1}
            )
            continue
        winner, arbitrated = _arbitrated_winner(rows, arbitrations.get(_cell_of(rows[0])))
        consensus_resources.append(
            {
                **winner,
                "observers": [row.get("tenant") for row in rows],
                "consensus": len(rows),
                "arbitrated": arbitrated,
            }
        )
    consensus_resources.sort(key=lambda row: (num(row.get("x")), num(row.get("y"))))

    core_by_owner: dict[str, dict[str, Any]] = {}
    for row in enemy_cores:
        owner = str(row.get("owner") or "")
        if not owner:
            continue
        current = core_by_owner.get(owner)
        if current is None:
            core_by_owner[owner] = {**row, "observers": [row.get("tenant")]}
        else:
            observers = list(current.get("observers") or [])
            if row.get("tenant") not in observers:
                observers.append(row.get("tenant"))
            if num(row.get("tick")) > num(current.get("tick")):
                core_by_owner[owner] = {**row, "observers": observers}
            else:
                current["observers"] = observers
    consensus_cores = list(core_by_owner.values())

    chunk_by_key: dict[str, dict[str, Any]] = {}
    for row in chunks:
        key = str(row.get("key") or f"{row.get('cx')},{row.get('cy')}")
        current = chunk_by_key.get(key)
        if current is None:
            chunk_by_key[key] = {**row, "observers": [row.get("tenant")]}
        else:
            observers = list(current.get("observers") or [])
            if row.get("tenant") not in observers:
                observers.append(row.get("tenant"))
            if num(row.get("lastSeenTick")) > num(current.get("lastSeenTick")):
                chunk_by_key[key] = {**row, "observers": observers}
            else:
                current["observers"] = observers
    consensus_chunks = list(chunk_by_key.values())

    resource_overlaps: list[dict[str, Any]] = []
    for cell, rows in res_by_cell.items():
        if len(rows) <= 1:
            continue
        arbitration = arbitrations.get(cell)
        winner_tenant = arbitration.get("winnerTenant") if arbitration else None
        auto = _pick_resource_winner(rows)
        if winner_tenant is not None:
            winner = next(
                (row for row in rows if str(row.get("tenant")) == str(winner_tenant)),
                auto,
            )
        else:
            winner = auto
        losers = [row.get("tenant") for row in rows if row is not winner]
        tie_broken = all(num(row.get("tick")) == num(winner.get("tick")) for row in rows)
        arbitrated = winner_tenant is not None
        if arbitrated:
            note = ""
            if arbitration is not None and isinstance(arbitration.get("note"), str):
                note = arbitration["note"]
            reason = f"人工仲裁：{winner_tenant} 占矿{'（' + note + '）' if note else ''}"
        elif tie_broken:
            reason = f"同 tick 平局，租户序 {winner.get('tenant')} 胜"
        else:
            reason = f"lastSeen {num(winner.get('tick'))} 最新，{winner.get('tenant')} 占矿"
        resource_overlaps.append(
            {
                "cell": cell,
                "tenants": [row.get("tenant") for row in rows],
                "states": [row.get("state") for row in rows],
                "lastSeenTicks": [row.get("tick") for row in rows],
                "arbitration": {
                    "winner": str(winner.get("tenant")),
                    "losers": losers,
                    "tieBroken": tie_broken,
                    "arbitrated": arbitrated,
                    "reason": reason,
                },
            }
        )
    resource_overlaps.sort(key=lambda item: str(item["cell"]))

    obstacle_cells: dict[str, list[str]] = {}
    for row in obstacles:
        key = f"{row.get('x')},{row.get('y')}"
        tenants = obstacle_cells.setdefault(key, [])
        tenant_value = str(row.get("tenant"))
        if tenant_value not in tenants:
            tenants.append(tenant_value)

    obstacle_resource_conflicts: list[dict[str, Any]] = []
    for cell, rows in res_by_cell.items():
        obstacle_tenants = obstacle_cells.get(cell)
        if obstacle_tenants:
            obstacle_resource_conflicts.append(
                {
                    "cell": cell,
                    "resourceTenants": [row.get("tenant") for row in rows],
                    "obstacleTenants": obstacle_tenants,
                }
            )
    obstacle_resource_conflicts.sort(key=lambda item: str(item["cell"]))

    return {
        "generatedAt": at,
        "colors": tenant_colors,
        "tenantSummaries": tenant_summaries,
        "enemyCores": enemy_cores,
        "resources": resources,
        "obstacles": obstacles,
        "chunks": chunks,
        "lifecycle": lifecycle,
        "conflicts": {
            "resourceOverlaps": resource_overlaps,
            "obstacleResourceConflicts": obstacle_resource_conflicts,
        },
        "consensusResources": consensus_resources,
        "consensusCores": consensus_cores,
        "consensusChunks": consensus_chunks,
        "cachedAt": at,
    }


def _cell_of(row: dict[str, Any]) -> str:
    return f"{row.get('x')},{row.get('y')}"


def _survey_from_db(path: Path) -> dict[str, Any]:
    missing = {
        "resourceCells": [],
        "obstacleCells": [],
        "coreCells": [],
        "chunks": [],
        "caseCount": 0,
        "tickMax": 0,
    }
    if not path.exists():
        return dict(missing)
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return dict(missing)
    try:
        tick_row = connection.execute("SELECT MAX(tick) FROM agents").fetchone()
        tick_max = tick_row[0] if tick_row and isinstance(tick_row[0], int) else 0
        resource_rows = connection.execute(
            "SELECT x, y, first_seen_tick, last_seen_tick, state, seen_count FROM resources"
        ).fetchall()
        resource_cells = [
            {
                "x": row[0],
                "y": row[1],
                "firstSeenTick": row[2],
                "tick": row[3],
                "state": row[4],
                "seenCount": row[5],
            }
            for row in resource_rows
        ]
        obstacle_rows = connection.execute("SELECT x, y, last_seen_tick FROM obstacles").fetchall()
        obstacle_cells = [{"x": row[0], "y": row[1], "tick": row[2]} for row in obstacle_rows]
        core_rows = connection.execute(
            "SELECT x, y, last_seen_tick, owner, source FROM core_hunts "
            "ORDER BY last_seen_tick DESC"
        ).fetchall()
        seen_owners: set[str] = set()
        core_cells: list[dict[str, Any]] = []
        for row in core_rows:
            owner = row[3] if isinstance(row[3], str) and row[3] else None
            if owner is not None:
                if owner in seen_owners:
                    continue
                seen_owners.add(owner)
            core_cells.append(
                {"x": row[0], "y": row[1], "tick": row[2], "owner": owner, "source": row[4]}
            )
        return {
            "resourceCells": resource_cells,
            "obstacleCells": obstacle_cells,
            "coreCells": core_cells,
            "chunks": [],
            "caseCount": 0,
            "tickMax": tick_max,
        }
    except sqlite3.Error:
        return dict(missing)
    finally:
        connection.close()


def load_alliance_survey(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Read per-tenant survey tables and aggregate (``/api/alliance/survey``)."""
    root = validate_data_root(data_root)
    tenant_surveys: dict[str, dict[str, Any]] = {}
    for t in TENANTS:
        tenant_surveys[t] = _survey_from_db(survey_db_path(root, t))
    arbitrations = load_arbitrations(
        load_jsonl_rows(root / "runtime" / "survey" / "arbitration.jsonl")
    )
    return aggregate_alliance_survey(tenant_surveys, arbitrations, now_ms=now_ms)
