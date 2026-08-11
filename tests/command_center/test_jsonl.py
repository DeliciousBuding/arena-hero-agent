"""JSONL data base semantics (legacy fs-jsonl.ts port)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

from arena_hero_agent.command_center import (
    RUN_CACHE_TTL_MS,
    CommandCenterError,
    TtlCache,
    append_jsonl,
    latest_run_dir,
    list_cases,
    load_jsonl_rows,
    parse_tick,
    read_jsonl_tail,
    runs_by_max_tick,
)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_read_jsonl_tail_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_jsonl_tail(tmp_path / "missing.jsonl", 10) == []


def test_read_jsonl_tail_empty_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "e.jsonl"
    path.write_text("", encoding="utf-8")
    assert read_jsonl_tail(path, 10) == []


def test_read_jsonl_tail_returns_last_n_rows(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_jsonl(path, [{"tick": i, "value": f"row-{i}"} for i in range(10)])
    rows = read_jsonl_tail(path, 3)
    assert [row["tick"] for row in rows] == [7, 8, 9]


def test_read_jsonl_tail_max_lines_zero_returns_all(tmp_path: Path) -> None:
    # TS lines.slice(-0) === slice(0) === all rows.
    path = tmp_path / "t.jsonl"
    _write_jsonl(path, [{"tick": i} for i in range(5)])
    rows = read_jsonl_tail(path, 0)
    assert [row["tick"] for row in rows] == [0, 1, 2, 3, 4]


def test_read_jsonl_tail_skips_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"tick": 1}\n{broken}\n{"tick": 2}\n', encoding="utf-8")
    rows = read_jsonl_tail(path, 100)
    assert [row["tick"] for row in rows] == [1, 2]


def test_read_jsonl_tail_handles_crlf(tmp_path: Path) -> None:
    path = tmp_path / "crlf.jsonl"
    path.write_text('{"tick": 1}\r\n{"tick": 2}\r\n', encoding="utf-8")
    assert [row["tick"] for row in read_jsonl_tail(path, 10)] == [1, 2]


def test_read_jsonl_tail_drops_partial_first_line_in_window(tmp_path: Path) -> None:
    # A single >64KiB line plus a short tail line: the 64KiB tail window starts
    # mid-line, so the partial first line must be discarded (oracle behavior).
    long_value = json.dumps({"pad": "x" * (70 * 1024)})
    path = tmp_path / "t.jsonl"
    path.write_text(f'{long_value}\n{{"tick": 9}}\n', encoding="utf-8")
    rows = read_jsonl_tail(path, 1)
    assert [row["tick"] for row in rows] == [9]


def test_read_jsonl_tail_non_object_row_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"tick": 1}\n[1, 2, 3]\n', encoding="utf-8")
    with pytest.raises(CommandCenterError, match="non-object JSON row"):
        read_jsonl_tail(path, 10)


@pytest.mark.parametrize("bad", [True, -1, 1.5])
def test_read_jsonl_tail_invalid_max_lines_raises(bad: object, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_jsonl(path, [{"a": 1}])
    with pytest.raises(CommandCenterError, match="max_lines"):
        read_jsonl_tail(path, cast(int, bad))


def test_load_jsonl_rows_full_and_windowed(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_jsonl(path, [{"tick": i} for i in range(10)])
    assert len(load_jsonl_rows(path)) == 10
    assert [row["tick"] for row in load_jsonl_rows(path, max_lines=2)] == [8, 9]


def test_load_jsonl_rows_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_jsonl_rows(tmp_path / "missing.jsonl") == []


def test_append_jsonl_writes_one_object_per_line(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "t.jsonl"
    append_jsonl(path, {"a": 1, "b": [1, 2]})
    append_jsonl(path, {"c": "中文"})
    assert [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == [
        {"a": 1, "b": [1, 2]},
        {"c": "中文"},
    ]


def test_append_jsonl_rejects_directory_target(tmp_path: Path) -> None:
    target = tmp_path / "dir"
    target.mkdir()
    with pytest.raises(CommandCenterError, match="directory"):
        append_jsonl(target, {"a": 1})


def test_append_jsonl_rejects_non_object(tmp_path: Path) -> None:
    with pytest.raises(CommandCenterError, match="JSON object"):
        append_jsonl(tmp_path / "t.jsonl", cast(dict[str, object], [1, 2]))


def test_parse_tick() -> None:
    assert parse_tick("12345-case.json") == 12345
    assert parse_tick("0007-foo.json") == 7
    assert parse_tick("no-tick.json") == 0
    assert parse_tick("") == 0


def _make_run_layout(tmp_path: Path, tenant: str, runs: dict[str, list[str]]) -> None:
    for run, cases in runs.items():
        cases_dir = tmp_path / "runtime" / tenant / "calibration" / run / "cases"
        cases_dir.mkdir(parents=True, exist_ok=True)
        for case in cases:
            (cases_dir / case).write_text('{"tick": 1}', encoding="utf-8")
    os.utime(tmp_path / "runtime" / tenant / "calibration", (1, 1))


def test_latest_run_dir_selects_newest_run_with_cases(tmp_path: Path) -> None:
    _make_run_layout(tmp_path, "t1", {"old-run": ["1.json"], "new-run": ["2.json"]})
    assert latest_run_dir(tmp_path, "t1") == "new-run"


def test_latest_run_dir_skips_runs_without_cases(tmp_path: Path) -> None:
    _make_run_layout(tmp_path, "t1", {"empty-run": []})
    assert latest_run_dir(tmp_path, "t1") is None


def test_latest_run_dir_missing_calibration_returns_none(tmp_path: Path) -> None:
    assert latest_run_dir(tmp_path, "t1") is None


def test_latest_run_dir_memoizes_within_ttl(tmp_path: Path) -> None:
    clock = _FakeClock()
    cache = TtlCache[str | None](RUN_CACHE_TTL_MS, clock=clock)
    _make_run_layout(tmp_path, "t1", {"run-a": ["1.json"]})
    assert latest_run_dir(tmp_path, "t1", cache=cache) == "run-a"
    # New run appears but the memo still returns the cached value inside TTL.
    _make_run_layout(tmp_path, "t1", {"run-b": ["1.json"]})
    # latest_run_dir orders runs by directory mtime; near-simultaneous creation
    # can tie on coarse filesystem clocks, so make run-b deterministically newer.
    run_b = tmp_path / "runtime" / "t1" / "calibration" / "run-b"
    newer = os.stat(run_b).st_mtime + 60
    os.utime(run_b, (newer, newer))
    assert latest_run_dir(tmp_path, "t1", cache=cache) == "run-a"
    clock.now = (RUN_CACHE_TTL_MS / 1000.0) + 0.001
    assert latest_run_dir(tmp_path, "t1", cache=cache) == "run-b"


def test_list_cases_sorted_json_only(tmp_path: Path) -> None:
    cases_dir = tmp_path / "runtime" / "t1" / "calibration" / "run-1" / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    (cases_dir / "10.json").write_text("{}", encoding="utf-8")
    (cases_dir / "2.json").write_text("{}", encoding="utf-8")
    (cases_dir / "notes.txt").write_text("x", encoding="utf-8")
    assert list_cases(tmp_path, "t1", "run-1") == ["10.json", "2.json"]


def test_list_cases_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert list_cases(tmp_path, "t1", "nope") == []


def test_runs_by_max_tick_descending(tmp_path: Path) -> None:
    _make_run_layout(tmp_path, "t1", {"run-a": ["5.json", "3.json"], "run-b": ["9.json"]})
    assert runs_by_max_tick(tmp_path, "t1") == [
        {"run": "run-b", "maxTick": 9},
        {"run": "run-a", "maxTick": 5},
    ]


def test_runs_by_max_tick_missing_returns_empty(tmp_path: Path) -> None:
    assert runs_by_max_tick(tmp_path, "t1") == []


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now
