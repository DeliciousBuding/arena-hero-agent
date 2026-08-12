"""Materialize an alliance-advice golden fixture into a P5-3 data root (W25).

The ``alliance_advice_*.json`` golden fixtures describe a full Command Center
data root (world calibration cases, survey SQLite tables, leaderboard
snapshot, telemetry JSONL tails, human-command audit) that the *real* legacy
TypeScript oracle reads end-to-end (``loadAllianceAdvice``) and that the
Python loader reads through the P5-4 projections. One shared materializer
keeps both sides byte-consistent: the golden is produced by running Node on a
data root built by this helper, and the parity test runs
``load_alliance_advice`` on a data root built by the same helper.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

TENANTS = ("t1", "t2", "t3", "t4")

# The legacy arena-agent survey-db schema (packages/arena-agent/src/intel/survey-db.ts),
# kept in the tooling so the fixture materializer builds databases the TS oracle reads.
SURVEY_SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
  cell TEXT PRIMARY KEY,
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  first_seen_tick INTEGER NOT NULL,
  last_seen_tick INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'visible',
  last_state_tick INTEGER NOT NULL,
  seen_count INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS obstacles (
  cell TEXT PRIMARY KEY,
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  first_seen_tick INTEGER NOT NULL,
  last_seen_tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS core_hunts (
  cell TEXT PRIMARY KEY,
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  owner TEXT,
  source TEXT NOT NULL DEFAULT 'CORE',
  first_seen_tick INTEGER NOT NULL,
  last_seen_tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS units_seen (
  cell TEXT NOT NULL,
  unit_type TEXT NOT NULL,
  controlled INTEGER NOT NULL,
  tick INTEGER NOT NULL,
  x INTEGER,
  y INTEGER,
  PRIMARY KEY (cell, tick)
);
CREATE TABLE IF NOT EXISTS sync_meta (
  run_id TEXT PRIMARY KEY,
  tenant TEXT NOT NULL,
  cases_synced INTEGER NOT NULL DEFAULT 0,
  last_tick INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_units_seen_controlled_tick ON units_seen(controlled, tick);
CREATE TABLE IF NOT EXISTS heat_archive (
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  unit_type TEXT NOT NULL,
  count INTEGER NOT NULL,
  first_tick INTEGER NOT NULL,
  last_tick INTEGER NOT NULL,
  PRIMARY KEY (x, y, unit_type)
);
CREATE TABLE IF NOT EXISTS resource_absences (
  cell TEXT NOT NULL,
  tick INTEGER NOT NULL,
  PRIMARY KEY (cell, tick)
);
CREATE TABLE IF NOT EXISTS resource_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cell TEXT NOT NULL,
  tick INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  reason_code TEXT,
  amount INTEGER,
  actor_id TEXT,
  UNIQUE(cell, tick, event_type, actor_id)
);
CREATE TABLE IF NOT EXISTS unit_lifecycle (
  unit_id TEXT PRIMARY KEY,
  unit_type TEXT NOT NULL,
  birth_tick INTEGER,
  birth_pos TEXT,
  death_tick INTEGER,
  death_pos TEXT,
  death_reason TEXT,
  last_seen_tick INTEGER NOT NULL,
  last_seen_pos TEXT,
  current_state TEXT NOT NULL DEFAULT 'alive'
);
CREATE TABLE IF NOT EXISTS core_spends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  tick INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  unit_type TEXT,
  unit_id TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
  chunk_key TEXT PRIMARY KEY,
  last_seen_tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notable_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant TEXT NOT NULL,
  tick INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  actor_id TEXT,
  target_id TEXT,
  x INTEGER,
  y INTEGER,
  amount INTEGER,
  unit_type TEXT,
  reason_code TEXT,
  destroyed_by TEXT,
  is_our_core INTEGER,
  UNIQUE(tenant, tick, event_type, actor_id, target_id)
);
CREATE TABLE IF NOT EXISTS resource_seen_history (
  cell TEXT NOT NULL,
  tick INTEGER NOT NULL,
  PRIMARY KEY (cell, tick)
);
CREATE TABLE IF NOT EXISTS agents (
  tenant TEXT NOT NULL,
  instance TEXT NOT NULL,
  tick INTEGER,
  resources INTEGER,
  population INTEGER,
  core_x INTEGER,
  core_y INTEGER,
  units INTEGER,
  visible_enemies INTEGER,
  status TEXT,
  sdk_version TEXT,
  base_url TEXT,
  pid INTEGER,
  platform TEXT,
  mode TEXT NOT NULL DEFAULT 'production',
  connection_state TEXT NOT NULL DEFAULT 'down',
  first_seen TEXT,
  last_heartbeat TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant, instance)
);
CREATE TABLE IF NOT EXISTS agent_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant TEXT NOT NULL,
  instance TEXT NOT NULL,
  event TEXT NOT NULL,
  tick INTEGER,
  detail TEXT,
  ts REAL NOT NULL
);
"""

# fixture key -> SQLite table / column projection
_TABLE_MAP: dict[str, dict[str, Any]] = {
    "syncMeta": {
        "table": "sync_meta",
        "columns": ("run_id", "tenant", "cases_synced", "last_tick", "updated_at"),
    },
    "resources": {
        "table": "resources",
        "columns": (
            "cell",
            "x",
            "y",
            "first_seen_tick",
            "last_seen_tick",
            "state",
            "last_state_tick",
            "seen_count",
        ),
    },
    "obstacles": {
        "table": "obstacles",
        "columns": ("cell", "x", "y", "first_seen_tick", "last_seen_tick"),
    },
    "coreHunts": {
        "table": "core_hunts",
        "columns": ("cell", "x", "y", "owner", "source", "first_seen_tick", "last_seen_tick"),
    },
    "unitsSeen": {
        "table": "units_seen",
        "columns": ("cell", "unit_type", "controlled", "tick", "x", "y"),
    },
    "chunks": {"table": "chunks", "columns": ("chunk_key", "last_seen_tick")},
    "resourceEvents": {
        "table": "resource_events",
        "columns": ("cell", "tick", "event_type", "reason_code", "amount", "actor_id"),
    },
    "resourceSeenHistory": {
        "table": "resource_seen_history",
        "columns": ("cell", "tick"),
    },
    "unitLifecycle": {
        "table": "unit_lifecycle",
        "columns": (
            "unit_id",
            "unit_type",
            "birth_tick",
            "birth_pos",
            "death_tick",
            "death_pos",
            "death_reason",
            "last_seen_tick",
            "last_seen_pos",
            "current_state",
        ),
    },
    "coreSpends": {
        "table": "core_spends",
        "columns": ("kind", "tick", "amount", "unit_type", "unit_id"),
    },
    "notableEvents": {
        "table": "notable_events",
        "columns": (
            "tenant",
            "tick",
            "event_type",
            "actor_id",
            "target_id",
            "x",
            "y",
            "amount",
            "unit_type",
            "reason_code",
            "destroyed_by",
            "is_our_core",
        ),
    },
    "agentEvents": {
        "table": "agent_events",
        "columns": ("tenant", "instance", "event", "tick", "detail", "ts"),
    },
    "resourceAbsences": {"table": "resource_absences", "columns": ("cell", "tick")},
    "heatArchive": {
        "table": "heat_archive",
        "columns": ("x", "y", "unit_type", "count", "first_tick", "last_tick"),
    },
    "agents": {
        "table": "agents",
        "columns": (
            "tenant",
            "instance",
            "tick",
            "resources",
            "population",
            "core_x",
            "core_y",
            "units",
            "visible_enemies",
            "status",
            "sdk_version",
            "base_url",
            "pid",
            "platform",
            "mode",
            "connection_state",
            "first_seen",
            "last_heartbeat",
            "updated_at",
        ),
    },
}


def _write_survey_db(path: Path, tenant: str, rows_by_table: dict[str, Any]) -> None:
    """Create one tenant survey database with the TS schema + fixture rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SURVEY_SCHEMA)
        for fixture_key, mapping in _TABLE_MAP.items():
            rows = rows_by_table.get(fixture_key) or ()
            columns = mapping["columns"]
            placeholders = ",".join("?" for _ in columns)
            insert = f"INSERT INTO {mapping['table']} ({','.join(columns)}) VALUES ({placeholders})"
            for row in rows:
                if not isinstance(row, dict):
                    continue
                values = [row.get(column) for column in columns]
                connection.execute(insert, values)
        connection.commit()
    finally:
        connection.close()


REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('production','simulation')),
  api_key_tail TEXT,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS keys (
  key_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('production','simulation')),
  key_hash TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  revoked_at TEXT
);
"""


def _write_registry(
    path: Path,
    agents: Sequence[Mapping[str, Any]] | None = None,
    keys: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    """Create the agent registry database with the TS schema + fixture rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(REGISTRY_SCHEMA)
        if agents:
            connection.executemany(
                "INSERT INTO agents (agent_id, username, mode, api_key_tail, created_at,"
                " revoked_at) VALUES (:agent_id, :username, :mode, :api_key_tail,"
                " :created_at, :revoked_at)",
                agents,
            )
        if keys:
            connection.executemany(
                "INSERT INTO keys (key_id, agent_id, mode, key_hash, issued_at, revoked_at)"
                " VALUES (:key_id, :agent_id, :mode, :key_hash, :issued_at, :revoked_at)",
                keys,
            )
        connection.commit()
    finally:
        connection.close()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def _write_calibration_run(
    root: Path, tenant: str, run_name: str, cases: Sequence[Mapping[str, Any]]
) -> None:
    """One calibration run with multiple case files (W44 wave 7 intel scans).

    The intel projection scans the last ``INTEL_CASE_LIMIT`` cases per run and
    orders runs by their highest case tick, so the fixture may span several
    runs (``calibrationRuns`` key) to exercise run ordering end-to-end.
    """
    cases_dir = root / "runtime" / tenant / "calibration" / run_name / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    for entry in cases:
        if not isinstance(entry, dict):
            continue
        after = entry.get("after")
        before = entry.get("before")
        if not isinstance(after, dict):
            after = {}
        if not isinstance(before, dict):
            before = {}
        raw_tick = after.get("tick")
        if raw_tick is None:
            raw_tick = before.get("tick")
        if raw_tick is None:
            raw_tick = entry.get("tick")
        tick = int(raw_tick or 0)
        if tick <= 0:
            continue
        (cases_dir / f"{tick}.json").write_text(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )


def _write_world(root: Path, tenant: str, case: dict[str, Any], tick: int) -> None:
    """Latest calibration run with one case file (TS ``loadWorld`` shape)."""
    cases_dir = root / "runtime" / tenant / "calibration" / "run-1" / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    payload = case if isinstance(case, dict) else {}
    if "after" not in payload and "before" not in payload:
        payload = {"after": payload}
    (cases_dir / f"{tick}.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )


def materialize_advice_data_root(fixture: dict[str, Any], root: Path) -> Path:
    """Write the fixture spec into ``root`` as a Command Center data root."""
    root.mkdir(parents=True, exist_ok=True)
    now_ms = int(fixture.get("nowMs", 0))
    calibration_runs = fixture.get("calibrationRuns") or {}
    for tenant, runs in calibration_runs.items():
        if not isinstance(runs, dict):
            continue
        for run_name, cases in runs.items():
            if isinstance(cases, list):
                _write_calibration_run(root, str(tenant), str(run_name), cases)
    worlds = fixture.get("worlds") or {}
    survey = fixture.get("survey") or {}
    for tenant in TENANTS:
        case = worlds.get(tenant) or {}
        if isinstance(case, dict) and isinstance(case.get("cases"), list):
            for entry in case["cases"]:
                if not isinstance(entry, dict):
                    continue
                tick = 0
                after = entry.get("after")
                before = entry.get("before")
                if not isinstance(after, dict):
                    after = {}
                if not isinstance(before, dict):
                    before = {}
                raw_tick = after.get("tick")
                if raw_tick is None:
                    raw_tick = before.get("tick")
                if raw_tick is None:
                    raw_tick = entry.get("tick")
                tick = int(raw_tick or 0)
                if tick > 0:
                    _write_world(root, tenant, entry, tick)
            _write_survey_db(
                root / "runtime" / "survey" / f"{tenant}.db", tenant, survey.get(tenant) or {}
            )
            continue
        tick = 0
        if isinstance(case, dict):
            after = case.get("after")
            if not isinstance(after, dict):
                after = {}
            before = case.get("before")
            if not isinstance(before, dict):
                before = {}
            raw_tick = after.get("tick")
            if raw_tick is None:
                raw_tick = before.get("tick")
            if raw_tick is None:
                raw_tick = case.get("tick")
            tick = int(raw_tick or 0)
        if tick > 0:
            _write_world(root, tenant, case, tick)
        _write_survey_db(
            root / "runtime" / "survey" / f"{tenant}.db", tenant, survey.get(tenant) or {}
        )
    leaderboard = fixture.get("leaderboard")
    if isinstance(leaderboard, dict) and leaderboard.get("content"):
        lb_dir = root / "leaderboard"
        lb_dir.mkdir(parents=True, exist_ok=True)
        file_name = str(leaderboard.get("fileName") or "leaderboard-2026-07-01-00-00-00.json")
        lb_path = lb_dir / file_name
        lb_path.write_text(
            json.dumps(leaderboard["content"], ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        mtime_ms = leaderboard.get("mtimeMs", now_ms)
        os.utime(lb_path, (mtime_ms / 1000, mtime_ms / 1000))
    telemetry = fixture.get("telemetry") or {}
    for tenant, files in telemetry.items():
        if not isinstance(files, dict):
            continue
        for name in ("decision", "outcome"):
            rows = files.get(name) or ()
            if rows:
                _write_jsonl(root / "runtime" / tenant / "telemetry" / f"{name}.jsonl", rows)
    human_audit = fixture.get("humanAudit") or ()
    if human_audit:
        _write_jsonl(root / "runtime" / "human-command-audit.jsonl", human_audit)
    arbitration = fixture.get("arbitration") or ()
    if arbitration:
        _write_jsonl(root / "runtime" / "survey" / "arbitration.jsonl", arbitration)
    runtime_jsonl = fixture.get("runtimeJsonl") or {}
    for tenant, rows in runtime_jsonl.items():
        if rows:
            _write_jsonl(root / "runtime" / tenant / "telemetry" / "runtime.jsonl", rows)
    command_audit = fixture.get("commandAudit") or {}
    for tenant, rows in command_audit.items():
        if rows:
            _write_jsonl(root / "runtime" / "command-audit" / f"{tenant}.jsonl", rows)
    supervisor = fixture.get("supervisor") or ()
    if supervisor:
        _write_jsonl(root / "runtime" / "supervisor.jsonl", supervisor)
    shop_history = fixture.get("shopHistory") or ()
    if shop_history:
        _write_jsonl(root / "runtime" / "shop-history.jsonl", shop_history)
    redeem_log = fixture.get("redeemLog") or ()
    if redeem_log:
        _write_jsonl(root / "runtime" / "redeem-log.jsonl", redeem_log)
    registry = fixture.get("registry")
    if isinstance(registry, dict):
        _write_registry(
            root / "runtime" / "registry.db",
            list(registry.get("agents") or ()),
            list(registry.get("keys") or ()),
        )
    return root
