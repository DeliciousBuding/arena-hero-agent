"""Migration plan data model (migration-system-v1 §6.1).

The plan is an immutable (frozen) record mirroring the ``migration-plan-v1``
schema: operation identity, conductor epoch, core generation, lease, target,
path, legs, pacing, roles, and conductor metadata. Parsing is strict and
fail-closed: any missing, mistyped, or unknown field value rejects the whole
plan instead of being partially adopted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeGuard, cast

from .state_machine import MigrationState

PLAN_SCHEMA = "migration-plan-v1"

_MODE_VALUES = ("migrate", "receive")
_PACE_POLICIES = ("adaptive", "time-based", "harvest-driven")
_CLEAR_REASONS = ("initial", "blocked-retry", "replan")
_MISSING_ROLES = ("SC", "SW", "ES", "RG")


@dataclass(frozen=True, slots=True)
class MigrationPosition:
    """Signed 32-bit map coordinate."""

    x: int
    y: int


@dataclass(frozen=True, slots=True)
class MigrationAuditResult:
    """Corridor audit outcome recorded for one leg."""

    ok: bool
    fresh_resources: int
    active_enemy_cores: int


@dataclass(frozen=True, slots=True)
class MigrationLeg:
    """One audit-granularity path segment."""

    index: int
    from_pos: MigrationPosition
    to: MigrationPosition
    audit: MigrationAuditResult


@dataclass(frozen=True, slots=True)
class MigrationPace:
    """Pacing window policy (§3)."""

    policy: Literal["adaptive", "time-based", "harvest-driven"]
    burst_cells: int
    settle_target: int
    min_settle: int
    max_settle: int
    harvest_radius: int


@dataclass(frozen=True, slots=True)
class MigrationRoleQuotas:
    """Squad role budget percentages."""

    escort: int
    sweep: int
    scout: int
    rear: int


@dataclass(frozen=True, slots=True)
class MigrationRoles:
    """Role quotas plus the deterministic sticky-assignment seed."""

    quotas: MigrationRoleQuotas
    seed: int


@dataclass(frozen=True, slots=True)
class MigrationCoreIdentity:
    """Core generation identity; a destroyed/respawned core changes ids."""

    origin_core_id: str | None
    current_core_id: str | None
    generation: int


@dataclass(frozen=True, slots=True)
class MigrationLease:
    """Plan lease: effective-until game tick plus conductor heartbeat."""

    until_tick: int
    heartbeat_at: str


@dataclass(frozen=True, slots=True)
class MigrationTarget:
    """Destination position and the recorded reason."""

    x: int
    y: int
    reason: str


@dataclass(frozen=True, slots=True)
class MigrationPath:
    """Actual path cells plus corridor auditing parameters."""

    cells: tuple[tuple[int, int], ...]
    corridor_width: int
    lookahead: int


@dataclass(frozen=True, slots=True)
class MigrationLegProgress:
    """Resume bookmark within the current operation."""

    leg_index: int
    cells_this_leg: int


@dataclass(frozen=True, slots=True)
class MigrationConductorMeta:
    """Issuing conductor process metadata."""

    pid: int


@dataclass(frozen=True, slots=True)
class MigrationClearRequest:
    """M6: cells to clear before the next START_MOVE (at most three)."""

    x: int
    y: int
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MigrationAssist:
    """M6: clear-ahead assist configuration."""

    clear_ahead_cells: int
    clear_ahead_reason: Literal["initial", "blocked-retry", "replan"]


@dataclass(frozen=True, slots=True)
class MigrationReplenish:
    """M8: formation gap replenishment request."""

    gap: int
    missing_role: Literal["SC", "SW", "ES", "RG"]
    since_tick: int


@dataclass(frozen=True, slots=True)
class MigrationPlanV1:
    """Immutable migration plan aligned with the §6.1 schema."""

    schema: Literal["migration-plan-v1"]
    operation_id: str
    revision: int
    conductor_epoch: int
    tenant: str
    mode: Literal["migrate", "receive"]
    state: MigrationState
    core: MigrationCoreIdentity
    lease: MigrationLease
    target: MigrationTarget
    path: MigrationPath
    legs: tuple[MigrationLeg, ...]
    leg_progress: MigrationLegProgress
    pace: MigrationPace
    roles: MigrationRoles
    conductor: MigrationConductorMeta
    updated_at: str
    clear_requests: tuple[MigrationClearRequest, ...] | None = None
    assist: MigrationAssist | None = None
    replenish: MigrationReplenish | None = None

    def to_json_object(self) -> dict[str, object]:
        """Serialize to the §6.1 camelCase JSON shape (optional fields omitted)."""
        payload: dict[str, object] = {
            "schema": self.schema,
            "operationId": self.operation_id,
            "revision": self.revision,
            "conductorEpoch": self.conductor_epoch,
            "tenant": self.tenant,
            "mode": self.mode,
            "state": self.state.value,
            "core": {
                "originCoreId": self.core.origin_core_id,
                "currentCoreId": self.core.current_core_id,
                "generation": self.core.generation,
            },
            "lease": {
                "untilTick": self.lease.until_tick,
                "heartbeatAt": self.lease.heartbeat_at,
            },
            "target": {"x": self.target.x, "y": self.target.y, "reason": self.target.reason},
            "path": {
                "cells": [[x, y] for x, y in self.path.cells],
                "corridorWidth": self.path.corridor_width,
                "lookahead": self.path.lookahead,
            },
            "legs": [
                {
                    "index": leg.index,
                    "from": {"x": leg.from_pos.x, "y": leg.from_pos.y},
                    "to": {"x": leg.to.x, "y": leg.to.y},
                    "audit": {
                        "ok": leg.audit.ok,
                        "freshResources": leg.audit.fresh_resources,
                        "activeEnemyCores": leg.audit.active_enemy_cores,
                    },
                }
                for leg in self.legs
            ],
            "legProgress": {
                "legIndex": self.leg_progress.leg_index,
                "cellsThisLeg": self.leg_progress.cells_this_leg,
            },
            "pace": {
                "policy": self.pace.policy,
                "burstCells": self.pace.burst_cells,
                "settleTarget": self.pace.settle_target,
                "minSettle": self.pace.min_settle,
                "maxSettle": self.pace.max_settle,
                "harvestRadius": self.pace.harvest_radius,
            },
            "roles": {
                "quotas": {
                    "escort": self.roles.quotas.escort,
                    "sweep": self.roles.quotas.sweep,
                    "scout": self.roles.quotas.scout,
                    "rear": self.roles.quotas.rear,
                },
                "seed": self.roles.seed,
            },
            "conductor": {"pid": self.conductor.pid},
            "updatedAt": self.updated_at,
        }
        if self.clear_requests is not None:
            payload["clearRequests"] = [
                {
                    "x": request.x,
                    "y": request.y,
                    **({"reason": request.reason} if request.reason is not None else {}),
                }
                for request in self.clear_requests
            ]
        if self.assist is not None:
            payload["assist"] = {
                "clearAheadCells": self.assist.clear_ahead_cells,
                "clearAheadReason": self.assist.clear_ahead_reason,
            }
        if self.replenish is not None:
            payload["replenish"] = {
                "gap": self.replenish.gap,
                "missingRole": self.replenish.missing_role,
                "sinceTick": self.replenish.since_tick,
            }
        return payload


@dataclass(frozen=True, slots=True)
class MigrationPlanParseResult:
    """Strict parse outcome; ``ok=False`` carries a rejection reason."""

    ok: bool
    plan: MigrationPlanV1 | None = None
    reason: str | None = None


def _is_record(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: object) -> TypeGuard[str]:
    return isinstance(value, str)


def _is_iso_timestamp(value: object) -> TypeGuard[str]:
    if not _is_str(value):
        return False
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


def _position(raw: object) -> MigrationPosition | None:
    if not _is_record(raw):
        return None
    x, y = raw.get("x"), raw.get("y")
    if not _is_int(x) or not _is_int(y):
        return None
    return MigrationPosition(x=x, y=y)


def _reject(raw: object, reason: str) -> MigrationPlanParseResult:
    return MigrationPlanParseResult(ok=False, reason=reason)


def parse_migration_plan(raw: object) -> MigrationPlanParseResult:
    """Strictly validate and build a plan; any defect rejects the whole plan."""
    if not _is_record(raw):
        return _reject(raw, "plan must be a JSON object")
    if raw.get("schema") != PLAN_SCHEMA:
        return _reject(raw, f"unrecognized schema: {raw.get('schema')!r}")

    operation_id = raw.get("operationId")
    if not _is_str(operation_id) or not operation_id:
        return _reject(raw, "operationId is missing or empty")
    revision = raw.get("revision")
    if not _is_int(revision) or revision < 1:
        return _reject(raw, "revision must be an integer >= 1")
    conductor_epoch = raw.get("conductorEpoch")
    if not _is_int(conductor_epoch) or conductor_epoch < 0:
        return _reject(raw, "conductorEpoch must be an integer >= 0")
    tenant = raw.get("tenant")
    if not _is_str(tenant) or not tenant:
        return _reject(raw, "tenant is missing or empty")
    mode = raw.get("mode")
    if mode not in _MODE_VALUES:
        return _reject(raw, f"invalid mode: {mode!r}")

    state_value = raw.get("state")
    if not _is_str(state_value) or state_value not in MigrationState:
        return _reject(raw, f"invalid state: {state_value!r}")

    core = raw.get("core")
    if not _is_record(core):
        return _reject(raw, "core section is missing")
    origin_core_id = core.get("originCoreId")
    current_core_id = core.get("currentCoreId")
    if origin_core_id is not None and not _is_str(origin_core_id):
        return _reject(raw, "core.originCoreId must be a string or null")
    if current_core_id is not None and not _is_str(current_core_id):
        return _reject(raw, "core.currentCoreId must be a string or null")
    generation = core.get("generation")
    if not _is_int(generation) or generation < 0:
        return _reject(raw, "core.generation must be an integer >= 0")

    lease = raw.get("lease")
    if not _is_record(lease):
        return _reject(raw, "lease section is missing")
    until_tick = lease.get("untilTick")
    if not _is_int(until_tick):
        return _reject(raw, "lease.untilTick must be an integer")
    heartbeat_at = lease.get("heartbeatAt")
    if not _is_iso_timestamp(heartbeat_at):
        return _reject(raw, "lease.heartbeatAt must be an ISO-8601 timestamp")

    target_raw = raw.get("target")
    if not _is_record(target_raw):
        return _reject(raw, "target section is missing")
    target_pos = _position(target_raw)
    target_reason = target_raw.get("reason")
    if target_pos is None or not _is_str(target_reason):
        return _reject(raw, "target must be a position with a reason")

    path = raw.get("path")
    if not _is_record(path):
        return _reject(raw, "path section is missing")
    cells_raw = path.get("cells")
    if not isinstance(cells_raw, list):
        return _reject(raw, "path.cells must be an array")
    cells: list[tuple[int, int]] = []
    for cell in cells_raw:
        if not isinstance(cell, list) or len(cell) != 2:
            return _reject(raw, "path.cells entries must be [x, y] pairs")
        if not _is_int(cell[0]) or not _is_int(cell[1]):
            return _reject(raw, "path.cells entries must be integer [x, y] pairs")
        cells.append((cell[0], cell[1]))
    corridor_width = path.get("corridorWidth")
    if not _is_int(corridor_width) or corridor_width < 0:
        return _reject(raw, "path.corridorWidth must be an integer >= 0")
    lookahead = path.get("lookahead")
    if not _is_int(lookahead) or lookahead < 0:
        return _reject(raw, "path.lookahead must be an integer >= 0")

    legs_raw = raw.get("legs")
    if not isinstance(legs_raw, list):
        return _reject(raw, "legs must be an array")
    legs: list[MigrationLeg] = []
    for leg in legs_raw:
        if not _is_record(leg):
            return _reject(raw, "legs entries must be objects")
        leg_index = leg.get("index")
        from_pos = _position(leg.get("from"))
        to_pos = _position(leg.get("to"))
        audit = leg.get("audit")
        if not _is_int(leg_index) or from_pos is None or to_pos is None or not _is_record(audit):
            return _reject(raw, "legs entries require index/from/to/audit")
        audit_ok = audit.get("ok")
        fresh_resources = audit.get("freshResources")
        active_enemy_cores = audit.get("activeEnemyCores")
        if (
            not isinstance(audit_ok, bool)
            or not _is_int(fresh_resources)
            or not _is_int(active_enemy_cores)
        ):
            return _reject(raw, "legs audit result is invalid")
        legs.append(
            MigrationLeg(
                index=leg_index,
                from_pos=from_pos,
                to=to_pos,
                audit=MigrationAuditResult(
                    ok=audit_ok,
                    fresh_resources=fresh_resources,
                    active_enemy_cores=active_enemy_cores,
                ),
            )
        )

    leg_progress = raw.get("legProgress")
    if not _is_record(leg_progress):
        return _reject(raw, "legProgress section is missing")
    leg_index = leg_progress.get("legIndex")
    cells_this_leg = leg_progress.get("cellsThisLeg")
    if not _is_int(leg_index) or not _is_int(cells_this_leg):
        return _reject(raw, "legProgress.legIndex/cellsThisLeg must be integers")

    pace = raw.get("pace")
    if not _is_record(pace):
        return _reject(raw, "pace section is missing")
    policy = pace.get("policy")
    if policy not in _PACE_POLICIES:
        return _reject(raw, f"invalid pace.policy: {policy!r}")
    burst_cells = pace.get("burstCells")
    settle_target = pace.get("settleTarget")
    min_settle = pace.get("minSettle")
    max_settle = pace.get("maxSettle")
    harvest_radius = pace.get("harvestRadius")
    if not _is_int(burst_cells) or burst_cells < 1:
        return _reject(raw, "pace.burstCells must be an integer >= 1")
    if not _is_int(settle_target) or settle_target < 0:
        return _reject(raw, "pace.settleTarget must be an integer >= 0")
    if not _is_int(min_settle) or min_settle < 0:
        return _reject(raw, "pace.minSettle must be an integer >= 0")
    if not _is_int(max_settle) or max_settle < 0:
        return _reject(raw, "pace.maxSettle must be an integer >= 0")
    if not _is_int(harvest_radius) or harvest_radius < 0:
        return _reject(raw, "pace.harvestRadius must be an integer >= 0")

    roles = raw.get("roles")
    if not _is_record(roles):
        return _reject(raw, "roles section is missing")
    quotas = roles.get("quotas")
    seed = roles.get("seed")
    if not _is_record(quotas) or not _is_int(seed):
        return _reject(raw, "roles.quotas/seed are missing")
    quota_values: dict[str, int] = {}
    for key in ("escort", "sweep", "scout", "rear"):
        value = quotas.get(key)
        if not _is_int(value) or value < 0:
            return _reject(raw, f"roles.quotas.{key} must be an integer >= 0")
        quota_values[key] = value

    conductor = raw.get("conductor")
    if not _is_record(conductor):
        return _reject(raw, "conductor section is missing")
    pid = conductor.get("pid")
    if not _is_int(pid):
        return _reject(raw, "conductor.pid must be an integer")

    updated_at = raw.get("updatedAt")
    if not _is_iso_timestamp(updated_at):
        return _reject(raw, "updatedAt must be an ISO-8601 timestamp")

    clear_requests: tuple[MigrationClearRequest, ...] | None = None
    if "clearRequests" in raw:
        requests_raw = raw.get("clearRequests")
        if not isinstance(requests_raw, list) or len(requests_raw) > 3:
            return _reject(raw, "clearRequests must be an array of at most 3 cells")
        parsed_requests: list[MigrationClearRequest] = []
        for request in requests_raw:
            if not _is_record(request):
                return _reject(raw, "clearRequests entries must be objects")
            position = _position(request)
            request_reason = request.get("reason")
            if position is None:
                return _reject(raw, "clearRequests entries must be positions")
            if request_reason is not None and not _is_str(request_reason):
                return _reject(raw, "clearRequests.reason must be a string or null")
            parsed_requests.append(
                MigrationClearRequest(x=position.x, y=position.y, reason=request_reason)
            )
        clear_requests = tuple(parsed_requests)

    assist: MigrationAssist | None = None
    if "assist" in raw:
        assist_raw = raw.get("assist")
        if not _is_record(assist_raw):
            return _reject(raw, "assist section must be an object")
        clear_ahead_cells = assist_raw.get("clearAheadCells")
        clear_ahead_reason = assist_raw.get("clearAheadReason")
        if not _is_int(clear_ahead_cells) or clear_ahead_cells < 1:
            return _reject(raw, "assist.clearAheadCells must be an integer >= 1")
        if clear_ahead_reason not in _CLEAR_REASONS:
            return _reject(raw, f"invalid assist.clearAheadReason: {clear_ahead_reason!r}")
        assist = MigrationAssist(
            clear_ahead_cells=clear_ahead_cells,
            clear_ahead_reason=cast(
                Literal["initial", "blocked-retry", "replan"], clear_ahead_reason
            ),
        )

    replenish: MigrationReplenish | None = None
    if "replenish" in raw:
        replenish_raw = raw.get("replenish")
        if not _is_record(replenish_raw):
            return _reject(raw, "replenish section must be an object")
        gap = replenish_raw.get("gap")
        missing_role = replenish_raw.get("missingRole")
        since_tick = replenish_raw.get("sinceTick")
        if not _is_int(gap) or gap < 1:
            return _reject(raw, "replenish.gap must be an integer >= 1")
        if missing_role not in _MISSING_ROLES:
            return _reject(raw, f"invalid replenish.missingRole: {missing_role!r}")
        if not _is_int(since_tick):
            return _reject(raw, "replenish.sinceTick must be an integer")
        replenish = MigrationReplenish(
            gap=gap,
            missing_role=cast(Literal["SC", "SW", "ES", "RG"], missing_role),
            since_tick=since_tick,
        )

    plan = MigrationPlanV1(
        schema=PLAN_SCHEMA,
        operation_id=operation_id,
        revision=revision,
        conductor_epoch=conductor_epoch,
        tenant=tenant,
        mode=cast(Literal["migrate", "receive"], mode),
        state=MigrationState(state_value),
        core=MigrationCoreIdentity(
            origin_core_id=origin_core_id,
            current_core_id=current_core_id,
            generation=generation,
        ),
        lease=MigrationLease(until_tick=until_tick, heartbeat_at=heartbeat_at),
        target=MigrationTarget(x=target_pos.x, y=target_pos.y, reason=target_reason),
        path=MigrationPath(
            cells=tuple(cells),
            corridor_width=corridor_width,
            lookahead=lookahead,
        ),
        legs=tuple(legs),
        leg_progress=MigrationLegProgress(leg_index=leg_index, cells_this_leg=cells_this_leg),
        pace=MigrationPace(
            policy=cast(Literal["adaptive", "time-based", "harvest-driven"], policy),
            burst_cells=burst_cells,
            settle_target=settle_target,
            min_settle=min_settle,
            max_settle=max_settle,
            harvest_radius=harvest_radius,
        ),
        roles=MigrationRoles(
            quotas=MigrationRoleQuotas(
                escort=quota_values["escort"],
                sweep=quota_values["sweep"],
                scout=quota_values["scout"],
                rear=quota_values["rear"],
            ),
            seed=seed,
        ),
        conductor=MigrationConductorMeta(pid=pid),
        updated_at=updated_at,
        clear_requests=clear_requests,
        assist=assist,
        replenish=replenish,
    )
    return MigrationPlanParseResult(ok=True, plan=plan)


__all__ = [
    "MigrationAssist",
    "MigrationAuditResult",
    "MigrationClearRequest",
    "MigrationConductorMeta",
    "MigrationCoreIdentity",
    "MigrationLease",
    "MigrationLeg",
    "MigrationLegProgress",
    "MigrationPace",
    "MigrationPath",
    "MigrationPlanParseResult",
    "MigrationPlanV1",
    "MigrationPosition",
    "MigrationReplenish",
    "MigrationRoleQuotas",
    "MigrationRoles",
    "MigrationTarget",
    "PLAN_SCHEMA",
    "parse_migration_plan",
]
