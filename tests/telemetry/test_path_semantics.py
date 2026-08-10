"""Path semantics, escape, nested-directory, torn-tail, and rotation stress tests.

These tests cover the Windows/filesystem hardening surface that the Python-first
spec calls out explicitly: traversal and symlink escape, nested directories,
torn-tail recovery, concurrent appends under rotation, rotation retention, and
Windows path normalization. No test opens a network connection or touches the
SDK.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest

from arena_hero_agent.telemetry import (
    JsonlRotationOptions,
    JsonlWriter,
    JsonlWriterError,
    runtime_trace,
)

RT = {
    "tick": 1000,
    "runId": "run-1",
    "deadlineOutcome": "candidate",
    "agentLatencyMs": 100,
    "selectionLatencyMs": 150,
    "abortRequested": False,
    "rotationGeneration": 0,
    "submitResult": "accepted",
}


def _record(tick: int, run_id: str) -> dict[str, object]:
    return {**RT, "tick": tick, "runId": run_id}


def _rows(path: str) -> list[dict[str, object]]:
    if not Path(path).exists():
        return []
    text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _read_all(path: str, max_backups: int = 3) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(max_backups, 0, -1):
        rows.extend(_rows(f"{path}.{index}"))
    rows.extend(_rows(path))
    return rows


# ---------------------------------------------------------------------------
# Path escape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "escape",
    [
        "..\\escape.jsonl",
        "a\\..\\..\\escape.jsonl",
        "a/../../escape.jsonl",
        "a\\b/../c.jsonl",
        "..",
    ],
)
def test_traversal_components_rejected(escape: str, tmp_path: Path) -> None:
    candidate = str(tmp_path / escape)
    with pytest.raises(JsonlWriterError):
        JsonlWriter(candidate)


def test_nul_byte_rejected(tmp_path: Path) -> None:
    with pytest.raises(JsonlWriterError, match="NUL"):
        JsonlWriter(str(tmp_path / "bad\x00name.jsonl"))


def test_empty_and_root_only_paths_rejected(tmp_path: Path) -> None:
    with pytest.raises(JsonlWriterError):
        JsonlWriter("")
    with pytest.raises(JsonlWriterError):
        JsonlWriter("\\")
    with pytest.raises(JsonlWriterError):
        JsonlWriter(os.fspath(tmp_path) + "\\" + "..")


def test_non_string_path_rejected() -> None:
    with pytest.raises(JsonlWriterError):
        JsonlWriter(cast(Any, 1234))


# ---------------------------------------------------------------------------
# Nested directories
# ---------------------------------------------------------------------------


def test_nested_directories_write_when_parents_exist(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    path = str(nested / "traces.jsonl")
    writer = JsonlWriter(path)
    writer.write(runtime_trace(_record(1, "n-1")))
    writer.write(runtime_trace(_record(2, "n-2")))
    writer.close()
    assert [row["tick"] for row in _rows(path)] == [1, 2]


def test_missing_parent_dir_counts_as_drop_not_silent_mkdir(tmp_path: Path) -> None:
    # Matches TypeScript: appendFileSync on a missing parent raises ENOENT,
    # which JsonlWriter counts as a best-effort drop without creating dirs.
    path = str(tmp_path / "missing" / "deep" / "traces.jsonl")
    writer = JsonlWriter(path)
    writer.write(runtime_trace(RT))
    assert writer.dropped_count == 1
    assert writer.last_error is not None
    assert not (tmp_path / "missing").exists()
    writer.close()


# ---------------------------------------------------------------------------
# Windows path semantics
# ---------------------------------------------------------------------------


def test_forward_and_backslash_refer_to_same_file(tmp_path: Path) -> None:
    # The in-process single-writer registry keys on the resolved path, so
    # both spellings must be recognized as the same writer target.
    first = JsonlWriter(str(tmp_path / "same.jsonl"))
    try:
        with pytest.raises(JsonlWriterError, match="another active"):
            JsonlWriter(
                str(tmp_path / "same" / ".." / "same.jsonl")
                if False
                else (str(tmp_path) + "/same.jsonl")
            )
    finally:
        first.close()


def test_dot_component_is_allowed(tmp_path: Path) -> None:
    path = os.path.join(str(tmp_path), ".", "dot.jsonl")
    writer = JsonlWriter(path)
    writer.write(runtime_trace(RT))
    writer.close()
    assert len(_rows(path)) == 1


def test_pathlike_object_accepted(tmp_path: Path) -> None:
    writer = JsonlWriter(tmp_path / "obj.jsonl")
    writer.write(runtime_trace(RT))
    writer.close()
    assert len(_rows(str(tmp_path / "obj.jsonl"))) == 1


def test_drive_relative_and_root_relative_construct_and_dedupe() -> None:
    first = JsonlWriter("telemetry-drive-rel.jsonl")
    try:
        with pytest.raises(JsonlWriterError, match="another active"):
            JsonlWriter("telemetry-drive-rel.jsonl")
    finally:
        first.close()
    second = JsonlWriter("\\telemetry-root-rel.jsonl")
    try:
        with pytest.raises(JsonlWriterError, match="another active"):
            JsonlWriter("\\telemetry-root-rel.jsonl")
    finally:
        second.close()


# ---------------------------------------------------------------------------
# Torn-tail recovery
# ---------------------------------------------------------------------------


def test_torn_tail_with_no_newline_recovers_to_empty_then_appends(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    path.write_text('{"tick":999,"runId":"torn"', encoding="utf-8")
    writer = JsonlWriter(str(path), recover_torn_tail=True)
    writer.write(runtime_trace(RT))
    writer.close()
    rows = _read_all(str(path))
    assert [row["tick"] for row in rows] == [1000]
    assert writer.dropped_count == 1


def test_torn_tail_multi_line_recovers_last_complete_line(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    path.write_text(
        '{"tick":1,"runId":"a"}\n{"tick":2,"runId":"b"}\n{"tick":3,"runId":"c"', encoding="utf-8"
    )
    writer = JsonlWriter(str(path), recover_torn_tail=True)
    writer.write(runtime_trace(_record(1000, "after")))
    writer.close()
    rows = _read_all(str(path))
    assert [row["tick"] for row in rows] == [1, 2, 1000]
    assert writer.dropped_count == 1


# ---------------------------------------------------------------------------
# Concurrent append (with and without rotation)
# ---------------------------------------------------------------------------


def test_concurrent_appends_all_records_present(tmp_path: Path) -> None:
    path = str(tmp_path / "conc.jsonl")
    writer = JsonlWriter(path)
    count = 128
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda i: writer.write(runtime_trace(_record(i, f"c-{i}"))), range(count)))
    writer.close()
    rows = _read_all(path)
    assert len(rows) == count
    assert {row["tick"] for row in rows} == set(range(count))
    assert len({row["runId"] for row in rows}) == count


def test_concurrent_appends_with_rotation_preserve_newest_suffix(tmp_path: Path) -> None:
    # maxBytes < one record: every append rotates, retaining max_backups + 1
    # complete files (bounded retention, no torn lines, newest suffix only).
    path = str(tmp_path / "conc-rot.jsonl")
    writer = JsonlWriter(path, JsonlRotationOptions(max_bytes=16, max_backups=3))
    count = 32
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: writer.write(runtime_trace(_record(i, f"r-{i}"))), range(count)))
    writer.close()
    rows = _read_all(path, max_backups=3)
    assert [row["runId"] for row in rows] == [f"r-{i}" for i in range(count - 4, count)]
    for row in rows:
        assert set(row.keys()) >= {"tick", "runId"}  # every row is complete JSON


# ---------------------------------------------------------------------------
# Rotation retention
# ---------------------------------------------------------------------------


def test_rotation_max_backups_zero_keeps_only_active(tmp_path: Path) -> None:
    path = str(tmp_path / "zero.jsonl")
    writer = JsonlWriter(path, JsonlRotationOptions(max_bytes=16, max_backups=0))
    writer.write(runtime_trace(_record(1, "z-0")))
    writer.write(runtime_trace(_record(2, "z-1")))
    writer.close()
    assert [row["runId"] for row in _rows(path)] == ["z-1"]
    assert not Path(f"{path}.1").exists()


def test_rotation_shifts_all_backups_and_never_exceeds(tmp_path: Path) -> None:
    path = str(tmp_path / "shift.jsonl")
    writer = JsonlWriter(path, JsonlRotationOptions(max_bytes=16, max_backups=3))
    for index in range(8):
        writer.write(runtime_trace(_record(index, f"s-{index}")))
    writer.close()
    assert [row["runId"] for row in _read_all(path, max_backups=3)] == [
        f"s-{i}" for i in range(4, 8)
    ]
    assert not Path(f"{path}.4").exists()
