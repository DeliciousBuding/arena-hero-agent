"""Enemy-core trail projection (W25).

Port of the legacy TypeScript ``packages/command-center/lib/trails.ts``
(``loadCoreTrailsFromSurveyDb``): group survey-db ``core_hunts`` rows by owner
(last-seen ascending = trajectory sequence, consecutive same-cell points
deduplicated), keep at least ``min_points`` per trail and at most
``max_points`` (most recent), and order trails by length descending. The
advice layer consumes these trails via ``alliance/advice.collect_core_threats``
for approaching / proximity enemy-core threats.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from ..paths import survey_db_path, validate_data_root

__all__ = ["load_core_trails_from_survey_db"]


def _trails_from_db(path: Path, max_points: int, min_points: int) -> list[dict[str, Any]]:
    """Survey-db core_hunts -> per-owner trajectory (TS ``loadCoreTrailsFromSurveyDb``)."""
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(
            "SELECT owner, x, y, last_seen_tick FROM core_hunts"
            " WHERE owner IS NOT NULL AND owner != '' ORDER BY last_seen_tick ASC"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    by_user: dict[str, list[dict[str, int]]] = {}
    for row in rows:
        owner = str(row[0])
        x = int(row[1])
        y = int(row[2])
        tick = int(row[3])
        trail = by_user.setdefault(owner, [])
        if trail and trail[-1]["x"] == x and trail[-1]["y"] == y:
            continue
        trail.append({"x": x, "y": y, "tick": tick})
    out: list[dict[str, Any]] = []
    for username, points in by_user.items():
        if len(points) >= min_points:
            out.append(
                {
                    "username": username,
                    "trail": points[-max_points:] if len(points) > max_points else points,
                }
            )
    out.sort(key=lambda item: len(item["trail"]), reverse=True)
    return out


def load_core_trails_from_survey_db(
    data_root: str | os.PathLike[str],
    tenant: str,
    max_points: int = 48,
    min_points: int = 2,
) -> list[dict[str, Any]]:
    """Load one tenant's enemy-core trails from the survey database."""
    root = validate_data_root(data_root)
    return _trails_from_db(survey_db_path(root, tenant), max_points, min_points)
