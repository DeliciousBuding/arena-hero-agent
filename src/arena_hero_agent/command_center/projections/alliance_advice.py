"""Command Center alliance advice projection (W25).

Ports the legacy TypeScript ``/api/alliance/advice`` handler
(``loadAllianceAdvice()`` in ``alliance-advice.ts``) into the P5-4 projection
pattern: a deterministic aggregation core (``alliance/advice.py``, golden-tested
against the TS oracle) plus a thin loader that composes the existing P5-4
projections over the P5-3 data base (snapshot / survey / leaderboard / enemy
heat / mine patterns / mine utilization / decision trends / human conflict /
mining effectiveness / exploration / core trails). All I/O stays in this
loader layer; the domain model does every piece of semantic work.

Registered divergences (ALLOWED, domain-documented):

- ``generatedAt``/``cachedAt`` and every advice ``at`` are derived from the
  injectable ``now_ms`` (TS wall clock is not oracle-comparable); with the
  same ``now_ms`` the golden matches byte-for-byte.
- mine-pattern refill prediction fields are not needed by the advice layer;
  they degrade to the TS empty-data defaults.
"""

from __future__ import annotations

import os
from typing import Any

from ...alliance.advice import build_alliance_advice_payload
from ..paths import validate_data_root
from ._common import current_epoch_ms
from .alliance_snapshot import load_alliance_snapshot
from .alliance_survey import load_alliance_survey
from .conflicts import load_human_conflict
from .core_trails import load_core_trails_from_survey_db
from .decisions import load_decision_trend
from .enemy_heat import load_enemy_heat
from .exploration_coverage import load_alliance_exploration
from .leaderboard import load_leaderboard_intel
from .mine_patterns import load_mine_patterns
from .mines import load_mine_utilization
from .mining_effectiveness import load_mining_effectiveness

__all__ = ["build_alliance_advice_payload", "load_alliance_advice"]


def load_alliance_advice(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load the ``/api/alliance/advice`` payload from the P5-3 data base.

    Composes every read-model source the TS oracle composes: the W20 snapshot,
    the shared survey, leaderboard intel, enemy heat, mine patterns and
    utilization, per-tenant decision trends, human-conflict rates, mining
    effectiveness, exploration coverage, and per-tenant enemy-core trails.
    """
    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    snapshot = load_alliance_snapshot(root, now_ms=now)
    survey = load_alliance_survey(root, now_ms=now)
    leaderboard = load_leaderboard_intel(root, now_ms=now)
    enemy_heat = load_enemy_heat(root, "all", now_ms=now)
    mine_patterns = load_mine_patterns(root, "all", now_ms=now)
    mine_utilization = load_mine_utilization(root, "all")
    human_conflict = load_human_conflict(root, "all", window=3000)
    mining_effectiveness = load_mining_effectiveness(root, now_ms=now)
    exploration = load_alliance_exploration(root, now_ms=now)
    members = snapshot.get("members") or {}
    decision_trends: dict[str, Any] = {}
    core_trails: dict[str, Any] = {}
    for tenant in members:
        decision_trends[tenant] = load_decision_trend(root, str(tenant), window=500, steps=4)
        core_trails[tenant] = load_core_trails_from_survey_db(
            root, str(tenant), max_points=48, min_points=1
        )
    return build_alliance_advice_payload(
        now_ms=now,
        snapshot=snapshot,
        survey=survey,
        leaderboard=leaderboard,
        enemy_heat=enemy_heat,
        mine_patterns=mine_patterns,
        mine_utilization=mine_utilization,
        decision_trends=decision_trends,
        human_conflict=human_conflict,
        mining_effectiveness=mining_effectiveness,
        exploration=exploration,
        core_trails=core_trails,
    )
