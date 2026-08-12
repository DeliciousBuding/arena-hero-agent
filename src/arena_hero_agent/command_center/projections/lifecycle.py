"""Unit/mine/core lifecycle audit (port of legacy ``lifecycle-audit.ts``).

Ports ``aggregateLifecycle`` / ``loadLifecycleAudit`` from the TypeScript
oracle: aggregate the latest calibration run's case events (``after.state
.events``) into per-actor unit lifecycles, per-cell mine lifecycles, the core
lifecycle, and a consumption summary, then enrich from the survey database
(``unit_lifecycle`` unit types / cross-run birth-death, ``core_spends`` core
consumption, ``notable_events`` core capture/damage/destroyed). Pure read;
``/api/audit/lifecycle`` (``?tenant=all|tN``, default all).

Registered differences from the TS oracle:

- ``generatedAt``/``cachedAt`` are injectable via ``now_ms`` (TS
  ``new Date().toISOString()``).
- No in-memory 30 s TTL cache: every call reads fresh (cache behavior only).
- The TS route's invalid-tenant 400 is enforced by the request pipeline's
  fail-closed tenant validation.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import calibration_dir, latest_run_dir, list_cases, parse_tick
from ..paths import TENANTS, survey_db_path, validate_data_root
from ._common import current_epoch_ms, finite_number, num

__all__ = ["MAX_CASES", "MAX_POSITION_SAMPLES", "aggregate_lifecycle", "load_lifecycle_audit"]

MAX_CASES = 500
MAX_POSITION_SAMPLES = 24

# TS ``CORE_KINDS``: event kinds routed to the core lifecycle instead of units.
CORE_KINDS: frozenset[str] = frozenset(
    {
        "CORE_DAMAGED",
        "CORE_DESTROYED",
        "CORE_HEAL_FAILED",
        "CORE_HEAL_SUCCEEDED",
        "CORE_MOVE_FAILED",
        "CORE_MOVE_STARTED",
        "CORE_MOVE_START_FAILED",
        "CORE_MOVE_PROGRESS",
        "CORE_MOVE_SUCCEEDED",
        "CORE_RESOURCES_CAPTURED",
        "CORE_RESOURCE_OVERFLOW_DESTROYED",
        "CORE_SPAWN_SUCCEEDED",
        "CORE_SPAWN_FAILED",
    }
)

_EMPTY_SPENDS = {"byKind": {}, "byType": {}, "total": 0}


def _js_round(value: float) -> int:
    """Mirror TS ``Math.round`` (half away from zero)."""
    import math

    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _pair(value: object) -> list[int] | None:
    """TS ``pair``: a finite ``[x, y]`` from a 2+ element sequence, else None."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    x = finite_number(value[0])
    y = finite_number(value[1])
    if x is None or y is None:
        return None
    return [int(x), int(y)]


def _empty_payload(tenant: str, at: str, run_id: str | None) -> dict[str, Any]:
    return {
        "generatedAt": at,
        "tenant": tenant,
        "runId": run_id,
        "window": {"fromTick": None, "toTick": None, "cases": 0, "events": 0},
        "units": [],
        "mines": [],
        "core": None,
        "consumption": {
            "harvestOk": 0,
            "harvestFail": 0,
            "harvestAmount": 0,
            "depositOk": 0,
            "depositFail": 0,
            "depositAmount": 0,
            "cargoDropped": 0,
            "spawns": 0,
            "respawns": 0,
            "unitDestroyed": 0,
            "selfDestructs": 0,
            "destroyedByEnemy": 0,
            "coreDamageTaken": 0,
            "spends": {"byKind": {}, "byType": {}, "total": 0},
        },
        "cachedAt": at,
    }


def _new_core() -> dict[str, Any]:
    """A fresh core-lifecycle accumulator (TS ``CoreLifecycle`` default)."""
    return {
        "actor": None,
        "damageTaken": 0,
        "damageEvents": 0,
        "healOk": 0,
        "healFail": 0,
        "moveOk": 0,
        "moveFail": 0,
        "capturedResources": 0,
        "captures": {"count": 0, "amount": 0},
        "destroyed": False,
        "destroyedAtTick": None,
        "destroyedBy": None,
        "lastPosition": None,
        "positionSamples": [],
    }


def _normalize_event(ev: dict[str, Any], file_tick: int) -> dict[str, Any] | None:
    """One raw calibration event -> normalized lifecycle event (TS parity)."""
    kind = str(ev.get("event_type") if ev.get("event_type") is not None else "").upper()
    if kind == "":
        return None
    values = ev.get("values")
    if not isinstance(values, dict):
        values = {}
    amount = finite_number(values.get("amount"))
    if amount is None:
        amount = finite_number(values.get("damage"))
    # TS ``num(x) || null``: zero/non-numeric coerces to null.
    amount = None if amount is None or amount == 0 else amount
    hp = finite_number(values.get("hp"))
    hp = None if hp is None or hp == 0 else hp
    capacity = finite_number(values.get("capacity"))
    capacity = None if capacity is None or capacity == 0 else capacity
    tick = finite_number(ev.get("tick"))
    tick = file_tick if tick is None or tick == 0 else int(tick)
    return {
        "tick": tick,
        "kind": kind,
        "actor": ev.get("actor_id") if ev.get("actor_id") is not None else None,
        "target": ev.get("target_id") if ev.get("target_id") is not None else None,
        "reason": ev.get("reason_code") if ev.get("reason_code") is not None else None,
        "position": _pair(ev.get("position")),
        "amount": amount,
        "hp": hp,
        "source": values.get("source") if values.get("source") is not None else None,
        "capacity": capacity,
        "destroyedBy": (
            values.get("destroyed_by") if values.get("destroyed_by") is not None else None
        ),
        "destination": _pair(values.get("destination")),
    }


def _sample(samples: list[dict[str, Any]], tick: int, position: list[int]) -> None:
    """TS ``sample``: append {tick, position} (same-tick dedup) + cap length."""
    if not samples or samples[-1]["tick"] != tick:
        samples.append({"tick": tick, "position": position})
        if len(samples) > MAX_POSITION_SAMPLES:
            del samples[: len(samples) - MAX_POSITION_SAMPLES]


def aggregate_lifecycle(
    tenant: str, run_id: str | None, events: list[dict[str, Any]], *, now_ms: int | None = None
) -> dict[str, Any]:
    """Pure port of TS ``aggregateLifecycle`` (events pre-normalized, tick asc)."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    units: dict[str, dict[str, Any]] = {}
    mines: dict[str, dict[str, Any]] = {}
    core = _new_core()
    cons: dict[str, Any] = {
        "harvestOk": 0,
        "harvestFail": 0,
        "harvestAmount": 0,
        "depositOk": 0,
        "depositFail": 0,
        "depositAmount": 0,
        "cargoDropped": 0,
        "spawns": 0,
        "respawns": 0,
        "unitDestroyed": 0,
        "selfDestructs": 0,
        "destroyedByEnemy": 0,
        "coreDamageTaken": 0,
        "spends": {"byKind": {}, "byType": {}, "total": 0},
    }
    from_tick: int | None = None
    to_tick: int | None = None

    def unit(actor: str) -> dict[str, Any]:
        existing = units.get(actor)
        if existing is None:
            existing = {
                "actor": actor,
                "unitType": None,
                "role": "unit",
                "firstSeenTick": None,
                "lastSeenTick": None,
                "alive": True,
                "destroyedAtTick": None,
                "destroyedBy": None,
                "spawned": False,
                "moves": {"ok": 0, "fail": 0},
                "harvest": {"ok": 0, "fail": 0, "amount": 0},
                "deposit": {"ok": 0, "fail": 0, "amount": 0},
                "combat": {
                    "shotsHit": 0,
                    "shotsMissed": 0,
                    "blocked": 0,
                    "sweepsResolved": 0,
                    "damageDealt": 0,
                },
                "heals": {"ok": 0, "fail": 0},
                "drops": 0,
                "pickups": 0,
                "lastPosition": None,
                "positionSamples": [],
            }
            units[actor] = existing
        return existing

    def mine_at(position: list[int]) -> dict[str, Any]:
        key = f"{position[0]},{position[1]}"
        existing = mines.get(key)
        if existing is None:
            existing = {
                "cell": key,
                "x": position[0],
                "y": position[1],
                "firstSeenTick": None,
                "lastSeenTick": None,
                "harvestCount": 0,
                "harvestAmount": 0,
                "harvestFailCount": 0,
                "active": True,
                "refillGapTicks": None,
            }
            mines[key] = existing
        return existing

    for ev in events:
        if from_tick is None or ev["tick"] < from_tick:
            from_tick = ev["tick"]
        if to_tick is None or ev["tick"] > to_tick:
            to_tick = ev["tick"]
        kind = ev["kind"]
        amount = ev["amount"] if ev["amount"] is not None else 0
        is_core = kind in CORE_KINDS or (ev["actor"] is not None and ev["actor"] == core["actor"])

        if is_core:
            core["actor"] = ev["actor"] if ev["actor"] is not None else core["actor"]
            core["lastPosition"] = (
                ev["position"] if ev["position"] is not None else core["lastPosition"]
            )
            if ev["position"] is not None and ev["actor"] is not None:
                _sample(core["positionSamples"], ev["tick"], ev["position"])
            if kind == "CORE_DAMAGED":
                core["damageTaken"] += amount
                core["damageEvents"] += 1
                cons["coreDamageTaken"] += amount
            elif kind == "CORE_HEAL_SUCCEEDED":
                core["healOk"] += 1
            elif kind == "CORE_HEAL_FAILED":
                core["healFail"] += 1
            elif kind == "CORE_MOVE_SUCCEEDED":
                core["moveOk"] += 1
            elif kind in ("CORE_MOVE_FAILED", "CORE_MOVE_START_FAILED"):
                core["moveFail"] += 1
            elif kind == "CORE_RESOURCES_CAPTURED":
                core["capturedResources"] += amount
                core["captures"]["count"] += 1
                core["captures"]["amount"] += amount
            elif kind == "CORE_DESTROYED":
                core["destroyed"] = True
                core["destroyedAtTick"] = ev["tick"]
                core["destroyedBy"] = (
                    ev["destroyedBy"] if ev["destroyedBy"] is not None else ev["source"]
                )
                if core["destroyedBy"] is not None:
                    cons["destroyedByEnemy"] += 1
            elif kind == "CORE_RESOURCE_OVERFLOW_DESTROYED":
                core["destroyed"] = True
                core["destroyedAtTick"] = ev["tick"]
            elif kind == "CORE_SPAWN_SUCCEEDED":
                cons["spawns"] += 1
            continue

        if ev["actor"] is None:
            continue
        u = unit(ev["actor"])
        if u["firstSeenTick"] is None or ev["tick"] < u["firstSeenTick"]:
            u["firstSeenTick"] = ev["tick"]
        if u["lastSeenTick"] is None or ev["tick"] > u["lastSeenTick"]:
            u["lastSeenTick"] = ev["tick"]
        u["lastPosition"] = ev["position"] if ev["position"] is not None else u["lastPosition"]
        if ev["position"] is not None:
            _sample(u["positionSamples"], ev["tick"], ev["position"])

        if kind == "UNIT_MOVE_SUCCEEDED":
            u["moves"]["ok"] += 1
        elif kind == "UNIT_MOVE_FAILED":
            u["moves"]["fail"] += 1
        elif kind == "HARVEST_SUCCEEDED":
            u["harvest"]["ok"] += 1
            u["harvest"]["amount"] += amount
            cons["harvestOk"] += 1
            cons["harvestAmount"] += amount
            if ev["position"] is not None:
                m = mine_at(ev["position"])
                m["harvestCount"] += 1
                m["harvestAmount"] += amount
                if m["firstSeenTick"] is None:
                    m["firstSeenTick"] = ev["tick"]
                m["lastSeenTick"] = ev["tick"]
        elif kind == "HARVEST_FAILED":
            u["harvest"]["fail"] += 1
            cons["harvestFail"] += 1
            if ev["position"] is not None:
                m = mine_at(ev["position"])
                m["harvestFailCount"] += 1
                if m["firstSeenTick"] is None:
                    m["firstSeenTick"] = ev["tick"]
                m["lastSeenTick"] = ev["tick"]
        elif kind == "DEPOSIT_SUCCEEDED":
            u["deposit"]["ok"] += 1
            u["deposit"]["amount"] += amount
            cons["depositOk"] += 1
            cons["depositAmount"] += amount
        elif kind == "DEPOSIT_FAILED":
            u["deposit"]["fail"] += 1
            cons["depositFail"] += 1
        elif kind == "SHOT_HIT":
            u["combat"]["shotsHit"] += 1
            u["combat"]["damageDealt"] += amount
        elif kind == "SHOT_MISSED":
            u["combat"]["shotsMissed"] += 1
        elif kind == "SHOT_BLOCKED":
            u["combat"]["blocked"] += 1
        elif kind == "SWEEP_RESOLVED":
            u["combat"]["sweepsResolved"] += 1
        elif kind == "UNIT_HEAL_SUCCEEDED":
            u["heals"]["ok"] += 1
        elif kind == "UNIT_HEAL_FAILED":
            u["heals"]["fail"] += 1
        elif kind == "WORKER_CARGO_DROPPED":
            u["drops"] += 1
            cons["cargoDropped"] += 1
        elif kind == "PICKUP_BEACON_SUCCEEDED":
            u["pickups"] += 1
        elif kind == "RESPAWN":
            u["spawned"] = True
            u["alive"] = True
            u["destroyedAtTick"] = None
            cons["respawns"] += 1
        elif kind == "SPAWN_SUCCEEDED":
            u["spawned"] = True
            cons["spawns"] += 1
        elif kind == "UNIT_DESTROYED":
            u["alive"] = False
            u["destroyedAtTick"] = ev["tick"]
            u["destroyedBy"] = ev["destroyedBy"] if ev["destroyedBy"] is not None else ev["source"]
            cons["unitDestroyed"] += 1
            if u["destroyedBy"] is not None:
                cons["destroyedByEnemy"] += 1
        elif kind == "SELF_DESTRUCT":
            u["alive"] = False
            u["destroyedAtTick"] = ev["tick"]
            u["destroyedBy"] = "self"
            cons["selfDestructs"] += 1

    # Role classification (core already diverted): harvest/deposit -> worker,
    # combat activity -> combat, otherwise unit.
    for u in units.values():
        if (
            u["harvest"]["ok"] + u["harvest"]["fail"] + u["deposit"]["ok"] + u["deposit"]["fail"]
            > 0
        ):
            u["role"] = "worker"
        elif (
            u["combat"]["shotsHit"]
            + u["combat"]["shotsMissed"]
            + u["combat"]["blocked"]
            + u["combat"]["sweepsResolved"]
            > 0
        ):
            u["role"] = "combat"

    for m in mines.values():
        m["active"] = m["lastSeenTick"] is not None and (
            to_tick is None or m["lastSeenTick"] >= (to_tick or 0) - 5
        )
    for m in mines.values():
        if (
            m["harvestCount"] >= 2
            and m["firstSeenTick"] is not None
            and m["lastSeenTick"] is not None
            and m["lastSeenTick"] > m["firstSeenTick"]
        ):
            m["refillGapTicks"] = _js_round(
                (m["lastSeenTick"] - m["firstSeenTick"]) / (m["harvestCount"] - 1)
            )

    sorted_units = sorted(
        units.values(),
        key=lambda item: item["lastSeenTick"] if item["lastSeenTick"] is not None else -1,
        reverse=True,
    )
    sorted_mines = sorted(
        mines.values(),
        key=lambda item: item["lastSeenTick"] if item["lastSeenTick"] is not None else -1,
        reverse=True,
    )
    return {
        "generatedAt": at,
        "tenant": tenant,
        "runId": run_id,
        "window": {"fromTick": from_tick, "toTick": to_tick, "cases": 0, "events": len(events)},
        "units": sorted_units,
        "mines": sorted_mines,
        "core": core if core["actor"] is not None else None,
        "consumption": cons,
        "cachedAt": at,
    }


def _enrich_from_survey_db(connection: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """survey-db backfill: unit types/deaths, core spends, notable events."""
    units: Any = payload["units"]
    consumption: Any = payload["consumption"]
    rows = connection.execute(
        "SELECT unit_id AS id, unit_type AS type, birth_tick AS b, birth_pos AS bp,"
        " death_tick AS d, death_pos AS dp, death_reason AS dr, current_state AS st"
        " FROM unit_lifecycle"
    ).fetchall()
    by_id = {str(row[0]): row for row in rows}
    for u in units:
        rec = by_id.get(str(u["actor"]))
        if rec is None:
            continue
        u["unitType"] = rec[1] if rec[1] is not None else None
        if u["firstSeenTick"] is None and rec[2] is not None:
            u["firstSeenTick"] = num(rec[2])
        if rec[4] is not None:
            u["alive"] = False
            u["destroyedAtTick"] = num(rec[4])
            u["destroyedBy"] = rec[6] if rec[6] is not None else u["destroyedBy"]
        if u["lastSeenTick"] is None and rec[2] is not None:
            u["lastSeenTick"] = num(rec[2])
    spends = connection.execute(
        "SELECT kind AS k, amount AS a, unit_type AS t FROM core_spends"
    ).fetchall()
    for row in spends:
        amt = num(row[1])
        kind = str(row[0]) if row[0] is not None else "other"
        consumption["spends"]["byKind"][kind] = consumption["spends"]["byKind"].get(kind, 0) + amt
        unit_type = str(row[2]) if row[2] is not None else "unknown"
        consumption["spends"]["byType"][unit_type] = (
            consumption["spends"]["byType"].get(unit_type, 0) + amt
        )
        consumption["spends"]["total"] += amt
    notables = connection.execute(
        "SELECT event_type AS e, amount AS a FROM notable_events"
    ).fetchall()
    core_notables = [
        row
        for row in notables
        if row[0] in ("CORE_RESOURCES_CAPTURED", "CORE_DAMAGED", "CORE_DESTROYED")
    ]
    core: dict[str, Any] | None = payload["core"]
    if core_notables and core is None:
        core = _new_core()
        payload["core"] = core
    for row in core_notables:
        if core is None:
            continue
        event_type = row[0]
        if event_type == "CORE_RESOURCES_CAPTURED":
            core["captures"]["count"] += 1
            core["captures"]["amount"] += num(row[1])
            core["capturedResources"] += num(row[1])
        elif event_type == "CORE_DAMAGED":
            core["damageTaken"] += num(row[1])
            core["damageEvents"] += 1
        elif event_type == "CORE_DESTROYED":
            core["destroyed"] = True


def _audit_tenant(data_root: str | os.PathLike[str], tenant: str, now_ms: int) -> dict[str, Any]:
    """Read the latest run's case events, aggregate, then survey-db enrich."""
    root = validate_data_root(data_root)
    at = iso_utc(now_ms)
    run_dir = latest_run_dir(root, tenant)
    if run_dir is None:
        return _empty_payload(tenant, at, None)
    files = list_cases(root, tenant, run_dir)[-MAX_CASES:]
    events: list[dict[str, Any]] = []
    base = calibration_dir(root, tenant) / run_dir / "cases"
    for case_file in files:
        file_tick = parse_tick(case_file)
        try:
            raw = json.loads((base / case_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        after = raw.get("after")
        before = raw.get("before")
        after_events = after.get("state", {}).get("events") if isinstance(after, dict) else None
        before_events = before.get("state", {}).get("events") if isinstance(before, dict) else None
        raw_events = after_events if after_events is not None else before_events
        if not isinstance(raw_events, list):
            continue
        for ev in raw_events:
            if not isinstance(ev, dict):
                continue
            normalized = _normalize_event(ev, file_tick)
            if normalized is None:
                continue
            events.append(normalized)
    events.sort(key=lambda item: item["tick"])
    payload = aggregate_lifecycle(tenant, run_dir, events, now_ms=now_ms)
    payload["window"]["cases"] = len(files)
    _enrich_survey_db(root, tenant, payload)
    return payload


def _enrich_survey_db(
    data_root: str | os.PathLike[str], tenant: str, payload: dict[str, Any]
) -> None:
    """Open the tenant survey db read-only and backfill; never blocks on errors."""
    root = validate_data_root(data_root)
    path = survey_db_path(root, tenant)
    if not path.is_file():
        return
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return
    try:
        _enrich_from_survey_db(connection, payload)
    except sqlite3.Error:
        pass
    finally:
        connection.close()


def load_lifecycle_audit(
    data_root: str | os.PathLike[str],
    tenant: str = "all",
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load the ``/api/audit/lifecycle`` payload (tenant or all tenants)."""
    now = now_ms if now_ms is not None else current_epoch_ms()
    if tenant == "all":
        return {t: _audit_tenant(data_root, t, now) for t in TENANTS}
    return _audit_tenant(data_root, tenant, now)
