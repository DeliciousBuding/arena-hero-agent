"""Human command store semantics (legacy store.ts port)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arena_hero_agent.command_center import (
    STUCK_TICKS,
    CommandCenterError,
    GoalEntry,
    HumanCommand,
    cancel_stuck_goals,
    empty_store,
    iso_utc,
    latest_human_override,
    read_human_store,
    reconcile_human_store,
    stuck_record,
    write_human_store,
)

NOW = 1_752_000_000_000


def _store_file(root: Path, tenant: str = "t1") -> Path:
    return root / "runtime" / "human-commands" / f"{tenant}.json"


def _goal(unit_id: str = "u1", goal_id: str = "g1", target: tuple[int, int] = (5, 5)) -> GoalEntry:
    return GoalEntry(
        id=goal_id,
        unit_id=unit_id,
        kind="goto",
        target=target,
        created_at=iso_utc(NOW),
    )


def _command(unit_id: str = "u2", command_id: str = "c1") -> HumanCommand:
    return HumanCommand(
        id=command_id,
        unit_id=unit_id,
        action={"type": "MOVE", "to": [1, 1]},
        created_at=iso_utc(NOW),
    )


def test_read_missing_file_returns_empty_store(tmp_path: Path) -> None:
    store = read_human_store(tmp_path, "t1")
    assert store.tenant == "t1"
    assert store.version == 1
    assert store.mode == "override"
    assert store.commands == []
    assert store.goals == []
    assert store.updated_at is None


def test_read_corrupt_json_returns_empty_store(tmp_path: Path) -> None:
    file = _store_file(tmp_path)
    file.parent.mkdir(parents=True)
    file.write_text("{not json", encoding="utf-8")
    store = read_human_store(tmp_path, "t1")
    assert store.commands == [] and store.goals == []


def test_read_non_object_json_returns_empty_store(tmp_path: Path) -> None:
    file = _store_file(tmp_path)
    file.parent.mkdir(parents=True)
    file.write_text("[1, 2]", encoding="utf-8")
    assert read_human_store(tmp_path, "t1").commands == []


def test_write_read_round_trip_preserves_fields(tmp_path: Path) -> None:
    store = empty_store("t1")
    store.goals.append(_goal())
    store.commands.append(_command())
    persisted = write_human_store(tmp_path, "t1", store, now_ms=NOW)
    assert persisted.tenant is None
    assert persisted.version == 1
    assert persisted.updated_at == iso_utc(NOW)
    raw = json.loads(_store_file(tmp_path).read_text(encoding="utf-8"))
    assert list(raw) == ["version", "mode", "commands", "goals", "updatedAt"]
    assert raw["version"] == 1
    assert raw["mode"] == "override"
    assert raw["commands"][0] == {
        "id": "c1",
        "unitId": "u2",
        "action": {"type": "MOVE", "to": [1, 1]},
        "createdAt": iso_utc(NOW),
    }
    assert raw["goals"][0] == {
        "id": "g1",
        "unitId": "u1",
        "kind": "goto",
        "target": [5, 5],
        "createdAt": iso_utc(NOW),
    }
    assert raw["updatedAt"] == iso_utc(NOW)
    back = read_human_store(tmp_path, "t1")
    assert back.goals[0].target == (5, 5)
    assert back.commands[0].unit_id == "u2"
    assert back.updated_at == iso_utc(NOW)


def test_write_omits_note_when_absent_and_keeps_when_present(tmp_path: Path) -> None:
    store = empty_store("t1")
    store.goals.append(_goal())
    store.goals.append(
        GoalEntry(
            id="g2", unit_id="u9", kind="mine", target=(1, 1), created_at=iso_utc(NOW), note="note!"
        )
    )
    write_human_store(tmp_path, "t1", store, now_ms=NOW)
    raw = json.loads(_store_file(tmp_path).read_text(encoding="utf-8"))
    assert "note" not in raw["goals"][0]
    assert raw["goals"][1]["note"] == "note!"


def test_write_is_atomic_and_cleans_temp(tmp_path: Path) -> None:
    store = empty_store("t1")
    write_human_store(tmp_path, "t1", store, now_ms=NOW)
    leftovers = [p for p in _store_file(tmp_path).parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    assert _store_file(tmp_path).is_file()


def test_read_tolerates_ts_defaults(tmp_path: Path) -> None:
    file = _store_file(tmp_path)
    file.parent.mkdir(parents=True)
    file.write_text(
        json.dumps({"mode": "disabled", "commands": [], "goals": [], "updatedAt": None}),
        encoding="utf-8",
    )
    store = read_human_store(tmp_path, "t1")
    assert store.version == 1
    assert store.mode == "disabled"
    assert store.updated_at is None


def test_read_rejects_malformed_command(tmp_path: Path) -> None:
    file = _store_file(tmp_path)
    file.parent.mkdir(parents=True)
    file.write_text(
        json.dumps({"version": 1, "mode": "override", "commands": [{"id": "x"}], "goals": []}),
        encoding="utf-8",
    )
    with pytest.raises(CommandCenterError, match="field 'unitId'"):
        read_human_store(tmp_path, "t1")


def test_read_rejects_malformed_goal(tmp_path: Path) -> None:
    file = _store_file(tmp_path)
    file.parent.mkdir(parents=True)
    file.write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "override",
                "commands": [],
                "goals": [{"id": "g", "unitId": "u", "kind": "nope", "target": [1, 1]}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CommandCenterError, match="goal kind"):
        read_human_store(tmp_path, "t1")


def test_read_rejects_non_integer_version(tmp_path: Path) -> None:
    file = _store_file(tmp_path)
    file.parent.mkdir(parents=True)
    file.write_text(
        json.dumps({"version": "abc", "mode": "override", "commands": [], "goals": []}),
        encoding="utf-8",
    )
    with pytest.raises(CommandCenterError, match="version"):
        read_human_store(tmp_path, "t1")


def test_latest_human_override_missing_file_returns_none(tmp_path: Path) -> None:
    assert latest_human_override(tmp_path, "t1") is None


def test_latest_human_override_reads_last_row(tmp_path: Path) -> None:
    outcome = tmp_path / "runtime" / "t1" / "telemetry" / "outcome.jsonl"
    outcome.parent.mkdir(parents=True)
    outcome.write_text(
        '{"tick": 1, "humanOverride": {"active": true, "applied": ["a"]}}\n'
        '{"tick": 2, "humanOverride": {"active": false, "applied": ["b"], "rejected": [], '
        '"satisfied": [], "updatedAt": "2026-08-12T00:00:00.000Z"}}\n',
        encoding="utf-8",
    )
    override = latest_human_override(tmp_path, "t1")
    assert override is not None
    assert override["tick"] == 2
    assert override["updatedAt"] == "2026-08-12T00:00:00.000Z"


def test_latest_human_override_empty_override_returns_none(tmp_path: Path) -> None:
    outcome = tmp_path / "runtime" / "t1" / "telemetry" / "outcome.jsonl"
    outcome.parent.mkdir(parents=True)
    outcome.write_text(
        '{"tick": 3, "humanOverride": {"active": false, "applied": [], "rejected": [], '
        '"satisfied": []}}\n',
        encoding="utf-8",
    )
    assert latest_human_override(tmp_path, "t1") is None


def test_latest_human_override_rejects_non_object(tmp_path: Path) -> None:
    outcome = tmp_path / "runtime" / "t1" / "telemetry" / "outcome.jsonl"
    outcome.parent.mkdir(parents=True)
    outcome.write_text('{"tick": 1, "humanOverride": "yes"}\n', encoding="utf-8")
    with pytest.raises(CommandCenterError, match="humanOverride"):
        latest_human_override(tmp_path, "t1")


def _seed_reconcile(
    root: Path,
    *,
    goals: list[GoalEntry],
    commands: list[HumanCommand],
    override: dict[str, object],
) -> None:
    store = empty_store("t1")
    store.goals = goals
    store.commands = commands
    write_human_store(root, "t1", store, now_ms=NOW)
    outcome = root / "runtime" / "t1" / "telemetry" / "outcome.jsonl"
    outcome.parent.mkdir(parents=True, exist_ok=True)
    outcome.write_text(
        json.dumps({"tick": 10, "humanOverride": override}) + "\n",
        encoding="utf-8",
    )


def test_reconcile_cleans_satisfied_goal(tmp_path: Path) -> None:
    _seed_reconcile(
        tmp_path,
        goals=[_goal()],
        commands=[],
        override={
            "active": False,
            "satisfied": ["u1"],
            "applied": [],
            "rejected": [],
            "updatedAt": iso_utc(NOW + 1000),
        },
    )
    store = reconcile_human_store(tmp_path, "t1", now_ms=NOW)
    assert store.goals == []
    assert read_human_store(tmp_path, "t1").goals == []


def test_reconcile_cleans_applied_command(tmp_path: Path) -> None:
    _seed_reconcile(
        tmp_path,
        goals=[],
        commands=[_command()],
        override={
            "active": False,
            "satisfied": [],
            "applied": ["u2"],
            "rejected": [],
            "updatedAt": iso_utc(NOW + 1000),
        },
    )
    store = reconcile_human_store(tmp_path, "t1", now_ms=NOW)
    assert store.commands == []


def test_reconcile_cleans_unknown_unit(tmp_path: Path) -> None:
    _seed_reconcile(
        tmp_path,
        goals=[_goal()],
        commands=[_command()],
        override={
            "active": False,
            "satisfied": [],
            "applied": [],
            "rejected": [
                {"unitId": "u1", "reason": "unknown_unit"},
                {"unitId": "u2", "reason": "unknown_unit"},
            ],
            "updatedAt": iso_utc(NOW + 1000),
        },
    )
    store = reconcile_human_store(tmp_path, "t1", now_ms=NOW)
    assert store.goals == [] and store.commands == []


def test_reconcile_respects_timing_guard(tmp_path: Path) -> None:
    # The override processedAt predates the command, so it must not be cleaned.
    _seed_reconcile(
        tmp_path,
        goals=[],
        commands=[_command()],
        override={
            "active": False,
            "satisfied": [],
            "applied": ["u2"],
            "rejected": [],
            "updatedAt": iso_utc(NOW - 10_000),
        },
    )
    store = reconcile_human_store(tmp_path, "t1", now_ms=NOW)
    assert len(store.commands) == 1


def test_reconcile_noop_without_changes(tmp_path: Path) -> None:
    _seed_reconcile(
        tmp_path,
        goals=[_goal()],
        commands=[],
        override={"active": True, "satisfied": [], "applied": [], "rejected": []},
    )
    store = reconcile_human_store(tmp_path, "t1", now_ms=NOW)
    assert len(store.goals) == 1


def _seed_stuck(root: Path, *, goal: GoalEntry, actions: list[str]) -> None:
    store = empty_store("t1")
    store.goals = [goal]
    write_human_store(root, "t1", store, now_ms=NOW)
    cases_dir = root / "runtime" / "t1" / "calibration" / "run-1" / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    for index, action_type in enumerate(actions):
        payload = {"plan": {"unitActions": {goal.unit_id: {"type": action_type}}}}
        (cases_dir / f"{index:06d}.json").write_text(json.dumps(payload), encoding="utf-8")
    outcome = root / "runtime" / "t1" / "telemetry" / "outcome.jsonl"
    outcome.parent.mkdir(parents=True, exist_ok=True)
    outcome.write_text(
        json.dumps(
            {
                "tick": 10,
                "humanOverride": {
                    "active": True,
                    "applied": [goal.unit_id],
                    "satisfied": [],
                    "rejected": [],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_reconcile_cancels_stuck_goal(tmp_path: Path) -> None:
    goal = _goal(goal_id="g-stuck")
    _seed_stuck(tmp_path, goal=goal, actions=["WAIT"] * STUCK_TICKS)
    store = reconcile_human_store(tmp_path, "t1", now_ms=NOW)
    assert store.goals == []
    records = stuck_record("t1")
    assert len(records) == 1
    assert records[0].unit_id == goal.unit_id
    assert "无推进" in records[0].reason


def test_reconcile_keeps_moving_goal(tmp_path: Path) -> None:
    goal = _goal(goal_id="g-move")
    _seed_stuck(
        tmp_path,
        goal=goal,
        actions=["WAIT"] * (STUCK_TICKS - 2) + ["MOVE"] + ["WAIT"] * 3,
    )
    store = reconcile_human_store(tmp_path, "t1", now_ms=NOW)
    assert len(store.goals) == 1


def test_cancel_stuck_goals_requires_enough_cases(tmp_path: Path) -> None:
    goal = _goal(goal_id="g-short")
    _seed_stuck(tmp_path, goal=goal, actions=["WAIT"] * 3)
    store = empty_store("t1")
    store.goals = [goal]
    store, stuck = cancel_stuck_goals(
        tmp_path, "t1", store, {"applied": [goal.unit_id]}, now_ms=NOW
    )
    assert stuck == []
    assert len(store.goals) == 1


def test_stuck_ring_keeps_last_six(tmp_path: Path) -> None:
    # Directly exercise the ring through repeated reconciles with distinct units.
    for index in range(8):
        goal = _goal(unit_id=f"u{index}", goal_id=f"g{index}")
        _seed_stuck(tmp_path, goal=goal, actions=["WAIT"] * STUCK_TICKS)
        reconcile_human_store(tmp_path, "t1", now_ms=NOW + index)
    records = stuck_record("t1")
    assert len(records) == 6
    assert records[-1].unit_id == "u7"
