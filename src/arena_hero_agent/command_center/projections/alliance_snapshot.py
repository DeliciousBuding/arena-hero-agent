"""Command Center alliance snapshot projection (W20).

Ports the legacy TypeScript ``alliance-snapshot.ts`` loader into the P5-4
projection pattern: a deterministic aggregation core (golden-tested against the
TS oracle) plus a thin loader over the P5-3 data base. The canonical alliance
domain model (``alliance/snapshot.py`` + ``alliance/threat.py`` +
``alliance/shared_intel.py``) does all the semantic work; this module composes
members / sightings / counts / intel / threat / threatSummaries into the
``/api/alliance/snapshot`` payload (TS ``AllianceSnapshotPayload``).

Registered divergences (ALLOWED, domain-documented P4-17 stale-data guard):

- wall-clock ``generatedAt``/``cachedAt`` are injectable via ``now_ms`` (not
  oracle-comparable);
- the payload golden fixtures stay inside the stale-safe envelope (no
  HISTORICAL sightings): the Python snapshot build and threat summaries
  exclude HISTORICAL sightings fail-closed (``project_threat_field`` /
  ``build_threat_summaries_from_snapshot``), and ``maxDirect`` breaks ties by
  the smallest cell key instead of first-inserted;
- leaderboard aggression degrades to empty when no leaderboard snapshot exists
  (TS ``lb?.profiles ?? []``).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from arena_hero_agent.alliance.shared_intel import (
    FusedEntitySighting,
    SharedIntelCounts,
    SharedIntelView,
    aggregate_alliance_intel,
)
from arena_hero_agent.alliance.snapshot import (
    AllianceMemberState,
    AllianceObservation,
    CoreRef,
    EntitySighting,
    EvidenceKind,
    MemberStatus,
    SightingKind,
    UnitType,
    build_alliance_snapshot_from_sightings,
    current_confidence,
    observations_to_sightings,
)
from arena_hero_agent.alliance.threat import (
    TenantThreatSummary,
    ThreatCell,
    ThreatSector,
    build_threat_summaries_from_snapshot,
)
from arena_hero_agent.domain import Coordinate, TenantId

from ..goal_store import iso_utc
from ..jsonl import latest_run_dir, list_cases
from ..paths import TENANTS, calibration_dir, survey_db_path, validate_data_root
from ._common import current_epoch_ms, num

__all__ = ["build_alliance_snapshot_payload", "load_alliance_snapshot"]

TOP_CELLS_CAP = 300
_LEADERBOARD_SNAPSHOT_RE = re.compile(r"^leaderboard-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.json$")


# --- pure aggregation core (golden-tested against the TS oracle) -----------


def build_alliance_snapshot_payload(
    *,
    revision: int,
    members: Sequence[AllianceMemberState],
    sightings: Sequence[EntitySighting],
    ally_entity_ids: frozenset[str] | Sequence[str],
    now_tick: int,
    generated_at_ms: int = 0,
    leaderboard_aggression: Mapping[str, float] | None = None,
    treasury_tenant: TenantId | None = None,
) -> dict[str, Any]:
    """Compose the ``/api/alliance/snapshot`` payload (TS ``loadAllianceSnapshot`` core).

    Mirrors the in-memory section of the TS loader: canonical snapshot build,
    fused shared-intel view, per-tenant threat summaries, and the threat
    top-cells projection (score-descending, capped). Pure and deterministic.
    """
    snapshot = build_alliance_snapshot_from_sightings(
        revision=revision,
        members=members,
        sightings=sightings,
        ally_entity_ids=ally_entity_ids,
        now_tick=now_tick,
        generated_at_ms=generated_at_ms,
        leaderboard_aggression=leaderboard_aggression,
        treasury_tenant=treasury_tenant,
    )
    intel = aggregate_alliance_intel(
        sightings=sightings,
        ally_entity_ids=ally_entity_ids,
        current_tick=now_tick,
    )
    summaries = build_threat_summaries_from_snapshot(snapshot)
    threat = snapshot.threat
    scored = sorted(
        threat.cells.items(),
        key=lambda item: -(item[1].direct_combat + item[1].projected_combat + item[1].core_raid),
    )
    top_cells = [
        {"key": key, "cell": _threat_cell_payload(cell)} for key, cell in scored[:TOP_CELLS_CAP]
    ]
    return {
        "currentTick": now_tick,
        "revision": snapshot.revision,
        "members": {
            tenant.value: _member_payload(member) for tenant, member in snapshot.members.items()
        },
        "sightings": [_sighting_payload(sighting) for sighting in snapshot.sightings],
        "counts": _counts_payload(snapshot.counts),
        "intel": _intel_payload(intel),
        "threat": {
            "topCells": top_cells,
            "cellCount": len(threat.cells),
            "maxDirect": _threat_cell_payload(threat.max_direct)
            if threat.max_direct is not None
            else None,
            "estimatedCombatForce": threat.estimated_combat_force,
            "tickWindow": [threat.tick_window[0], threat.tick_window[1]],
            "generatedAtMs": threat.generated_at_ms,
        },
        "threatSummaries": [_threat_summary_payload(summary) for summary in summaries],
        "treasuryTenant": (
            snapshot.treasury_tenant.value if snapshot.treasury_tenant is not None else ""
        ),
        "leaderboardAggression": dict(leaderboard_aggression or {}),
    }


# --- payload serializers (TS field names, optional entity fields omitted) ---


def _core_payload(core: CoreRef) -> dict[str, Any]:
    return {
        "id": core.id,
        "position": [core.position.x, core.position.y],
        "hp": core.hp,
        "shield": core.shield,
        "moving": core.moving,
    }


def _member_payload(member: AllianceMemberState) -> dict[str, Any]:
    return {
        "tenantId": member.tenant_id.value,
        "tick": member.tick,
        "observedAtMs": member.observed_at_ms,
        "core": _core_payload(member.core) if member.core is not None else None,
        "resources": member.resources,
        "resourceCapacity": member.resource_capacity,
        "population": member.population,
        "workers": member.workers,
        "vanguards": member.vanguards,
        "rangers": member.rangers,
        "carriedResources": member.carried_resources,
        "activeFleetIds": list(member.active_fleet_ids),
        "localThreat": member.local_threat,
        "localHarvestRate": member.local_harvest_rate,
        "status": member.status.value,
    }


def _sighting_payload(
    sighting: EntitySighting | FusedEntitySighting,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "key": sighting.key,
        "kind": sighting.kind.value,
        "position": [sighting.position.x, sighting.position.y],
        "sourceTenant": sighting.source_tenant.value,
        "firstSeenTick": sighting.first_seen_tick,
        "lastSeenTick": sighting.last_seen_tick,
        "currentlyVisible": sighting.currently_visible,
        "confidence": sighting.confidence,
        "evidence": sighting.evidence.value,
    }
    if sighting.unit_type is not None:
        payload["unitType"] = sighting.unit_type.value
    if sighting.entity_id is not None:
        payload["entityId"] = sighting.entity_id
    if sighting.owner_username is not None:
        payload["ownerUsername"] = sighting.owner_username
    return payload


def _counts_payload(counts: Any) -> dict[str, Any]:
    return {
        "currentVisibleCombat": counts.current_visible_combat,
        "recentUniqueCombat": counts.recent_unique_combat,
        "historicalSightingCount": counts.historical_sighting_count,
        "estimatedForce": counts.estimated_force,
    }


def _threat_cell_payload(cell: ThreatCell) -> dict[str, Any]:
    return {
        "position": [cell.position.x, cell.position.y],
        "directCombat": cell.direct_combat,
        "projectedCombat": cell.projected_combat,
        "coreRaid": cell.core_raid,
        "uncertainty": cell.uncertainty,
    }


def _fused_payload(sighting: FusedEntitySighting) -> dict[str, Any]:
    payload = _sighting_payload(sighting)
    payload["sourceTenants"] = list(sighting.source_tenants)
    payload["ageTicks"] = sighting.age_ticks
    payload["decayedConfidence"] = sighting.decayed_confidence
    payload["freshness"] = sighting.freshness.value
    return payload


def _intel_counts_payload(counts: SharedIntelCounts) -> dict[str, Any]:
    return {
        "currentEnemyUnits": counts.current_enemy_units,
        "currentEnemyCores": counts.current_enemy_cores,
        "recentEnemyUnits": counts.recent_enemy_units,
        "recentEnemyCores": counts.recent_enemy_cores,
        "historicalEnemyUnits": counts.historical_enemy_units,
        "historicalEnemyCores": counts.historical_enemy_cores,
    }


def _intel_payload(intel: SharedIntelView) -> dict[str, Any]:
    return {
        "currentTick": intel.current_tick,
        "memberReports": [dict(report) for report in intel.member_reports],
        "currentlyVisible": [_fused_payload(item) for item in intel.currently_visible],
        "recentFused": [_fused_payload(item) for item in intel.recent_fused],
        "historicalKnown": [_fused_payload(item) for item in intel.historical_known],
        "counts": _intel_counts_payload(intel.counts),
    }


def _sector_payload(sector: ThreatSector) -> dict[str, Any]:
    return {
        "direction": sector.direction.value,
        "score": sector.score,
        "entityCount": sector.entity_count,
        "nearestDistance": sector.nearest_distance,
        "entityKeys": list(sector.entity_keys),
    }


def _threat_summary_payload(summary: TenantThreatSummary) -> dict[str, Any]:
    return {
        "tenantId": summary.tenant_id.value,
        "corePosition": (
            [summary.core_position.x, summary.core_position.y]
            if summary.core_position is not None
            else None
        ),
        "sectors": [_sector_payload(sector) for sector in summary.sectors],
        "highDirections": [direction.value for direction in summary.high_directions],
        "multiDirectionPressure": summary.multi_direction_pressure,
        "totalScore": summary.total_score,
    }


# --- thin loader over the P5-3 data base (world cases + survey-db) ---------


def _position_of(value: object) -> Coordinate:
    if isinstance(value, list) and len(value) >= 2:
        return Coordinate(int(num(value[0])), int(num(value[1])))
    return Coordinate(0, 0)


def _as_unit_type(value: object) -> UnitType | None:
    text = str(value or "")
    if text == "WORKER":
        return UnitType.WORKER
    if text == "VANGUARD":
        return UnitType.VANGUARD
    if text == "RANGER":
        return UnitType.RANGER
    return None


def _load_world(data_root: Path, tenant: str) -> dict[str, Any]:
    """Latest calibration case world snapshot (TS ``loadWorld``)."""
    run_dir = latest_run_dir(data_root, tenant)
    if run_dir is None:
        return {"tenant": tenant, "tick": None, "state": None}
    files = list_cases(data_root, tenant, run_dir)
    if not files:
        return {"tenant": tenant, "tick": None, "state": None}
    path = calibration_dir(data_root, tenant) / run_dir / "cases" / files[-1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tenant": tenant, "tick": None, "state": None}
    if not isinstance(raw, dict):
        return {"tenant": tenant, "tick": None, "state": None}
    after = raw.get("after") if isinstance(raw.get("after"), dict) else {}
    before = raw.get("before") if isinstance(raw.get("before"), dict) else {}
    tick = after.get("tick") if after.get("tick") is not None else before.get("tick")
    state = after.get("state") if isinstance(after.get("state"), dict) else None
    if state is None and isinstance(before.get("state"), dict):
        state = before.get("state")
    return {"tenant": tenant, "tick": tick, "state": state}


def _member_state_from_world(
    tenant: str, world: Mapping[str, Any], now_ms: int
) -> AllianceMemberState | None:
    """Compressed member state from a world snapshot (TS ``memberStateFromWorld``)."""
    state = world.get("state")
    if not isinstance(state, dict):
        return None
    objects = state.get("objects")
    if not isinstance(objects, list):
        return None
    core: CoreRef | None = None
    workers = vanguards = rangers = 0
    carried = 0
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        kind = obj.get("kind")
        if kind == "CORE" and obj.get("controlled") is True:
            position = _position_of(obj.get("position"))
            core = CoreRef(
                id=str(obj.get("id") or f"{tenant}-core"),
                position=position,
                hp=int(num(obj.get("hp"))),
                shield=int(num(obj.get("shield"))),
                moving=bool(obj.get("moving")),
            )
        elif kind == "UNIT" and obj.get("controlled") is True:
            unit_type = _as_unit_type(obj.get("unit_type"))
            if unit_type is UnitType.WORKER:
                workers += 1
            elif unit_type is UnitType.VANGUARD:
                vanguards += 1
            elif unit_type is UnitType.RANGER:
                rangers += 1
            carried += int(num(obj.get("cargo")))
    return AllianceMemberState(
        tenant_id=TenantId(tenant),
        tick=int(num(world.get("tick"))),
        observed_at_ms=now_ms,
        core=core,
        resources=int(num(state.get("resources"))),
        resource_capacity=int(num(state.get("resource_capacity"))),
        population=int(num(state.get("population"))),
        workers=workers,
        vanguards=vanguards,
        rangers=rangers,
        carried_resources=carried,
        active_fleet_ids=(),
        local_threat=0.0,
        local_harvest_rate=0.0,
        status=MemberStatus.READY if state.get("status") == "ACTIVE" else MemberStatus.DEGRADED,
    )


def _core_observations_from_survey(data_root: Path, tenant: str) -> list[AllianceObservation]:
    """Survey-db enemy cores as CALIBRATION observations (TS coreObservationsFromSurvey)."""
    path = survey_db_path(data_root, tenant)
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(
            "SELECT x, y, last_seen_tick, owner FROM core_hunts ORDER BY last_seen_tick DESC"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    observations: list[AllianceObservation] = []
    for row in rows:
        owner = row[3]
        observations.append(
            AllianceObservation(
                tenant_id=TenantId(tenant),
                tick=int(num(row[2])),
                kind=SightingKind.CORE,
                owner_username=str(owner) if owner is not None else None,
                controlled=False,
                position=Coordinate(int(num(row[0])), int(num(row[1]))),
                evidence=EvidenceKind.CALIBRATION,
            )
        )
    return observations


def _live_enemy_observations_from_world(
    tenant: str, world: Mapping[str, Any]
) -> list[AllianceObservation]:
    """Visible enemy CORE/UNIT objects as LIVE observations (TS port)."""
    state = world.get("state")
    if not isinstance(state, dict):
        return []
    objects = state.get("objects")
    if not isinstance(objects, list):
        return []
    tick = int(num(world.get("tick")))
    observations: list[AllianceObservation] = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        kind = obj.get("kind")
        if kind == "CORE" and obj.get("controlled") is not True:
            entity_id = obj.get("id")
            observations.append(
                AllianceObservation(
                    tenant_id=TenantId(tenant),
                    tick=tick,
                    kind=SightingKind.CORE,
                    entity_id=str(entity_id) if isinstance(entity_id, str) and entity_id else None,
                    owner_username=_non_empty_str(obj.get("owner_username")),
                    controlled=False,
                    position=_position_of(obj.get("position")),
                    evidence=EvidenceKind.LIVE,
                )
            )
        elif kind == "UNIT" and obj.get("controlled") is not True:
            entity_id = obj.get("id")
            observations.append(
                AllianceObservation(
                    tenant_id=TenantId(tenant),
                    tick=tick,
                    kind=SightingKind.UNIT,
                    entity_id=str(entity_id) if isinstance(entity_id, str) and entity_id else None,
                    unit_type=_as_unit_type(obj.get("unit_type")),
                    controlled=False,
                    position=_position_of(obj.get("position")),
                    evidence=EvidenceKind.LIVE,
                )
            )
    return observations


def _non_empty_str(value: object) -> str | None:
    if isinstance(value, str) and value != "":
        return value
    return None


def _ally_entity_ids_from_worlds(worlds: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Controlled object id union across tenants (TS allyEntityIdsFromWorlds)."""
    ids: list[str] = []
    for world in worlds.values():
        state = world.get("state")
        if not isinstance(state, dict):
            continue
        objects = state.get("objects")
        if not isinstance(objects, list):
            continue
        for obj in objects:
            if (
                isinstance(obj, dict)
                and obj.get("controlled") is True
                and isinstance(obj.get("id"), str)
                and obj.get("id") != ""
            ):
                ids.append(str(obj["id"]))
    return ids


def _leaderboard_aggression(data_root: Path) -> dict[str, float]:
    """Threat prior from the newest leaderboard snapshot (fail-open empty)."""
    directory = data_root / "leaderboard"
    if not directory.is_dir():
        return {}
    files = sorted(name for name in os.listdir(directory) if _LEADERBOARD_SNAPSHOT_RE.match(name))
    if not files:
        return {}
    path = directory / files[-1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    aggression: dict[str, float] = {}
    for row in raw.get("damage_dealt") or ():
        if not isinstance(row, dict):
            continue
        username = row.get("username")
        if not isinstance(username, str) or not username:
            continue
        rank = int(num(row.get("rank")))
        if rank >= 1 and rank <= 10:
            aggression[username] = 0.9
        elif rank <= 30:
            aggression[username] = 0.6
        else:
            aggression[username] = 0.2
    return aggression


def _treasury_of(members: Sequence[AllianceMemberState]) -> TenantId | None:
    """Treasury = highest-resource member (TS treasuryOf; None when no members)."""
    best: TenantId | None = None
    best_resources = -1
    for member in members:
        if member.resources > best_resources:
            best_resources = member.resources
            best = member.tenant_id
    return best


def load_alliance_snapshot(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load the ``/api/alliance/snapshot`` payload from the P5-3 data base."""
    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    worlds: dict[str, dict[str, Any]] = {}
    current_tick = 0
    for tenant in TENANTS:
        world = _load_world(root, tenant)
        worlds[tenant] = world
        tick = int(num(world.get("tick")))
        if tick > current_tick:
            current_tick = tick
    members: list[AllianceMemberState] = []
    for tenant in TENANTS:
        member = _member_state_from_world(tenant, worlds[tenant], now)
        if member is not None:
            members.append(member)
    observations: list[AllianceObservation] = []
    for tenant in TENANTS:
        observations.extend(_core_observations_from_survey(root, tenant))
        observations.extend(_live_enemy_observations_from_world(tenant, worlds[tenant]))
    ally_ids = _ally_entity_ids_from_worlds(worlds)
    aggression = _leaderboard_aggression(root)
    sightings = observations_to_sightings(observations, current_tick)
    sightings = tuple(
        sighting
        if sighting.last_seen_tick >= current_tick
        else _with_decayed_confidence(sighting, current_tick)
        for sighting in sightings
    )
    payload = build_alliance_snapshot_payload(
        revision=1,
        members=members,
        sightings=sightings,
        ally_entity_ids=ally_ids,
        now_tick=current_tick,
        generated_at_ms=now,
        leaderboard_aggression=aggression,
        treasury_tenant=_treasury_of(members),
    )
    payload["generatedAt"] = iso_utc(now)
    payload["cachedAt"] = iso_utc(now)
    return payload


def _with_decayed_confidence(sighting: EntitySighting, now_tick: int) -> EntitySighting:
    """Rebuild a sighting with age-decayed confidence (TS currentConfidence)."""
    import dataclasses

    return dataclasses.replace(sighting, confidence=current_confidence(sighting, now_tick))
