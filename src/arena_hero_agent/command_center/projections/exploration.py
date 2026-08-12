"""Per-tenant survey exploration (port of legacy ``server.ts`` + ``survey-cache.ts``).

Ports the ``/api/exploration`` route composition: the TS
``loadTenantSurveyCached`` payload (survey from the survey database, lifecycle
summary, spend trend, unit detail, chunks) plus the ``current`` world subset
(``loadWorld`` -> caseFile/tick/objects/resources/population/champion_beacon).
When the survey database is missing the route falls back to the cumulative
calibration scan (TS ``loadSurvey``); when both are absent it fails open with
``survey: null`` (never 500).

Registered differences from the TS oracle:

- ``generatedAt`` / ``cachedAt`` are injectable via ``now_ms`` (TS
  ``new Date().toISOString()``) and there is no in-memory TTL cache.
"""

from __future__ import annotations

import os
from typing import Any

from ..goal_store import iso_utc
from ..paths import validate_data_root
from ._common import current_epoch_ms
from .snapshots import load_world
from .survey import _load_survey_from_cases, load_tenant_survey_cached

__all__ = ["load_exploration"]


def load_exploration(
    data_root: str | os.PathLike[str],
    tenant: str,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """``/api/exploration`` route payload (survey + lifecycle + current world)."""
    now = now_ms if now_ms is not None else current_epoch_ms()
    at = iso_utc(now)
    validate_data_root(data_root)
    cached = load_tenant_survey_cached(data_root, tenant, now_ms=now)
    survey = cached["survey"]
    if survey is None:
        survey = _load_survey_from_cases(data_root, tenant)
    if survey is None:
        return {"tenant": tenant, "generatedAt": at, "survey": None}
    world = load_world(data_root, tenant, now_ms=now)
    state = world.get("state")
    current: dict[str, Any] | None = None
    if isinstance(state, dict):
        current = {
            "caseFile": world.get("caseFile"),
            "tick": world.get("tick"),
            "objects": state.get("objects"),
            "resources": state.get("resources"),
            "population": state.get("population"),
            "champion_beacon": state.get("champion_beacon"),
        }
    return {
        "tenant": tenant,
        "generatedAt": at,
        "survey": survey,
        "lifecycle": cached["lifecycle"],
        "current": current,
    }
