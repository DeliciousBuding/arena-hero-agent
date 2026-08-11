"""Agent registry SQLite store (port of legacy ``registry.ts``).

``data/runtime/registry.db`` (WAL) holds the agent ledger and key hashes:

- production agents register the official key tail only (never the full key);
- simulation agents are issued a one-time plaintext sim key
  (``simkey-<24 hex>``) whose SHA-256 hash is stored; the plaintext is
  returned exactly once and is not recoverable afterwards;
- every list/query surface excludes plaintext keys;
- revocation is a soft delete (``revoked_at`` on the agent and active keys).

Fail-closed rules carried over from the oracle: an empty username, an unknown
mode, and a production registration without ``api_key_tail`` all raise. Key
comparison for ``verify_sim_key`` is constant-time over the SHA-256 digests.

Single-writer: the same in-process target registry and advisory cross-process
lock used by the P4-5 recorder guard this database, so a second live writer
for the same file fails loudly instead of interleaving.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from arena_hero_agent.adapters.recorder._common import (
    RecorderError,
    acquire_process_lock,
    register_target,
    release_process_lock,
    unregister_target,
)

from .errors import CommandCenterError
from .goal_store import iso_utc
from .paths import registry_db_path, validate_data_root

SIMKEY_PREFIX = "simkey-"
SIMKEY_HEX_BYTES = 12
AgentMode = Literal["production", "simulation"]
AGENT_MODES: tuple[AgentMode, ...] = ("production", "simulation")

_REGISTRY_SCHEMA = """
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
CREATE INDEX IF NOT EXISTS idx_keys_agent_id ON keys(agent_id);
"""


@dataclass(frozen=True, slots=True)
class RegisteredAgent:
    """One agent ledger row (plaintext sim key only present once on issuance)."""

    agent_id: str
    username: str
    mode: AgentMode
    api_key_tail: str | None
    created_at: str
    revoked_at: str | None
    plaintext_sim_key: str | None = None


@dataclass(frozen=True, slots=True)
class RegistryKey:
    """One issued key row (hash only; the plaintext is never stored)."""

    key_id: str
    agent_id: str
    mode: AgentMode
    key_hash: str
    issued_at: str
    revoked_at: str | None


def sha256_hex(text: str) -> str:
    """SHA-256 hex digest over UTF-8 bytes (registry key hashing)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generate_sim_key() -> str:
    """One-time simulation key: ``simkey-`` + 24 lowercase hex chars."""
    return f"{SIMKEY_PREFIX}{os.urandom(SIMKEY_HEX_BYTES).hex()}"


def _agent_mode_of(value: object) -> AgentMode:
    if isinstance(value, str) and value in AGENT_MODES:
        return value
    return "production"


def _row_to_agent(row: sqlite3.Row) -> RegisteredAgent:
    return RegisteredAgent(
        agent_id=str(row["agent_id"]),
        username=str(row["username"]),
        mode=_agent_mode_of(row["mode"]),
        api_key_tail=None if row["api_key_tail"] is None else str(row["api_key_tail"]),
        created_at=str(row["created_at"]),
        revoked_at=None if row["revoked_at"] is None else str(row["revoked_at"]),
    )


def _row_to_key(row: sqlite3.Row) -> RegistryKey:
    return RegistryKey(
        key_id=str(row["key_id"]),
        agent_id=str(row["agent_id"]),
        mode=_agent_mode_of(row["mode"]),
        key_hash=str(row["key_hash"]),
        issued_at=str(row["issued_at"]),
        revoked_at=None if row["revoked_at"] is None else str(row["revoked_at"]),
    )


class RegistryStore:
    """WAL-backed agent registry with a single-writer guard."""

    def __init__(self, data_root: str | os.PathLike[str], *, busy_timeout_ms: int = 5000) -> None:
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise CommandCenterError(
                f"busy_timeout_ms must be an integer; actual={busy_timeout_ms!r}"
            )
        if busy_timeout_ms < 0:
            raise CommandCenterError(
                f"busy_timeout_ms cannot be negative; actual={busy_timeout_ms}"
            )
        self._path = registry_db_path(validate_data_root(data_root))
        self._lock_path = Path(f"{self._path}.lock")
        self._registry_key = str(self._path.resolve())
        self._closed = True
        self._connection: sqlite3.Connection | None = None
        self._process_lock: BinaryIO | None = None
        try:
            register_target(self._registry_key, self)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._process_lock = acquire_process_lock(self._lock_path)
            self._connection = sqlite3.connect(self._path)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(_REGISTRY_SCHEMA)
            self._connection.commit()
        except CommandCenterError:
            self._cleanup_partial_init()
            raise
        except RecorderError as exc:
            self._cleanup_partial_init()
            raise CommandCenterError(str(exc)) from exc
        except sqlite3.Error as exc:
            self._cleanup_partial_init()
            raise CommandCenterError(
                f"failed to open registry database {self._path}: {exc}"
            ) from exc
        except BaseException:
            self._cleanup_partial_init()
            raise
        self._closed = False

    def _cleanup_partial_init(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._process_lock is not None:
            release_process_lock(self._process_lock)
            self._process_lock = None
        unregister_target(self._registry_key, self)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> sqlite3.Connection:
        if self._closed or self._connection is None:
            raise CommandCenterError("registry store is closed")
        return self._connection

    def register_agent(
        self,
        *,
        username: str,
        mode: AgentMode,
        api_key_tail: str | None = None,
        now_ms: int | None = None,
        agent_id: str | None = None,
        key_id: str | None = None,
    ) -> RegisteredAgent:
        """Register a production (official key tail) or simulation (sim key) agent."""
        connection = self._require_open()
        trimmed = username.strip()
        if not trimmed:
            raise CommandCenterError("username cannot be empty")
        if mode not in AGENT_MODES:
            raise CommandCenterError(f"mode must be production or simulation; actual={mode!r}")
        tail = (api_key_tail or "").strip()
        if mode == "production" and not tail:
            raise CommandCenterError("production mode requires api_key_tail")
        now = iso_utc(now_ms) if now_ms is not None else iso_utc(time.time_ns() // 1_000_000)
        agent_id = agent_id or str(uuid.uuid4())
        try:
            with connection:
                connection.execute(
                    "INSERT INTO agents (agent_id, username, mode, api_key_tail, created_at, "
                    "revoked_at) VALUES (?, ?, ?, ?, ?, NULL)",
                    (agent_id, trimmed, mode, tail if mode == "production" else None, now),
                )
        except sqlite3.Error as exc:
            raise CommandCenterError(f"failed to register agent: {exc}") from exc
        plaintext_sim_key: str | None = None
        if mode == "simulation":
            plaintext_sim_key = self._issue_sim_key(agent_id, mode, now_ms=now_ms, key_id=key_id)
        return RegisteredAgent(
            agent_id=agent_id,
            username=trimmed,
            mode=mode,
            api_key_tail=tail if mode == "production" else None,
            created_at=now,
            revoked_at=None,
            plaintext_sim_key=plaintext_sim_key,
        )

    def issue_key(
        self,
        agent_id: str,
        *,
        now_ms: int | None = None,
        key_id: str | None = None,
    ) -> RegisteredAgent | None:
        """Reissue a plaintext sim key for a simulation agent (None when ineligible)."""
        connection = self._require_open()
        try:
            row = connection.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise CommandCenterError(f"failed to look up agent {agent_id!r}: {exc}") from exc
        if row is None:
            return None
        agent = _row_to_agent(row)
        if agent.mode != "simulation" or agent.revoked_at is not None:
            return None
        plaintext = self._issue_sim_key(agent_id, "simulation", now_ms=now_ms, key_id=key_id)
        return RegisteredAgent(
            agent_id=agent.agent_id,
            username=agent.username,
            mode=agent.mode,
            api_key_tail=agent.api_key_tail,
            created_at=agent.created_at,
            revoked_at=agent.revoked_at,
            plaintext_sim_key=plaintext,
        )

    def _issue_sim_key(
        self,
        agent_id: str,
        mode: AgentMode,
        *,
        now_ms: int | None = None,
        key_id: str | None = None,
    ) -> str:
        connection = self._require_open()
        key = generate_sim_key()
        now = iso_utc(now_ms) if now_ms is not None else iso_utc(time.time_ns() // 1_000_000)
        try:
            with connection:
                connection.execute(
                    "INSERT INTO keys (key_id, agent_id, mode, key_hash, issued_at, revoked_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL)",
                    (key_id or str(uuid.uuid4()), agent_id, mode, sha256_hex(key), now),
                )
        except sqlite3.Error as exc:
            raise CommandCenterError(f"failed to issue registry key: {exc}") from exc
        return key

    def list_agents(self) -> list[dict[str, object]]:
        """All agents with their key records (hashes only; no plaintext)."""
        connection = self._require_open()
        try:
            agent_rows = connection.execute(
                "SELECT * FROM agents ORDER BY created_at ASC"
            ).fetchall()
            key_rows = connection.execute("SELECT * FROM keys ORDER BY issued_at ASC").fetchall()
        except sqlite3.Error as exc:
            raise CommandCenterError(f"failed to list agents: {exc}") from exc
        keys_by_agent: dict[str, list[RegistryKey]] = {}
        for row in key_rows:
            key = _row_to_key(row)
            keys_by_agent.setdefault(key.agent_id, []).append(key)
        out: list[dict[str, object]] = []
        for row in agent_rows:
            agent = _row_to_agent(row)
            entry = asdict(agent)
            entry["plaintext_sim_key"] = None
            entry["keys"] = keys_by_agent.get(agent.agent_id, [])
            out.append(entry)
        return out

    def revoke_agent(self, agent_id: str, *, now_ms: int | None = None) -> RegisteredAgent | None:
        """Soft-delete an agent and its active keys (None when unknown)."""
        connection = self._require_open()
        try:
            row = connection.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise CommandCenterError(f"failed to look up agent {agent_id!r}: {exc}") from exc
        if row is None:
            return None
        now = iso_utc(now_ms) if now_ms is not None else iso_utc(time.time_ns() // 1_000_000)
        try:
            with connection:
                connection.execute(
                    "UPDATE agents SET revoked_at = ? WHERE agent_id = ?", (now, agent_id)
                )
                connection.execute(
                    "UPDATE keys SET revoked_at = ? WHERE agent_id = ? AND revoked_at IS NULL",
                    (now, agent_id),
                )
        except sqlite3.Error as exc:
            raise CommandCenterError(f"failed to revoke agent {agent_id!r}: {exc}") from exc
        updated = connection.execute(
            "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        return None if updated is None else _row_to_agent(updated)

    def verify_sim_key(self, key: str) -> str | None:
        """Validate a sim key with constant-time digest comparison; None when invalid."""
        connection = self._require_open()
        if not key.startswith(SIMKEY_PREFIX):
            return None
        digest = sha256_hex(key)
        try:
            expected = bytes.fromhex(digest)
            candidates = connection.execute(
                "SELECT key_hash FROM keys WHERE mode = 'simulation' AND revoked_at IS NULL"
            ).fetchall()
        except sqlite3.Error as exc:
            raise CommandCenterError(f"failed to verify sim key: {exc}") from exc
        for candidate in candidates:
            candidate_hex = str(candidate["key_hash"])
            try:
                actual = bytes.fromhex(candidate_hex)
            except ValueError:
                continue
            if len(actual) == len(expected) and hmac.compare_digest(actual, expected):
                row = connection.execute(
                    "SELECT agent_id FROM keys WHERE key_hash = ? AND mode = 'simulation' "
                    "AND revoked_at IS NULL",
                    (digest,),
                ).fetchone()
                return None if row is None else str(row["agent_id"])
        return None

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
        unregister_target(self._registry_key, self)

    def __enter__(self) -> RegistryStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "AGENT_MODES",
    "AgentMode",
    "SIMKEY_HEX_BYTES",
    "SIMKEY_PREFIX",
    "RegisteredAgent",
    "RegistryKey",
    "RegistryStore",
    "generate_sim_key",
    "sha256_hex",
]
