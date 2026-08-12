"""Survey mine-cell lifecycle detail (port of legacy ``server.ts`` + ``survey.ts``).

Ports the ``/api/survey/mine`` route semantics: read one tenant's survey-db
``resources`` in the TS ``loadSurveyDb`` shape (x/y/last-seen tick, derived
fresh/stale state with persisted harvested/empty negative states winning,
harvest aggregation from ``resource_events``) and return the requested cell's
mine plus its ``resource_events`` timeline (``loadResourceTimeline``). The
``cell`` query is ``x,y``; when absent or non-numeric the most recently seen
resource is chosen (TS ``sort tick desc`` default). ``/api/survey/mine``.

Registered differences from the TS oracle:

- ``resource_events`` is not part of the P5-3 Python survey schema; a missing
  table degrades to an empty harvest ledger / empty timeline (TS would throw
  and report the database as missing).
- ``now_ms`` is accepted for loader-signature consistency but unused: the TS
  response carries no timestamp.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from ..paths import survey_db_path, validate_data_root
from ._common import finite_number, num
from .mines import RESOURCE_FRESH_WINDOW_TICKS

__all__ = ["load_survey_mine"]

_TIMELINE_LIMIT = 500


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


def _read_resource_cells(connection: sqlite3.Connection, tick_max: int) -> list[dict[str, Any]]:
    """Survey-db resources in the TS ``loadSurveyDb`` resourceCells shape."""
    rows = connection.execute(
        "SELECT x, y, last_seen_tick, first_seen_tick, state, seen_count FROM resources"
    ).fetchall()
    try:
        harvest_rows = connection.execute(
            "SELECT cell, COUNT(*) AS n, MAX(tick) AS lastTick FROM resource_events"
            " WHERE event_type = 'HARVEST_SUCCEEDED' GROUP BY cell"
        ).fetchall()
    except sqlite3.OperationalError:
        harvest_rows = ()
    harvest_by_cell = {str(row[0]): (int(num(row[1])), num(row[2])) for row in harvest_rows}
    out: list[dict[str, Any]] = []
    for row in rows:
        x = int(num(row[0]))
        y = int(num(row[1]))
        last_seen = int(num(row[2]))
        first_seen = int(num(row[3]))
        db_state = str(row[4] or "")
        seen_count = int(num(row[5]))
        age_ticks = max(0, tick_max - last_seen) if tick_max > 0 else 0
        fresh = age_ticks <= RESOURCE_FRESH_WINDOW_TICKS
        state = db_state if db_state in ("harvested", "empty") else "visible" if fresh else "stale"
        harvest = harvest_by_cell.get(f"{x},{y}")
        out.append(
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
    return out


def _read_timeline(connection: sqlite3.Connection, cell: str) -> list[dict[str, Any]]:
    """``resource_events`` for one cell, tick ascending (TS ``loadResourceTimeline``)."""
    try:
        rows = connection.execute(
            "SELECT tick, event_type AS eventType, reason_code AS reason, amount,"
            " actor_id AS actorId FROM resource_events WHERE cell = ? ORDER BY tick ASC"
            " LIMIT ?",
            (cell, _TIMELINE_LIMIT),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "tick": num(row[0]),
            "eventType": str(row[1]),
            "reason": row[2],
            "amount": row[3],
            "actorId": row[4],
        }
        for row in rows
    ]


def _parse_cell(raw: str | None) -> tuple[int | None, int | None]:
    """Parse the ``x,y`` cell query; ``(None, None)`` when not determinable."""
    if raw is None:
        return None, None
    parts = raw.split(",")
    if len(parts) < 2:
        return None, None
    x = finite_number(parts[0].strip())
    y = finite_number(parts[1].strip())
    if x is None or y is None:
        return None, None
    return int(x), int(y)


def load_survey_mine(
    data_root: str | os.PathLike[str],
    tenant: str,
    cell: str | None = None,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load one mine cell + timeline (``/api/survey/mine`` source)."""
    root = validate_data_root(data_root)
    path = survey_db_path(root, tenant)
    if not path.is_file():
        return {"tenant": tenant, "error": "survey db missing"}
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {"tenant": tenant, "error": "survey db missing"}
    try:
        tick_max = _sync_tick_max(connection)
        resources = _read_resource_cells(connection, tick_max)
    except sqlite3.Error:
        return {"tenant": tenant, "error": "survey db missing"}
    finally:
        connection.close()
    x, y = _parse_cell(cell)
    mine: dict[str, Any] | None = None
    if x is not None and y is not None:
        for resource in resources:
            if resource["x"] == x and resource["y"] == y:
                mine = resource
                break
    elif resources:
        mine = max(resources, key=lambda item: num(item.get("tick")))
    if mine is None:
        return {"tenant": tenant, "mine": None, "timeline": []}
    cell_key = f"{mine['x']},{mine['y']}"
    return {
        "tenant": tenant,
        "mine": mine,
        "cell": cell_key,
        "timeline": _timeline_lazy(path, cell_key),
    }


def _timeline_lazy(path: Path, cell: str) -> list[dict[str, Any]]:
    """Read the timeline with its own short-lived connection (table may be absent)."""
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        return _read_timeline(connection, cell)
    finally:
        connection.close()
