"""Survey database base for the Python Command Center (P5-3).

``data/runtime/survey/<tenant>.db`` is written by the agent ingest data path
(python real-time domain) and read by the survey projections (P5-4). The
schema is the command-center's ``AGENT_SCHEMA`` contract: the ``agents`` /
``agent_events`` ledger plus the python-mapping telemetry tables
(``resources``, ``obstacles``, ``core_hunts``, ``units_seen``,
``resource_seen_history``) whose SQL semantics match the arena-agent
``survey-db.ts`` upserts.

Registered differences from the TS oracle:

- A read-only open of a missing database yields an unavailable handle
  (``is_available`` is False) instead of silently creating an empty database;
  write opens create the file, directory, WAL mode, and schema.
- Legacy TS-era schema migrations (single-column PK -> composite PK, missing
  ``mode``/``vanguards``/``rangers`` columns) are not auto-applied: a write
  open verifies the ``agents`` primary key is the composite
  ``(tenant, instance)`` and raises fail-closed when it is not.
- ``agents``/``agent_events`` rows are read through a strict event contract
  (event kind and mode allowlists), raising instead of trusting unknown values.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

from arena_hero_agent.adapters.recorder._common import (
    acquire_process_lock,
    register_target,
    release_process_lock,
    unregister_target,
)

from .errors import CommandCenterError
from .goal_store import iso_utc
from .paths import survey_db_path, validate_data_root, validate_survey_tenant

AgentEventKind = Literal["register", "connection", "tick_summary", "disconnected"]
AGENT_EVENT_KINDS: tuple[AgentEventKind, ...] = (
    "register",
    "connection",
    "tick_summary",
    "disconnected",
)
SURVEY_TENANT_MODE = ("production", "simulation")

SURVEY_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  tenant TEXT NOT NULL,
  instance TEXT NOT NULL,
  tick INTEGER,
  resources INTEGER,
  population INTEGER,
  core_x INTEGER,
  core_y INTEGER,
  units INTEGER,
  vanguards INTEGER,
  rangers INTEGER,
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
CREATE INDEX IF NOT EXISTS idx_agent_events_tenant_ts ON agent_events(tenant, ts);
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
CREATE TABLE IF NOT EXISTS resource_seen_history (
  cell TEXT NOT NULL,
  tick INTEGER NOT NULL,
  PRIMARY KEY (cell, tick)
);
CREATE INDEX IF NOT EXISTS idx_resource_seen_history_cell ON resource_seen_history(cell, tick);
CREATE INDEX IF NOT EXISTS idx_resource_seen_history_tick ON resource_seen_history(tick);
"""

_AGENT_UPSERT = """
INSERT INTO agents (
  tenant, instance, tick, resources, population, core_x, core_y, units,
  vanguards, rangers, visible_enemies, status, sdk_version, base_url, pid,
  platform, mode, connection_state, first_seen, last_heartbeat, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'production'), ?, ?, ?, ?)
ON CONFLICT(tenant, instance) DO UPDATE SET
  tick = COALESCE(excluded.tick, agents.tick),
  resources = COALESCE(excluded.resources, agents.resources),
  population = COALESCE(excluded.population, agents.population),
  core_x = COALESCE(excluded.core_x, agents.core_x),
  core_y = COALESCE(excluded.core_y, agents.core_y),
  units = COALESCE(excluded.units, agents.units),
  vanguards = COALESCE(excluded.vanguards, agents.vanguards),
  rangers = COALESCE(excluded.rangers, agents.rangers),
  visible_enemies = COALESCE(excluded.visible_enemies, agents.visible_enemies),
  status = COALESCE(excluded.status, agents.status),
  sdk_version = COALESCE(excluded.sdk_version, agents.sdk_version),
  base_url = COALESCE(excluded.base_url, agents.base_url),
  pid = COALESCE(excluded.pid, agents.pid),
  platform = COALESCE(excluded.platform, agents.platform),
  mode = COALESCE(?, agents.mode),
  connection_state = excluded.connection_state,
  last_heartbeat = COALESCE(excluded.last_heartbeat, agents.last_heartbeat),
  updated_at = excluded.updated_at
"""


@dataclass(frozen=True, slots=True)
class AgentIngestEvent:
    """One SDK telemetry event destined for the survey database."""

    tenant: str
    ts: float
    event: AgentEventKind
    instance: str | None = None
    tick: int | None = None
    status: str | None = None
    resources: int | None = None
    population: int | None = None
    core: tuple[int, int] | None = None
    units: int | None = None
    controlled_by_type: dict[str, int] | None = None
    visible_enemies: int | None = None
    api_key_tail: str | None = None
    base_url: str | None = None
    sdk_version: str | None = None
    pid: int | None = None
    platform: str | None = None
    mode: str | None = None
    error: str | None = None
    resource_cells: list[tuple[int, int]] | None = None
    obstacle_cells: list[tuple[int, int]] | None = None
    units_seen: list[tuple[Any, ...]] | None = None
    enemy_cores: list[tuple[int, int, str]] | None = None


@dataclass(frozen=True, slots=True)
class AgentRow:
    """One agents-ledger row (snake_case; camelCase keys are the API mapping)."""

    tenant: str
    instance: str
    tick: int | None
    resources: int | None
    population: int | None
    core_x: int | None
    core_y: int | None
    units: int | None
    vanguards: int | None
    rangers: int | None
    visible_enemies: int | None
    status: str | None
    sdk_version: str | None
    base_url: str | None
    pid: int | None
    platform: str | None
    mode: str
    connection_state: Literal["up", "down"]
    first_seen: str
    last_heartbeat: str | None
    updated_at: str


def _validate_event(event: AgentIngestEvent) -> None:
    if event.event not in AGENT_EVENT_KINDS:
        raise CommandCenterError(
            f"ingest event kind must be one of {AGENT_EVENT_KINDS}; actual={event.event!r}"
        )
    validate_survey_tenant(event.tenant)
    if event.mode is not None and event.mode not in SURVEY_TENANT_MODE:
        raise CommandCenterError(
            f"ingest mode must be production|simulation or omitted; actual={event.mode!r}"
        )


def _row_to_agent(row: sqlite3.Row) -> AgentRow:
    connection_state: Literal["up", "down"] = (
        "down" if str(row["connection_state"]) == "down" else "up"
    )
    return AgentRow(
        tenant=str(row["tenant"]),
        instance=str(row["instance"]),
        tick=_optional_int(row["tick"]),
        resources=_optional_int(row["resources"]),
        population=_optional_int(row["population"]),
        core_x=_optional_int(row["core_x"]),
        core_y=_optional_int(row["core_y"]),
        units=_optional_int(row["units"]),
        vanguards=_optional_int(row["vanguards"]),
        rangers=_optional_int(row["rangers"]),
        visible_enemies=_optional_int(row["visible_enemies"]),
        status=_optional_str(row["status"]),
        sdk_version=_optional_str(row["sdk_version"]),
        base_url=_optional_str(row["base_url"]),
        pid=_optional_int(row["pid"]),
        platform=_optional_str(row["platform"]),
        mode=str(row["mode"] or "production"),
        connection_state=connection_state,
        first_seen=str(row["first_seen"]),
        last_heartbeat=_optional_str(row["last_heartbeat"]),
        updated_at=str(row["updated_at"]),
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise CommandCenterError(f"expected an integer column value; actual={value!r}")
    if isinstance(value, (int, float, str)):
        return int(value)
    raise CommandCenterError(f"expected an integer column value; actual={value!r}")


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


class SurveyDb:
    """Read/write handle for one tenant's survey database."""

    def __init__(
        self,
        data_root: str | os.PathLike[str],
        tenant: str,
        *,
        write: bool = False,
        busy_timeout_ms: int = 5000,
    ) -> None:
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise CommandCenterError(
                f"busy_timeout_ms must be an integer; actual={busy_timeout_ms!r}"
            )
        if busy_timeout_ms < 0:
            raise CommandCenterError(
                f"busy_timeout_ms cannot be negative; actual={busy_timeout_ms}"
            )
        root = validate_data_root(data_root)
        self._tenant = validate_survey_tenant(tenant)
        self._path = survey_db_path(root, self._tenant)
        self._write = write
        self._busy_timeout_ms = busy_timeout_ms
        self._closed = True
        self._connection: sqlite3.Connection | None = None
        self._process_lock: BinaryIO | None = None
        self._registry_key = str(self._path.resolve())
        if write:
            register_target(self._registry_key, self)
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._process_lock = acquire_process_lock(Path(f"{self._path}.lock"))
                self._connection = sqlite3.connect(self._path)
                self._connection.row_factory = sqlite3.Row
                self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.executescript(SURVEY_DB_SCHEMA)
                self._connection.commit()
                self._verify_write_schema()
            except CommandCenterError:
                self._cleanup_partial_init()
                raise
            except sqlite3.Error as exc:
                self._cleanup_partial_init()
                raise CommandCenterError(
                    f"failed to open survey database {self._path}: {exc}"
                ) from exc
            except BaseException:
                self._cleanup_partial_init()
                raise
        elif self._path.exists():
            try:
                self._connection = sqlite3.connect(
                    f"file:{self._path.as_posix()}?mode=ro", uri=True
                )
                self._connection.row_factory = sqlite3.Row
                self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            except sqlite3.Error as exc:
                raise CommandCenterError(
                    f"failed to open survey database {self._path} read-only: {exc}"
                ) from exc
        self._closed = False

    def _verify_write_schema(self) -> None:
        assert self._connection is not None
        columns = [row["name"] for row in self._connection.execute("PRAGMA table_info(agents)")]
        if "tenant" not in columns or "instance" not in columns:
            return
        indexes = self._connection.execute("PRAGMA index_list(agents)").fetchall()
        for index in indexes:
            if index["origin"] != "pk":
                continue
            names = [
                row["name"]
                for row in self._connection.execute(f"PRAGMA index_info({index['name']})")
            ]
            if "tenant" in names and "instance" in names:
                return
        raise CommandCenterError(
            f"survey database {self._path} has a legacy single-column agents primary key; "
            "refusing to write without the composite (tenant, instance) key"
        )

    def _cleanup_partial_init(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._process_lock is not None:
            release_process_lock(self._process_lock)
            self._process_lock = None
        if self._write:
            unregister_target(self._registry_key, self)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def tenant(self) -> str:
        return self._tenant

    @property
    def is_available(self) -> bool:
        """False for a read-only open of a missing database."""
        return self._connection is not None

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise CommandCenterError("survey database is closed")
        if self._connection is None:
            raise CommandCenterError(
                f"survey database {self._path} is unavailable (read-only open of a missing file)"
            )
        return self._connection

    def apply_agent_event(self, event: AgentIngestEvent) -> None:
        """Upsert the agents row, append the event, and apply mapping upserts."""
        connection = self._require_connection()
        if not self._write:
            raise CommandCenterError("apply_agent_event requires a write handle")
        _validate_event(event)
        now = iso_utc(time.time_ns() // 1_000_000)
        instance = event.instance or event.tenant
        tick = event.tick
        core_x, core_y = event.core or (None, None)
        mode_value = event.mode if event.mode in SURVEY_TENANT_MODE else None
        controlled = event.controlled_by_type
        vanguards = None if controlled is None else controlled.get("VANGUARD", 0)
        rangers = None if controlled is None else controlled.get("RANGER", 0)
        try:
            with connection:
                connection.execute(
                    _AGENT_UPSERT,
                    (
                        event.tenant,
                        instance,
                        tick,
                        event.resources,
                        event.population,
                        core_x,
                        core_y,
                        event.units,
                        vanguards,
                        rangers,
                        event.visible_enemies,
                        event.status,
                        event.sdk_version,
                        event.base_url,
                        event.pid,
                        event.platform,
                        mode_value,
                        "down" if event.event == "disconnected" else "up",
                        now,
                        now if event.event == "tick_summary" else None,
                        now,
                        mode_value,
                    ),
                )
                connection.execute(
                    "INSERT INTO agent_events (tenant, instance, event, tick, detail, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event.tenant,
                        instance,
                        event.event,
                        tick,
                        event.error,
                        event.ts,
                    ),
                )
            if event.event == "tick_summary" and tick is not None:
                self._apply_mapping_upserts(event, tick)
        except sqlite3.Error as exc:
            raise CommandCenterError(f"failed to apply agent event: {exc}") from exc

    def _apply_mapping_upserts(self, event: AgentIngestEvent, tick: int) -> None:
        connection = self._require_connection()
        try:
            with connection:
                for x, y in event.resource_cells or []:
                    key = f"{x},{y}"
                    connection.execute(
                        "INSERT INTO resources (cell, x, y, first_seen_tick, last_seen_tick, "
                        "state, last_state_tick, seen_count) VALUES (?, ?, ?, ?, ?, "
                        "'visible', ?, 1) "
                        "ON CONFLICT(cell) DO UPDATE SET "
                        "last_seen_tick = MAX(resources.last_seen_tick, excluded.last_seen_tick), "
                        "state = 'visible', last_state_tick = excluded.last_state_tick, "
                        "seen_count = resources.seen_count + 1",
                        (key, x, y, tick, tick, tick),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO resource_seen_history (cell, tick) VALUES (?, ?)",
                        (key, tick),
                    )
                for x, y in event.obstacle_cells or []:
                    key = f"{x},{y}"
                    connection.execute(
                        "INSERT INTO obstacles (cell, x, y, first_seen_tick, last_seen_tick) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(cell) DO UPDATE SET "
                        "last_seen_tick = MAX(obstacles.last_seen_tick, excluded.last_seen_tick)",
                        (key, x, y, tick, tick),
                    )
                for entry in event.units_seen or []:
                    if len(entry) < 5:
                        continue
                    _unit_id, unit_type, controlled, x, y = entry[:5]
                    if controlled != 0:
                        continue
                    key = f"{x},{y}"
                    connection.execute(
                        "INSERT INTO units_seen (cell, unit_type, controlled, tick, x, y) "
                        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(cell, tick) DO NOTHING",
                        (key, unit_type, 0, tick, x, y),
                    )
                for x, y, owner in event.enemy_cores or []:
                    key = f"{x},{y}"
                    connection.execute(
                        "INSERT INTO core_hunts (cell, x, y, owner, source, first_seen_tick, "
                        "last_seen_tick) VALUES (?, ?, ?, ?, 'CORE', ?, ?) ON CONFLICT(cell) "
                        "DO UPDATE SET owner = COALESCE(excluded.owner, core_hunts.owner), "
                        "first_seen_tick = MIN(core_hunts.first_seen_tick, "
                        "excluded.first_seen_tick), "
                        "last_seen_tick = MAX(core_hunts.last_seen_tick, excluded.last_seen_tick)",
                        (key, x, y, owner, tick, tick),
                    )
        except sqlite3.Error as exc:
            raise CommandCenterError(f"failed to apply mapping upserts: {exc}") from exc

    def known_agent(self) -> AgentRow | None:
        """Most recent heartbeat row for this tenant (None when no data)."""
        if not self.is_available:
            return None
        connection = self._require_connection()
        try:
            row = connection.execute(
                "SELECT * FROM agents WHERE tenant = ? ORDER BY updated_at DESC LIMIT 1",
                (self._tenant,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise CommandCenterError(f"failed to read known agent: {exc}") from exc
        return None if row is None else _row_to_agent(row)

    def recent_agent_events(self, limit: int = 50) -> list[dict[str, object]]:
        """Most recent connection events for this tenant (default 50)."""
        if not self.is_available:
            return []
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise CommandCenterError(f"invalid event limit: {limit!r}")
        connection = self._require_connection()
        try:
            rows = connection.execute(
                "SELECT id, tenant, instance, event, tick, detail, ts FROM agent_events "
                "WHERE tenant = ? ORDER BY ts DESC LIMIT ?",
                (self._tenant, limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise CommandCenterError(f"failed to read agent events: {exc}") from exc
        return [dict(row) for row in rows]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._process_lock is not None:
            release_process_lock(self._process_lock)
            self._process_lock = None
        if self._write:
            unregister_target(self._registry_key, self)

    def __enter__(self) -> SurveyDb:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "AGENT_EVENT_KINDS",
    "AgentIngestEvent",
    "AgentRow",
    "SURVEY_DB_SCHEMA",
    "SurveyDb",
]
