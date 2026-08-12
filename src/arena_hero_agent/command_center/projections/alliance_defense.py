"""Command Center alliance defense projection (W21).

Ports the legacy TypeScript ``/api/alliance/defense`` handler (server.ts inline
composition over ``loadAllianceSnapshot()``) into the P5-4 projection pattern:
a deterministic aggregation core (golden-tested against the TS oracle) plus a
thin loader that reuses the W20 snapshot loader. The pure domain model
(``alliance/defense.py``) does all semantic work (endangered / reinforce /
formation / pocket advice); this module composes the snapshot payload
(members / sightings / threatSummaries) into the ``/api/alliance/defense``
payload (TS ``DefensePayload`` with merged advice and pockets).

Registered divergences (ALLOWED, domain-documented):

- ``generatedAtMs`` is injectable via ``now_ms`` (TS ``Date.now()`` is wall
  clock, not oracle-comparable);
- enemy cores with non-finite positions are dropped before POCKET clustering
  (TS ``Number.isFinite`` filter applied at the payload boundary; the domain
  operates on validated integer Coordinates);
- advice strings interpolate numbers with JS ``String(number)`` semantics
  (``alliance/defense.py``) so text matches the oracle byte-for-byte.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from typing import Any

from arena_hero_agent.alliance.defense import (
    DefenseAdvice,
    DefenseMemberInput,
    DefensePocket,
    build_defense_coordination,
    build_defense_pocket_advice,
    build_defense_pockets,
)
from arena_hero_agent.domain import Coordinate

from ..paths import validate_data_root
from ._common import current_epoch_ms, num
from .alliance_snapshot import load_alliance_snapshot

__all__ = ["build_alliance_defense_payload", "load_alliance_defense"]


# --- pure aggregation core (golden-tested against the TS oracle) -----------


def build_alliance_defense_payload(
    *,
    members: Mapping[str, Any],
    sightings: Sequence[Mapping[str, Any]],
    threat_summaries: Sequence[Mapping[str, Any]],
    now_ms: int = 0,
) -> dict[str, Any]:
    """Compose the ``/api/alliance/defense`` payload (TS server inline core).

    Mirrors the in-memory section of the TS handler: snapshot members mapped to
    defense inputs (military = vanguards + rangers, threat summaries joined by
    tenant), enemy CORE sightings to pocket cores, then coordination + pocket
    advice merged and re-sorted by severity then id. Pure and deterministic.
    """
    scores: dict[str, int | float] = {}
    directions: dict[str, tuple[str, ...]] = {}
    counts: dict[str, int] = {}
    for summary in threat_summaries:
        tenant = str(summary.get("tenantId", ""))
        scores[tenant] = num(summary.get("totalScore", 0))
        directions[tenant] = tuple(str(item) for item in (summary.get("highDirections") or ()))
        counts[tenant] = sum(
            int(num(sector.get("entityCount", 0)))
            for sector in (summary.get("sectors") or ())
            if isinstance(sector, Mapping)
        )
    domain_members = [
        _defense_member_from_payload(member, scores, directions, counts)
        for member in members.values()
        if isinstance(member, Mapping)
    ]
    enemy_cores = _pocket_cores_from_sightings(sightings)
    coordination = build_defense_coordination(domain_members, now_ms=now_ms)
    pockets = build_defense_pockets(domain_members, enemy_cores)
    pocket_advice = build_defense_pocket_advice(domain_members, enemy_cores)
    merged = sorted(
        (*coordination.advice, *pocket_advice),
        key=_advice_sort_key,
    )
    return {
        "generatedAtMs": coordination.generated_at_ms,
        "advice": [_advice_payload(item) for item in merged],
        "endangered": [
            {
                "tenantId": entry.tenant_id,
                "military": entry.military,
                "threatScore": entry.threat_score,
            }
            for entry in coordination.endangered
        ],
        "pockets": [_pocket_payload(item) for item in pockets],
    }


def _advice_sort_key(item: DefenseAdvice) -> tuple[int, str]:
    """Severity order then id (TS ``sevOrder`` comparator in the endpoint)."""
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "INFO": 3}
    return (order[item.severity.value], item.id)


def _defense_member_from_payload(
    member: Mapping[str, Any],
    scores: Mapping[str, int | float],
    directions: Mapping[str, tuple[str, ...]],
    counts: Mapping[str, int],
) -> DefenseMemberInput:
    """One snapshot member -> defense input (TS server members map)."""
    tenant = str(member.get("tenantId", ""))
    core = _core_of(member.get("core"))
    return DefenseMemberInput(
        tenant_id=tenant,
        core=core,
        military=int(num(member.get("vanguards", 0))) + int(num(member.get("rangers", 0))),
        status=str(member.get("status", "")),
        threat_score=num(scores.get(tenant, 0)),
        threat_directions=directions.get(tenant, ()),
        threat_count=int(num(counts.get(tenant, 0))),
    )


def _core_of(value: object) -> Coordinate | None:
    """Snapshot member core -> Coordinate (TS ``m.core?.position ?? null``)."""
    if not isinstance(value, Mapping):
        return None
    position = value.get("position")
    if not isinstance(position, Sequence) or isinstance(position, (str, bytes)):
        return None
    if len(position) < 2:
        return None
    x = num(position[0])
    y = num(position[1])
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    if isinstance(y, bool) or not isinstance(y, (int, float)):
        return None
    return Coordinate(int(x), int(y))


def _pocket_cores_from_sightings(
    sightings: Sequence[Mapping[str, Any]],
) -> list[Any]:
    """Enemy CORE sightings -> pocket cores (TS server filter + map).

    Cores with non-finite positions are dropped (TS ``Number.isFinite`` guard
    inside ``buildDefensePockets`` is applied at this payload boundary).
    """
    from arena_hero_agent.alliance.defense import PocketEnemyCore

    cores: list[PocketEnemyCore] = []
    for sighting in sightings:
        if str(sighting.get("kind", "")) != "CORE":
            continue
        owner = sighting.get("ownerUsername")
        if not isinstance(owner, str):
            continue
        position = sighting.get("position")
        if not isinstance(position, Sequence) or isinstance(position, (str, bytes)):
            continue
        if len(position) < 2:
            continue
        if not _is_finite_number(position[0]) or not _is_finite_number(position[1]):
            continue
        cores.append(
            PocketEnemyCore(
                key=str(sighting.get("key", "")),
                owner=owner,
                position=Coordinate(int(position[0]), int(position[1])),
                last_seen_tick=int(num(sighting.get("lastSeenTick", 0))),
            )
        )
    return cores


def _is_finite_number(value: object) -> bool:
    """JS ``Number.isFinite`` (only finite numbers, not strings/null/booleans)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _advice_payload(item: DefenseAdvice) -> dict[str, Any]:
    return {
        "id": item.id,
        "category": item.category.value,
        "severity": item.severity.value,
        "title": item.title,
        "detail": item.detail,
        "tenant": item.tenant,
        "relatedTenants": list(item.related_tenants),
        "evidence": [{"label": label, "value": value} for label, value in item.evidence],
    }


def _pocket_payload(item: DefensePocket) -> dict[str, Any]:
    return {
        "id": item.id,
        "centroid": [item.centroid.x, item.centroid.y],
        "enemyCores": [
            {"owner": owner, "position": [position.x, position.y]}
            for owner, position in item.enemy_cores
        ],
        "threatenedTenants": list(item.threatened_tenants),
        "minDistance": item.min_distance,
    }


# --- thin loader over the W20 snapshot payload -----------------------------


def load_alliance_defense(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load the ``/api/alliance/defense`` payload from the P5-3 data base.

    The defense read model consumes the W20 snapshot payload (the same source
    the TS handler reads via ``loadAllianceSnapshot()``); the snapshot loader
    is the single reader of the runtime artifacts.
    """
    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    snapshot = load_alliance_snapshot(root, now_ms=now)
    return build_alliance_defense_payload(
        members=snapshot["members"],
        sightings=snapshot["sightings"],
        threat_summaries=snapshot["threatSummaries"],
        now_ms=now,
    )
