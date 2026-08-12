"""Leaderboard intel projection (W25).

Port of the legacy TypeScript ``packages/command-center/lib/leaderboard.ts``
(``loadLeaderboardIntel``): read the newest ``leaderboard-*.json`` snapshot
under ``<data_root>/leaderboard`` and derive the aggression tiers (damage
top10 = ELITE_AGGRESSOR, top30 = AGGRESSOR, else STANDARD) plus dynamic
staleness (``ageSeconds`` / ``stale``) from the snapshot file mtime. Pure
read of a single JSON artifact; fail-open to ``None`` when the directory or
snapshot is missing or malformed (TS ``loadLeaderboardIntel`` returns null).

Registered divergence from the TS oracle: ``now_ms`` is injectable; the TS
derives ``ageSeconds`` from ``Date.now()`` (wall clock), which is not
oracle-comparable.
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any

from ..goal_store import iso_utc
from ..paths import validate_data_root
from ._common import current_epoch_ms, num

__all__ = ["SNAPSHOT_STALE_SECONDS", "load_leaderboard_intel"]

SNAPSHOT_STALE_SECONDS = 15 * 60

_LEADERBOARD_SNAPSHOT_RE = re.compile(r"^leaderboard-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.json$")


def _tier_of(rank: int) -> str:
    """Official damage-rank aggression tier (TS ``tierOf``)."""
    if rank >= 1 and rank <= 10:
        return "ELITE_AGGRESSOR"
    if rank <= 30:
        return "AGGRESSOR"
    return "STANDARD"


def _build_intel(
    raw: dict[str, Any],
    snapshot_name: str,
    snapshot_at_ms: int,
    now_ms: int,
) -> dict[str, Any]:
    """Raw snapshot -> leaderboard intel (TS ``buildIntel``)."""
    profiles: list[dict[str, Any]] = []
    for row in raw.get("damage_dealt") or ():
        if not isinstance(row, dict):
            continue
        rank = int(num(row.get("rank")))
        profiles.append(
            {
                "username": str(row.get("username")),
                "rank": rank,
                "damage": num(row.get("score")),
                "tier": _tier_of(rank),
            }
        )
    age_seconds = max(0, math.floor((now_ms - snapshot_at_ms) / 1000 + 0.5))
    return {
        "generatedAt": iso_utc(now_ms),
        "snapshot": snapshot_name,
        "snapshotAt": iso_utc(snapshot_at_ms),
        "ageSeconds": age_seconds,
        "stale": age_seconds > SNAPSHOT_STALE_SECONDS,
        "beacon_ticks_held": raw.get("beacon_ticks_held") or [],
        "damage_dealt": raw.get("damage_dealt") or [],
        "core_destruction_participations": raw.get("core_destruction_participations") or [],
        "profiles": profiles,
    }


def load_leaderboard_intel(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    """Load the newest leaderboard snapshot (``/api/leaderboard`` source)."""
    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    directory = root / "leaderboard"
    if not directory.is_dir():
        return None
    files = sorted(
        (name for name in os.listdir(directory) if _LEADERBOARD_SNAPSHOT_RE.match(name)),
        reverse=True,
    )
    if not files:
        return None
    path = directory / files[0]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("damage_dealt"), list):
        return None
    snapshot_at_ms = int(path.stat().st_mtime * 1000)
    return _build_intel(raw, files[0], snapshot_at_ms, now)
