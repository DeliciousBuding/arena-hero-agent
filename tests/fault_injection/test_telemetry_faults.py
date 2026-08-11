"""Telemetry JsonlWriter fault injection: disk-full IOError + torn tail.

Disk-full is injected as an ``OSError`` at the exact IO boundary the writer
already guards (``append_jsonl_line``) — chosen over a read-only directory
because a read-only directory is unreliable on Windows (the attribute does not
block writes to existing files and admin bypasses it). The writer must drop the
record without raising (decision path stays open) and recover once the fault
clears. A torn tail (half-line) fails closed without recovery and truncates
with it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arena_hero_agent.telemetry import JsonlWriter, TornTailError, runtime_trace

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


def _read_rows(path: str) -> list[dict[str, object]]:
    if not Path(path).exists():
        return []
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_disk_full_ioerror_dropped_not_raised_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOSPC at the append boundary drops one record and never blocks the path."""
    import arena_hero_agent.telemetry.jsonl_writer as writer_module

    path = str(tmp_path / "traces.jsonl")
    writer = JsonlWriter(path)
    original = writer_module.append_jsonl_line

    def _enospc(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(writer_module, "append_jsonl_line", _enospc)
    writer.write(runtime_trace(RT))
    assert writer.dropped_count == 1
    assert isinstance(writer.last_error, OSError)

    # The fault clears: the next record lands and is not counted as a drop.
    monkeypatch.setattr(writer_module, "append_jsonl_line", original)
    writer.write(runtime_trace({**RT, "tick": 2000}))
    assert writer.dropped_count == 1
    rows = _read_rows(path)
    assert len(rows) == 1
    assert rows[0]["tick"] == 2000
    writer.close()


def test_torn_tail_half_line_fails_closed_without_recovery(tmp_path: Path) -> None:
    """A half-written trailing line is detected and refuses to append."""
    path = tmp_path / "traces.jsonl"
    torn = '{"tick":1}\n{"tick":2,"partial":'
    path.write_text(torn, encoding="utf-8")
    writer = JsonlWriter(str(path))
    with pytest.raises(TornTailError, match="torn tail"):
        writer.write(runtime_trace(RT))
    # Fail-closed: the torn file is left untouched for forensics.
    assert path.read_text(encoding="utf-8") == torn
    writer.close()


def test_torn_tail_half_line_recovers_with_policy(tmp_path: Path) -> None:
    """Explicit recovery truncates to the last complete line and counts the drop."""
    path = tmp_path / "traces.jsonl"
    path.write_text('{"tick":1}\n{"tick":2}\n{"tick":3,', encoding="utf-8")
    writer = JsonlWriter(str(path), recover_torn_tail=True)
    writer.write(runtime_trace(RT))
    writer.close()
    rows = _read_rows(str(path))
    assert [row["tick"] for row in rows] == [1, 2, 1000]
    assert writer.dropped_count == 1
