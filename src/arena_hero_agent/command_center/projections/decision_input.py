"""Decision-input pipeline (port of legacy ``decision-input.ts``).

Ports ``buildDecisionInput`` / ``loadDecisionInput``: compose mine-patterns
refill predictions, survey-db chunk coverage, alliance-exploration resurvey
targets, enemy-core threats (from survey-db core trails + friendly core), and
mine-utilization mining candidates into the mission-layer Phase 2 shape —
with consensus-mining per-cell / per-chunk threat enrichment.
``/api/survey/decision-input?tenant=tN``.

Registered differences from the TS oracle:

- The P5-3 Python survey schema lacks the TS ``chunks`` table; a missing
  table degrades to empty ``chunkCoverage`` (TS ``loadTenantSurveyCached``
  would return an empty array for a missing table as well).
- ``generatedAt``/``cachedAt`` are injectable via ``now_ms``.
- Each optional input (threats / resurvey / core-trails / mine-utilization)
  is wrapped fail-open: unavailable data degrades to empty, never blocks the
  refill/chunk outputs (TS ``try { } catch { }`` parity).
"""

from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path
from typing import Any

from arena_hero_agent.alliance.advice import collect_core_threats

from ..goal_store import iso_utc
from ..paths import survey_db_path, validate_data_root
from ._common import current_epoch_ms, num
from .alliance_snapshot import load_alliance_snapshot
from .consensus_mining import load_consensus_mining
from .core_trails import load_core_trails_from_survey_db
from .exploration_coverage import CHUNK_SIZE, load_alliance_exploration
from .mine_patterns import load_mine_patterns
from .mines import load_mine_utilization

__all__ = ["build_decision_input", "load_decision_input"]

MINING_CANDIDATES_CAP = 40
CORE_TRAIL_MAX_POINTS = 48
CORE_TRAIL_MIN_POINTS = 1


def _chunks_from_db(path: Path) -> list[dict[str, Any]]:
    """Survey-db chunks with cx/cy derived from the chunk key (TS ``loadChunksDb``)."""
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
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = str(row[0])
        parts = key.split(",")
        cx = int(num(parts[0])) if len(parts) >= 1 else 0
        cy = int(num(parts[1])) if len(parts) >= 2 else 0
        out.append({"key": key, "cx": cx, "cy": cy, "lastSeenTick": row[1]})
    return out


def _survey_tick_max(path: Path) -> int | None:
    """Survey watermark (``sync_meta`` then ``agents``); None when no data."""
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        try:
            row = connection.execute("SELECT MAX(last_tick) AS m FROM sync_meta").fetchone()
            if row is not None and row[0] is not None:
                return int(num(row[0]))
        except sqlite3.OperationalError:
            pass
        row = connection.execute("SELECT MAX(tick) FROM agents").fetchone()
        if row is None or row[0] is None:
            return None
        return int(num(row[0]))
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()


def build_decision_input(
    tenant: str,
    current_tick: int | None,
    predictions: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    threat_by_cell: dict[str, dict[str, Any]] | None = None,
    resurvey: list[dict[str, Any]] | None = None,
    core_threats: list[dict[str, Any]] | None = None,
    mining_candidates: list[dict[str, Any]] | None = None,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Pure compose: predictions + chunks + threats -> mission-layer shape."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    threat_by_cell = threat_by_cell or {}
    refill_predictions: list[dict[str, Any]] = []
    for prediction in predictions or ():
        if not prediction.get("cell"):
            continue
        threat = threat_by_cell.get(str(prediction.get("cell")))
        refill_predictions.append(
            {
                "cell": prediction.get("cell"),
                "x": num(prediction.get("x")),
                "y": num(prediction.get("y")),
                "dueInTicks": prediction.get("dueInTicks")
                if prediction.get("dueInTicks") is not None
                else None,
                "predictedNextTick": prediction.get("predictedNextTick")
                if prediction.get("predictedNextTick") is not None
                else None,
                "lastSeenTick": num(prediction.get("lastSeenTick")),
                "threatLevel": threat.get("threatLevel") if threat else 0,
                "threatCombat": threat.get("threatCombat") if threat else 0,
            }
        )
    refill_predictions.sort(
        key=lambda item: item["dueInTicks"] if item["dueInTicks"] is not None else 1_000_000_000
    )
    chunk_coverage: list[dict[str, Any]] = []
    for chunk in chunks or ():
        key_value = chunk.get("key")
        if key_value is None:
            key_value = f"{num(chunk.get('cx'))},{num(chunk.get('cy'))}"
        last_seen = chunk.get("lastSeenTick")
        chunk_coverage.append(
            {
                "key": str(key_value),
                "cx": num(chunk.get("cx")),
                "cy": num(chunk.get("cy")),
                "lastSeenTick": None if last_seen is None else num(last_seen),
            }
        )
    chunk_coverage = [item for item in chunk_coverage if item["key"]]
    chunk_coverage.sort(
        key=lambda item: item["lastSeenTick"] if item["lastSeenTick"] is not None else -1
    )
    resurvey_targets: list[dict[str, Any]] = []
    for item in resurvey or ():
        if item.get("key") is None and (item.get("cx") is None or item.get("cy") is None):
            continue
        threat_level = num(item.get("threatLevel"))
        resurvey_targets.append(
            {
                "key": str(
                    item.get("key")
                    if item.get("key") is not None
                    else f"{num(item.get('cx'))},{num(item.get('cy'))}"
                ),
                "cx": num(item.get("cx")),
                "cy": num(item.get("cy")),
                "lastSeenTick": num(item.get("lastSeenTick")),
                "stalenessTicks": num(item.get("stalenessTicks")),
                "distChunks": num(item.get("distChunks")),
                "threatLevel": threat_level if threat_level in (1, 2, 3) else 0,
                "threatCombat": num(item.get("threatCombat")),
            }
        )
    resurvey_targets.sort(key=lambda item: num(item.get("stalenessTicks")), reverse=True)
    return {
        "generatedAt": at,
        "tenant": tenant,
        "currentTick": current_tick,
        "refillPredictions": refill_predictions,
        "chunkCoverage": chunk_coverage,
        "resurveyTargets": resurvey_targets,
        "coreThreats": list(core_threats or ()),
        "miningCandidates": list(mining_candidates or ()),
        "cachedAt": at,
    }


def load_decision_input(
    data_root: str | os.PathLike[str],
    tenant: str,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load one tenant's decision-input payload (``/api/survey/decision-input``)."""
    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    patterns = load_mine_patterns(root, tenant, now_ms=now)
    survey_path = survey_db_path(root, tenant)
    current_tick = _survey_tick_max(survey_path)
    chunk_rows = _chunks_from_db(survey_path)
    threat_by_cell: dict[str, dict[str, Any]] = {}
    try:
        for resource in load_consensus_mining(root, now_ms=now).get("resources") or ():
            cell = resource.get("cell")
            if cell and isinstance(resource.get("threatLevel"), (int, float)):
                threat_by_cell[str(cell)] = {
                    "threatLevel": int(resource["threatLevel"]),
                    "threatCombat": num(resource.get("threatCombat")),
                }
    except Exception:  # noqa: BLE001 - TS try/catch parity: threats optional
        pass
    chunk_threat: dict[str, dict[str, Any]] = {}
    for cell, threat in threat_by_cell.items():
        parts = cell.split(",")
        if len(parts) < 2:
            continue
        cx = math.floor(num(parts[0]) / CHUNK_SIZE)
        cy = math.floor(num(parts[1]) / CHUNK_SIZE)
        key = f"{int(cx)},{int(cy)}"
        current = chunk_threat.get(key)
        if (
            current is None
            or threat["threatLevel"] > current["threatLevel"]
            or (
                threat["threatLevel"] == current["threatLevel"]
                and threat["threatCombat"] > current["threatCombat"]
            )
        ):
            chunk_threat[key] = threat
    resurvey_rows: list[dict[str, Any]] = []
    try:
        for item in load_alliance_exploration(root, now_ms=now).get("resurveyTargets") or ():
            cx = num(item.get("cx"))
            cy = num(item.get("cy"))
            threat = chunk_threat.get(f"{math.floor(cx)},{math.floor(cy)}")
            resurvey_rows.append(
                {
                    "key": item.get("key"),
                    "cx": cx,
                    "cy": cy,
                    "lastSeenTick": item.get("lastSeenTick"),
                    "stalenessTicks": item.get("stalenessTicks"),
                    "distChunks": item.get("distChunks"),
                    "threatLevel": threat.get("threatLevel") if threat else 0,
                    "threatCombat": threat.get("threatCombat") if threat else 0,
                }
            )
    except Exception:  # noqa: BLE001 - TS try/catch parity: exploration optional
        pass
    core_threats: list[dict[str, Any]] = []
    try:
        friendly_core = (
            (load_alliance_snapshot(root, now_ms=now).get("members") or {})
            .get(tenant, {})
            .get("core", {})
            .get("position")
        )
        if friendly_core is not None and len(friendly_core) >= 2:
            trails = load_core_trails_from_survey_db(
                root, tenant, CORE_TRAIL_MAX_POINTS, CORE_TRAIL_MIN_POINTS
            )
            core_threats = collect_core_threats(trails, friendly_core, current_tick or 0)
    except Exception:  # noqa: BLE001 - TS try/catch parity: core trails optional
        pass
    mining_candidates: list[dict[str, Any]] = []
    try:
        util = (load_mine_utilization(root, tenant).get("tenants") or {}).get(tenant) or {}
        for candidate in (util.get("candidates") or [])[:MINING_CANDIDATES_CAP]:
            threat = threat_by_cell.get(str(candidate.get("cell")))
            mining_candidates.append(
                {
                    "cell": candidate.get("cell"),
                    "x": candidate.get("x"),
                    "y": candidate.get("y"),
                    "lastSeenTick": candidate.get("lastSeenTick") or 0,
                    "gapAgeTicks": candidate.get("gapAgeTicks")
                    if candidate.get("gapAgeTicks") is not None
                    else None,
                    "harvestFail": candidate.get("harvestFail") or 0,
                    "activity": candidate.get("activity") or 0,
                    "threatLevel": threat.get("threatLevel") if threat else 0,
                    "threatCombat": threat.get("threatCombat") if threat else 0,
                }
            )
    except Exception:  # noqa: BLE001 - TS try/catch parity: mine utilization optional
        pass
    predictions = (patterns.get("tenants") or {}).get(tenant, {}).get("predictions") or []
    return build_decision_input(
        tenant,
        current_tick,
        predictions,
        chunk_rows,
        threat_by_cell,
        resurvey_rows,
        core_threats,
        mining_candidates,
        now_ms=now,
    )
