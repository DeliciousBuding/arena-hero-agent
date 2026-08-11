"""Survey database base semantics (agent-ingest data layer port)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from arena_hero_agent.command_center import (
    AgentEventKind,
    AgentIngestEvent,
    CommandCenterError,
    SurveyDb,
)


def _event(
    tenant: str = "t1",
    ts: float = 1.0,
    event: AgentEventKind = "register",
    instance: str | None = "i1",
    tick: int | None = None,
    resources: int | None = None,
    mode: str | None = None,
    resource_cells: list[tuple[int, int]] | None = None,
    obstacle_cells: list[tuple[int, int]] | None = None,
    units_seen: list[tuple[object, ...]] | None = None,
    enemy_cores: list[tuple[int, int, str]] | None = None,
) -> AgentIngestEvent:
    return AgentIngestEvent(
        tenant=tenant,
        ts=ts,
        event=event,
        instance=instance,
        tick=tick,
        resources=resources,
        mode=mode,
        resource_cells=resource_cells,
        obstacle_cells=obstacle_cells,
        units_seen=units_seen,
        enemy_cores=enemy_cores,
    )


def _survey_path(tmp_path: Path, tenant: str = "t1") -> Path:
    return tmp_path / "runtime" / "survey" / f"{tenant}.db"


def test_write_open_creates_schema_tables(tmp_path: Path) -> None:
    with SurveyDb(tmp_path, "t1", write=True) as db:
        connection = sqlite3.connect(db.path)
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert {
                "agents",
                "agent_events",
                "resources",
                "obstacles",
                "core_hunts",
                "units_seen",
                "resource_seen_history",
            } <= tables
            pk = [
                row[2]
                for row in connection.execute("PRAGMA index_info('sqlite_autoindex_agents_1')")
            ]
            assert set(pk) == {"tenant", "instance"}
        finally:
            connection.close()


def test_read_open_missing_file_is_unavailable(tmp_path: Path) -> None:
    with SurveyDb(tmp_path, "t1", write=False) as db:
        assert not db.is_available
        assert db.known_agent() is None
        assert db.recent_agent_events() == []


def test_read_open_available_after_write(tmp_path: Path) -> None:
    with SurveyDb(tmp_path, "t1", write=True) as db:
        db.apply_agent_event(_event())
    with SurveyDb(tmp_path, "t1", write=False) as db:
        assert db.is_available
        agent = db.known_agent()
        assert agent is not None
        assert agent.instance == "i1"
        assert agent.mode == "production"


def test_apply_agent_event_upserts_and_appends(tmp_path: Path) -> None:
    with SurveyDb(tmp_path, "t1", write=True) as db:
        db.apply_agent_event(_event(event="register", ts=1.0))
        db.apply_agent_event(_event(event="tick_summary", ts=2.0, tick=5, resources=3))
        db.apply_agent_event(_event(event="tick_summary", ts=3.0, tick=6, resources=4))
        agent = db.known_agent()
        assert agent is not None
        assert (agent.tick, agent.resources) == (6, 4)
        events = db.recent_agent_events(10)
        assert [e["event"] for e in events] == ["tick_summary", "tick_summary", "register"]
        assert events[0]["tick"] == 6


def test_apply_agent_event_mode_preserved_on_later_events(tmp_path: Path) -> None:
    with SurveyDb(tmp_path, "t1", write=True) as db:
        db.apply_agent_event(_event(event="register", mode="simulation"))
        db.apply_agent_event(_event(event="tick_summary", ts=2.0, tick=1))
        agent = db.known_agent()
        assert agent is not None and agent.mode == "simulation"


def test_tick_summary_mapping_upserts(tmp_path: Path) -> None:
    with SurveyDb(tmp_path, "t1", write=True) as db:
        db.apply_agent_event(
            _event(
                event="tick_summary",
                ts=1.0,
                tick=10,
                resource_cells=[(1, 2), (1, 2)],
                obstacle_cells=[(3, 4)],
                units_seen=[("e1", "WORKER", 0, 7, 8, 100), ("friend", "WORKER", 1, 9, 9, 100)],
                enemy_cores=[(10, 10, "other")],
            )
        )
        db.apply_agent_event(
            _event(
                event="tick_summary",
                ts=2.0,
                tick=11,
                resource_cells=[(1, 2)],
                enemy_cores=[(10, 10, "other")],
            )
        )
        connection = sqlite3.connect(db.path)
        try:
            resources = connection.execute(
                "SELECT cell, last_seen_tick, seen_count FROM resources WHERE cell='1,2'"
            ).fetchone()
            # The same cell appears twice in the first event plus once in the second.
            assert resources == ("1,2", 11, 3)
            obstacles = connection.execute(
                "SELECT cell, last_seen_tick FROM obstacles WHERE cell='3,4'"
            ).fetchone()
            assert obstacles == ("3,4", 10)
            history = connection.execute(
                "SELECT COUNT(*) FROM resource_seen_history WHERE cell='1,2'"
            ).fetchone()[0]
            assert history == 2
            # controlled=1 rows are excluded from units_seen.
            unit_rows = connection.execute("SELECT COUNT(*) FROM units_seen").fetchone()[0]
            assert unit_rows == 1
            core = connection.execute(
                "SELECT cell, owner, first_seen_tick, last_seen_tick FROM core_hunts "
                "WHERE cell='10,10'"
            ).fetchone()
            assert core == ("10,10", "other", 10, 11)
        finally:
            connection.close()


def test_disconnected_event_marks_connection_down(tmp_path: Path) -> None:
    with SurveyDb(tmp_path, "t1", write=True) as db:
        db.apply_agent_event(_event(event="tick_summary", ts=1.0, tick=1))
        db.apply_agent_event(_event(event="disconnected", ts=2.0))
        agent = db.known_agent()
        assert agent is not None and agent.connection_state == "down"


def test_invalid_event_kind_raises(tmp_path: Path) -> None:
    with (
        SurveyDb(tmp_path, "t1", write=True) as db,
        pytest.raises(CommandCenterError, match="event kind"),
    ):
        db.apply_agent_event(_event(event=cast(AgentEventKind, "bogus")))


def test_invalid_mode_raises(tmp_path: Path) -> None:
    with (
        SurveyDb(tmp_path, "t1", write=True) as db,
        pytest.raises(CommandCenterError, match="mode"),
    ):
        db.apply_agent_event(_event(mode="bogus"))


def test_write_on_read_handle_raises(tmp_path: Path) -> None:
    with SurveyDb(tmp_path, "t1", write=True) as db:
        db.apply_agent_event(_event())
    with (
        SurveyDb(tmp_path, "t1", write=False) as db,
        pytest.raises(CommandCenterError, match="write handle"),
    ):
        db.apply_agent_event(_event())


def test_write_open_rejects_legacy_single_pk_schema(tmp_path: Path) -> None:
    path = _survey_path(tmp_path)
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE agents (tenant TEXT PRIMARY KEY, instance TEXT NOT NULL);"
    )
    connection.commit()
    connection.close()
    with pytest.raises(CommandCenterError, match="legacy single-column agents primary key"):
        SurveyDb(tmp_path, "t1", write=True)


def test_sim_tenant_namespace(tmp_path: Path) -> None:
    with SurveyDb(tmp_path, "sim-burn-in", write=True) as db:
        db.apply_agent_event(_event(tenant="sim-burn-in", event="register"))
        assert db.known_agent() is not None


def test_invalid_survey_tenant_raises(tmp_path: Path) -> None:
    with pytest.raises(CommandCenterError, match="not a survey tenant"):
        SurveyDb(tmp_path, "bogus", write=True)
