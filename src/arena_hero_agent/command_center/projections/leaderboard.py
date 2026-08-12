"""Leaderboard intel projection (W25, extended W44 wave 7).

Port of the legacy TypeScript ``packages/command-center/lib/leaderboard.ts``:
read the newest ``leaderboard-*.json`` snapshot under ``<data_root>/leaderboard``
and derive the aggression tiers (damage top10 = ELITE_AGGRESSOR, top30 =
AGGRESSOR, else STANDARD) plus dynamic staleness (``ageSeconds`` / ``stale``)
from the snapshot file mtime (``loadLeaderboardIntel``), extract the 4 tenant
official account names from the controlled CORE owner_username in the latest
calibration run (``loadOurUsernames``), and compose the ``/api/leaderboard``
route payload (``buildLeaderboardPayload``) that enriches every profile with
``ours`` and the encountered-player index built from
``loadAllianceIntel().enemies``. Pure reads; fail-open to ``None``/empty when
the directory or snapshot is missing or malformed (TS returns null / 404).

Registered differences from the TS oracle:

- ``now_ms`` is injectable (TS ``ageSeconds`` derives from ``Date.now()``);
- the route payload returns 200 with an empty success shape (plus the TS 404
  ``error`` text) when the snapshot is missing, instead of TS HTTP 404 —
  wave-7 fail-open discipline (never 500);
- ``maybeRefreshLeaderboardLazy`` is not ported: it performs an external
  official-API fetch (write side effect) which stays a gated P5-9 route;
- ``history.jsonl`` is only written by the POST refresh path and is not part
  of the GET payload in server.ts, so no read is ported for it.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import latest_run_dir, list_cases, parse_tick
from ..paths import TENANTS, calibration_dir, validate_data_root
from ._common import current_epoch_ms, num

__all__ = [
    "OUR_USERNAME_SCAN_LIMIT",
    "SNAPSHOT_STALE_SECONDS",
    "build_leaderboard_payload",
    "load_leaderboard_intel",
    "load_our_usernames",
]

SNAPSHOT_STALE_SECONDS = 15 * 60
OUR_USERNAME_SCAN_LIMIT = 24  # latest calibration run case scan window (TS slice(-24))

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


def _load_leaderboard_with_mtime(
    root: Path,
    now_ms: int,
) -> tuple[dict[str, Any], int] | None:
    """Newest leaderboard snapshot + file mtime ms (TS buildIntel + statSync)."""
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
    return _build_intel(raw, files[0], snapshot_at_ms, now_ms), snapshot_at_ms


def load_leaderboard_intel(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    """Load the newest leaderboard snapshot (``/api/leaderboard`` source)."""
    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    loaded = _load_leaderboard_with_mtime(root, now)
    return loaded[0] if loaded is not None else None


def load_our_usernames(data_root: str | os.PathLike[str]) -> list[dict[str, str]]:
    """Our 4 tenants' official account names (TS ``loadOurUsernames``).

    Scans the latest calibration run's last ``OUR_USERNAME_SCAN_LIMIT`` cases
    for a controlled CORE with a non-empty ``owner_username``; the newest tick
    wins (``>=`` so the ``after`` state of the newest case wins, TS parity).
    """
    root = validate_data_root(data_root)
    ours: list[dict[str, str]] = []
    for tenant in TENANTS:
        run_dir = latest_run_dir(root, tenant)
        if run_dir is None:
            continue
        case_files = list_cases(root, tenant, run_dir)[-OUR_USERNAME_SCAN_LIMIT:]
        username: str | None = None
        best_tick = -1
        base = calibration_dir(root, tenant) / run_dir / "cases"
        for file in case_files:
            tick = parse_tick(file)
            try:
                raw = json.loads((base / file).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            for state_key in ("before", "after"):
                holder = raw.get(state_key)
                state = holder.get("state") if isinstance(holder, dict) else None
                if not isinstance(state, dict):
                    continue
                objects = state.get("objects")
                if not isinstance(objects, list):
                    continue
                for obj in objects:
                    if (
                        isinstance(obj, dict)
                        and obj.get("kind") == "CORE"
                        and obj.get("controlled") is True
                        and isinstance(obj.get("owner_username"), str)
                        and obj["owner_username"]
                        and tick >= best_tick
                    ):
                        best_tick = tick
                        username = obj["owner_username"]
        if username:
            ours.append({"tenant": tenant, "username": username})
    return ours


def build_leaderboard_payload(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
    intel_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``/api/leaderboard`` route payload (TS server.ts composition).

    Enriches every leaderboard profile with ``ours`` (the owning tenant) and
    ``encountered`` (the per-tenant encounter entries from the intel enemies
    index), and appends ``ours`` / ``encounteredCount`` / ``encountered`` plus
    the snapshot ``snapshotAtMs`` the TS oracle emits. Fail-open: a missing
    snapshot returns 200 with an empty success shape + the TS 404 ``error``
    text (registered divergence; TS responds 404).
    """
    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    loaded = _load_leaderboard_with_mtime(root, now)
    if loaded is None:
        return {
            "generatedAt": iso_utc(now),
            "profiles": [],
            "ours": [],
            "encounteredCount": 0,
            "encountered": {},
            "error": (
                "排行榜快照缺失（运行 docs/progress/leaderboard-intel.py 拉取，"
                "或 POST /api/leaderboard/refresh）"
            ),
        }
    leaderboard, snapshot_at_ms = loaded
    ours = load_our_usernames(root)
    # Lazy import avoids a module cycle: intel.py imports this module for the
    # leaderboard profiles its scan joins.
    from .intel import build_encountered_index_from_enemies, load_alliance_intel

    if intel_payload is None:
        intel_payload = load_alliance_intel(root, now_ms=now)
    encountered = build_encountered_index_from_enemies(intel_payload["enemies"])
    profiles: list[dict[str, Any]] = []
    for profile in leaderboard.get("profiles") or ():
        entry = dict(profile)
        entry["ours"] = next(
            (item["tenant"] for item in ours if item["username"] == profile["username"]),
            None,
        )
        entry["encountered"] = encountered.get(profile["username"])
        profiles.append(entry)
    return {
        **leaderboard,
        "snapshotAtMs": snapshot_at_ms,
        "profiles": profiles,
        "ours": ours,
        "encounteredCount": len(encountered),
        "encountered": encountered,
    }
