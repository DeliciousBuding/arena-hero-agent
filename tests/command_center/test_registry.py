"""Agent registry SQLite store semantics (legacy registry.ts port)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from arena_hero_agent.command_center import (
    SIMKEY_PREFIX,
    AgentMode,
    CommandCenterError,
    RegistryKey,
    RegistryStore,
    generate_sim_key,
    sha256_hex,
)

NOW = 1_752_000_000_000
SIMKEY_RE = re.compile(r"^simkey-[0-9a-f]{24}$")


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "runtime" / "registry.db"


def test_open_creates_wal_schema(tmp_path: Path) -> None:
    with RegistryStore(tmp_path) as store:
        assert store.path == _db_path(tmp_path)
        connection = sqlite3.connect(store.path)
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert {"agents", "keys"} <= tables
            agents_cols = {row[1] for row in connection.execute("PRAGMA table_info(agents)")}
            assert {
                "agent_id",
                "username",
                "mode",
                "api_key_tail",
                "created_at",
                "revoked_at",
            } <= agents_cols
            keys_cols = {row[1] for row in connection.execute("PRAGMA table_info(keys)")}
            assert {
                "key_id",
                "agent_id",
                "mode",
                "key_hash",
                "issued_at",
                "revoked_at",
            } <= keys_cols
        finally:
            connection.close()


def test_register_simulation_issues_plaintext_simkey_once(tmp_path: Path) -> None:
    with RegistryStore(tmp_path) as store:
        agent = store.register_agent(
            username="bob", mode="simulation", now_ms=NOW, agent_id="a1", key_id="k1"
        )
        assert agent.agent_id == "a1"
        assert agent.plaintext_sim_key is not None
        assert SIMKEY_RE.match(agent.plaintext_sim_key)
        assert agent.api_key_tail is None
        rows = store.list_agents()
        assert rows[0]["agent_id"] == "a1"
        assert rows[0]["plaintext_sim_key"] is None
        keys = cast(list[RegistryKey], rows[0]["keys"])
        assert len(keys) == 1
        assert keys[0].key_hash == sha256_hex(agent.plaintext_sim_key)
        assert keys[0].key_id == "k1"


def test_register_production_requires_and_stores_tail(tmp_path: Path) -> None:
    with RegistryStore(tmp_path) as store:
        agent = store.register_agent(
            username="alice", mode="production", api_key_tail="abcd1234", now_ms=NOW, agent_id="a2"
        )
        assert agent.api_key_tail == "abcd1234"
        assert agent.plaintext_sim_key is None
        assert store.list_agents()[0]["api_key_tail"] == "abcd1234"


def test_register_production_without_tail_raises(tmp_path: Path) -> None:
    with RegistryStore(tmp_path) as store, pytest.raises(CommandCenterError, match="api_key_tail"):
        store.register_agent(username="alice", mode="production", now_ms=NOW)


def test_register_empty_username_raises(tmp_path: Path) -> None:
    with RegistryStore(tmp_path) as store, pytest.raises(CommandCenterError, match="username"):
        store.register_agent(username="   ", mode="simulation", now_ms=NOW)


def test_register_invalid_mode_raises(tmp_path: Path) -> None:
    with RegistryStore(tmp_path) as store, pytest.raises(CommandCenterError, match="mode"):
        store.register_agent(username="x", mode=cast(AgentMode, "bogus"), now_ms=NOW)


def test_issue_key_reissues_sim_key(tmp_path: Path) -> None:
    with RegistryStore(tmp_path) as store:
        store.register_agent(username="bob", mode="simulation", now_ms=NOW, agent_id="a1")
        first = store.issue_key("a1", now_ms=NOW + 1)
        assert first is not None and first.plaintext_sim_key is not None
        second = store.issue_key("a1", now_ms=NOW + 2)
        assert second is not None and second.plaintext_sim_key != first.plaintext_sim_key
        agents = store.list_agents()
        assert len(cast(list[RegistryKey], agents[0]["keys"])) == 3


def test_issue_key_rejects_production_unknown_revoked(tmp_path: Path) -> None:
    with RegistryStore(tmp_path) as store:
        store.register_agent(
            username="alice", mode="production", api_key_tail="t", now_ms=NOW, agent_id="p1"
        )
        assert store.issue_key("p1", now_ms=NOW) is None
        assert store.issue_key("missing", now_ms=NOW) is None
        store.register_agent(username="bob", mode="simulation", now_ms=NOW, agent_id="s1")
        store.revoke_agent("s1", now_ms=NOW)
        assert store.issue_key("s1", now_ms=NOW) is None


def test_verify_sim_key_round_trip(tmp_path: Path) -> None:
    with RegistryStore(tmp_path) as store:
        agent = store.register_agent(username="bob", mode="simulation", now_ms=NOW, agent_id="a1")
        assert agent.plaintext_sim_key is not None
        assert store.verify_sim_key(agent.plaintext_sim_key) == "a1"
        assert store.verify_sim_key("simkey-" + "0" * 24) is None
        assert store.verify_sim_key("not-a-simkey") is None


def test_verify_sim_key_after_revoke_returns_none(tmp_path: Path) -> None:
    with RegistryStore(tmp_path) as store:
        agent = store.register_agent(username="bob", mode="simulation", now_ms=NOW, agent_id="a1")
        assert agent.plaintext_sim_key is not None
        store.revoke_agent("a1", now_ms=NOW)
        assert store.verify_sim_key(agent.plaintext_sim_key) is None


def test_revoke_is_soft_delete(tmp_path: Path) -> None:
    with RegistryStore(tmp_path) as store:
        store.register_agent(
            username="alice", mode="production", api_key_tail="t", now_ms=NOW, agent_id="p1"
        )
        revoked = store.revoke_agent("p1", now_ms=NOW)
        assert revoked is not None and revoked.revoked_at == "2025-07-08T18:40:00.000Z"
        rows = store.list_agents()
        assert rows[0]["revoked_at"] is not None
        assert store.revoke_agent("missing", now_ms=NOW) is None


def test_list_agents_orders_and_groups(tmp_path: Path) -> None:
    with RegistryStore(tmp_path) as store:
        store.register_agent(username="b", mode="simulation", now_ms=NOW, agent_id="a2")
        store.register_agent(username="a", mode="simulation", now_ms=NOW, agent_id="a1")
        agents = store.list_agents()
        assert [a["agent_id"] for a in agents] == ["a2", "a1"]
        assert all(a["plaintext_sim_key"] is None for a in agents)


def test_second_live_writer_fails_loudly(tmp_path: Path) -> None:
    first = RegistryStore(tmp_path)
    try:
        with pytest.raises(CommandCenterError, match="another active recorder"):
            RegistryStore(tmp_path)
    finally:
        first.close()
    # After close the target is free again.
    with RegistryStore(tmp_path) as reopened:
        assert reopened.path.exists()


def test_generate_sim_key_shape() -> None:
    assert SIMKEY_RE.match(generate_sim_key())
    assert generate_sim_key().startswith(SIMKEY_PREFIX)


def test_sha256_hex_matches_reference_vector() -> None:
    import hashlib

    vector = "simkey-0123456789abcdef"
    assert sha256_hex(vector) == hashlib.sha256(vector.encode("utf-8")).hexdigest()
    assert len(sha256_hex(vector)) == 64
