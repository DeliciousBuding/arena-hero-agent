"""JsonlWriter, rotation, recovery, path hardening, and redaction tests."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

from arena_hero_agent.telemetry import (
    DEFAULT_JSONL_ROTATION,
    JsonlRotationOptions,
    JsonlWriter,
    JsonlWriterError,
    TornTailError,
    append_jsonl_line,
    decision_trace,
    outcome_trace,
    rotated_jsonl_paths,
    runtime_trace,
    sanitize_text,
    sanitize_value,
    to_json,
    to_json_object,
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

DT = {
    "tick": 1000,
    "runId": "run-1",
    "decisionSource": "hybrid",
    "agentActionCount": 2,
    "safetyReplacementCount": 1,
    "invalidAgentActionCount": 0,
    "repairCount": 0,
    "intentCounts": {"patrol": 2, "return_home": 1},
    "planHash": "sha256:abc",
}

OT = {
    "tick": 1000,
    "coreResourcesBefore": 5,
    "coreResourcesAfter": 7,
    "coreResourceDelta": 2,
    "uniqueWorkerCellCount": 3,
    "workerMaxDistanceFromCore": 8,
    "workerMeanDistanceFromCore": 4.5,
    "failedEvents": [
        {
            "eventType": "UNIT_MOVE_FAILED",
            "reasonCode": "blocked",
            "actorId": "w1",
            "targetId": None,
            "position": [2, 3],
            "priorAction": '{"type":"MOVE","direction":"RIGHT"}',
            "priorIntent": "return_home",
        }
    ],
    "events": ["DEPOSIT 2"],
}


def _read_rows(path: str) -> list[dict[str, object]]:
    if not Path(path).exists():
        return []
    text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_writer_round_trip_three_streams(tmp_path: Path) -> None:
    path = str(tmp_path / "traces.jsonl")
    writer = JsonlWriter(path)
    writer.write(runtime_trace(RT))
    writer.write(decision_trace(DT))
    writer.write(outcome_trace(OT))
    writer.close()
    rows = _read_rows(path)
    assert len(rows) == 3
    assert rows[0]["runId"] == "run-1"
    assert rows[1]["decisionSource"] == "hybrid"
    assert rows[2]["coreResourceDelta"] == 2


def test_writer_rotates_at_complete_line_boundaries(tmp_path: Path) -> None:
    path = str(tmp_path / "rot.jsonl")
    sample = runtime_trace({**RT, "tick": 2000, "runId": "rotation-0"})
    one_line_bytes = len(to_json(sample) + "\n")
    writer = JsonlWriter(path, JsonlRotationOptions(max_bytes=one_line_bytes + 8, max_backups=2))
    for index in range(5):
        writer.write(runtime_trace({**RT, "tick": 2000 + index, "runId": f"rotation-{index}"}))
    writer.close()

    backup1, backup2, backup3 = rotated_jsonl_paths(path, 3)
    assert Path(path).exists()
    assert Path(backup1).exists()
    assert Path(backup2).exists()
    assert not Path(backup3).exists()
    rows = _read_rows(backup2) + _read_rows(backup1) + _read_rows(path)
    assert [row["runId"] for row in rows] == ["rotation-2", "rotation-3", "rotation-4"]


def test_rotation_rejects_invalid_policy(tmp_path: Path) -> None:
    path = str(tmp_path / "x.jsonl")
    with pytest.raises(JsonlWriterError, match="maxBytes"):
        JsonlWriter(path, JsonlRotationOptions(max_bytes=0, max_backups=1))
    with pytest.raises(JsonlWriterError, match="maxBackups"):
        JsonlWriter(path, JsonlRotationOptions(max_bytes=1, max_backups=-1))


def test_rotated_jsonl_paths_validation() -> None:
    with pytest.raises(JsonlWriterError, match="maxBackups"):
        rotated_jsonl_paths("x.jsonl", -1)
    assert rotated_jsonl_paths("x.jsonl", 2) == ["x.jsonl.1", "x.jsonl.2"]


def test_redaction_api_key_authorization_token(tmp_path: Path) -> None:
    dirty = runtime_trace({**RT, "runId": "sk-abcdefghijklmnop123456"})
    text = sanitize_text(
        'Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456, key="sk-1234567890abcdef", '
        "ARENA_HERO_API_KEY_1=supersecrettoken1234567890123456"
    )
    assert "Bearer" not in text
    assert "sk-1234567890" not in text
    assert "supersecrettoken" not in text
    sanitized = cast(dict[str, object], sanitize_value(to_json_object(dirty)))
    assert sanitized["runId"] == "[REDACTED]"


def test_redaction_leaves_normal_text_alone() -> None:
    text = sanitize_text("tick=100 resources=6 population=3 plan=deposit")
    assert text == "tick=100 resources=6 population=3 plan=deposit"


def test_redaction_hash_field_whitelist() -> None:
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    record = {
        "configHash": f"sha256:{sha}",
        "strategyHash": f"sha256:{sha}",
        "planHash": "0123456789abcdef0123456789abcdef",
    }
    sanitized = cast(dict[str, object], sanitize_value(record))
    assert sanitized["configHash"] == f"sha256:{sha}"
    assert sanitized["strategyHash"] == f"sha256:{sha}"
    assert sanitized["planHash"] == "0123456789abcdef0123456789abcdef"
    assert sha in json.dumps(sanitized)
    assert "[REDACTED]" not in json.dumps(sanitized)


def test_mapping_key_redaction_is_recursive_and_semantic() -> None:
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    dirty = {
        "apiToken": "secret-value",
        "api_token": {"must": "not-survive"},
        "apiKey": ["must-not-survive"],
        "api_key": 123,
        "api-key": None,
        "Authorization": "Bearer abc",
        "nested": [
            {
                "password": "p",
                "serviceCredential": {"nested": "credential-value"},
                "clientSecret": False,
                "sessionCookie": 123,
                "accessToken": None,
                "safe": "ok",
            }
        ],
        "configHash": f"sha256:{sha}",
        "strategyHash": f"sha256:{sha}",
        "planHash": "0123456789abcdef0123456789abcdef",
        "apiTokenHash": f"sha256:{sha}",
        "hash": f"sha256:{sha}",
        "tokenCount": 17,
        "passwordPolicy": "rotate-quarterly",
    }

    sanitized = cast(dict[str, object], sanitize_value(dirty))

    for key in (
        "apiToken",
        "api_token",
        "apiKey",
        "api_key",
        "api-key",
        "Authorization",
        "apiTokenHash",
    ):
        assert sanitized[key] == "[REDACTED]"
    nested = cast(list[dict[str, object]], sanitized["nested"])
    for key in ("password", "serviceCredential", "clientSecret", "sessionCookie", "accessToken"):
        assert nested[0][key] == "[REDACTED]"
    assert nested[0]["safe"] == "ok"
    assert sanitized["configHash"] == f"sha256:{sha}"
    assert sanitized["strategyHash"] == f"sha256:{sha}"
    assert sanitized["planHash"] == "0123456789abcdef0123456789abcdef"
    assert sanitized["hash"] == f"sha256:{sha}"
    assert sanitized["tokenCount"] == 17
    assert sanitized["passwordPolicy"] == "rotate-quarterly"


def test_redaction_sha256_prefix_protection_and_bare_hex() -> None:
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    sanitized = sanitize_text(f"config=sha256:{sha} note={sha}")
    assert f"sha256:{sha}" in sanitized
    assert f"note={sha}" not in sanitized
    assert "note=[REDACTED]" in sanitized


def test_redaction_real_secrets_still_redacted() -> None:
    sk = "sk-abcdefghijklmnopqrstuvwxyz123456"
    ghp = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    random_token = "AbCdEf0123456789AbCdEf0123456789"
    assert sanitize_text(sk) == "[REDACTED]"
    assert "abcdefghijklmnopqrstuvwxyz123456" not in sanitize_text(ghp)
    assert sanitize_text(random_token) == "[REDACTED]"
    assert sanitize_text("abcdefghijklmnopqrstuvwxyzABCDEFG") == "[REDACTED]"


def test_redaction_regression_guard(tmp_path: Path) -> None:
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    sneaky = {
        "runId": "abcdefghijklmnopqrstuvwxyzABCDEFG",
        "submitError": f"upstream rejected sk-abcdefghijklmnopqrstuvwxyz123456 sha256:{sha}",
        "nested": {"note": "ghp_abcdefghijklmnopqrstuvwxyz123456"},
    }
    sanitized = cast(dict[str, object], sanitize_value(sneaky))
    assert sanitized["runId"] == "[REDACTED]"
    submit_error = cast(str, sanitized["submitError"])
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in submit_error
    assert f"sha256:{sha}" in submit_error
    assert "abcdefghijklmnopqrstuvwxyz123456" not in json.dumps(sanitized["nested"])


def test_redaction_uuid_run_ids_preserved() -> None:
    uuid = "123e4567-e89b-42d3-a456-426614174000"
    assert sanitize_text(uuid) == uuid
    run_id = "123e4567-e89b-42d3-a456-426614174000:demo-001:1000:0"
    assert sanitize_text(run_id) == run_id


def test_writer_redacts_on_disk_and_keeps_hashes(tmp_path: Path) -> None:
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    path = str(tmp_path / "runtime.jsonl")
    writer = JsonlWriter(path)
    writer.write(
        runtime_trace(
            {
                **RT,
                "runId": "sk-abcdefghijklmnop123456",
                "configHash": f"sha256:{sha}",
                "strategyHash": f"sha256:{sha}",
            }
        )
    )
    writer.close()
    line = Path(path).read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert parsed["configHash"] == f"sha256:{sha}"
    assert parsed["strategyHash"] == f"sha256:{sha}"
    assert parsed["runId"] == "[REDACTED]"
    assert "sk-abcdefghijklmnop123456" not in line


def test_writer_redacts_mapping_extension_fields_on_disk(tmp_path: Path) -> None:
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    path = tmp_path / "extended-runtime.jsonl"
    record = to_json_object(runtime_trace({**RT, "configHash": f"sha256:{sha}"}))
    record.update(
        {
            "apiToken": "secret-value",
            "authorization": "Bearer abc",
            "nested": [{"password": "p", "safe": "ok"}],
            "credential": {"nested": "credential-value"},
            "apiTokenHash": f"sha256:{sha}",
            "tokenCount": 3,
            "passwordPolicy": "managed",
        }
    )

    writer = JsonlWriter(path)
    writer.write(record)
    writer.close()

    line = path.read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert parsed["apiToken"] == "[REDACTED]"
    assert parsed["authorization"] == "[REDACTED]"
    assert parsed["nested"][0]["password"] == "[REDACTED]"
    assert parsed["nested"][0]["safe"] == "ok"
    assert parsed["credential"] == "[REDACTED]"
    assert parsed["apiTokenHash"] == "[REDACTED]"
    assert parsed["configHash"] == f"sha256:{sha}"
    assert parsed["tokenCount"] == 3
    assert parsed["passwordPolicy"] == "managed"
    for leaked in ("secret-value", "Bearer abc", "credential-value"):
        assert leaked not in line


def test_invalid_record_raises_and_does_not_create_file(tmp_path: Path) -> None:
    path = str(tmp_path / "bad.jsonl")
    writer = JsonlWriter(path)
    with pytest.raises(ValueError, match="invalid trace record"):
        writer.write({"bad": True})
    assert not Path(path).exists()
    writer.close()


def test_close_blocks_writes_and_io_failure_counts(tmp_path: Path) -> None:
    writer = JsonlWriter(str(tmp_path / "t.jsonl"))
    writer.close()
    with pytest.raises(JsonlWriterError, match="closed"):
        writer.write(runtime_trace(RT))
    bad_writer = JsonlWriter(str(tmp_path / "no-such-dir" / "t.jsonl"))
    bad_writer.write(runtime_trace(RT))
    assert bad_writer.dropped_count == 1
    assert bad_writer.last_error is not None


def test_torn_tail_fails_closed_by_default(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    path.write_text('{"tick":1,"partial":true}\n{"tick":2,"partial":', encoding="utf-8")
    writer = JsonlWriter(str(path))
    with pytest.raises(TornTailError, match="torn tail"):
        writer.write(runtime_trace(RT))
    assert (
        Path(path).read_text(encoding="utf-8") == '{"tick":1,"partial":true}\n{"tick":2,"partial":'
    )


def test_torn_tail_recovery_truncates_and_counts(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    path.write_text('{"tick":1}\n{"tick":2}\n{"tick":3,', encoding="utf-8")
    writer = JsonlWriter(str(path), recover_torn_tail=True)
    writer.write(runtime_trace(RT))
    writer.close()
    rows = _read_rows(str(path))
    # The torn partial line is dropped; the new record is appended after recovery.
    assert [row["tick"] for row in rows] == [1, 2, 1000]
    assert writer.dropped_count == 1


def test_traversal_and_nul_paths_rejected(tmp_path: Path) -> None:
    with pytest.raises(JsonlWriterError, match="traversal"):
        JsonlWriter(str(tmp_path / ".." / "escape.jsonl"))
    with pytest.raises(JsonlWriterError, match="NUL"):
        JsonlWriter("bad\x00name.jsonl")
    directory = tmp_path / "adir"
    directory.mkdir()
    writer = JsonlWriter(str(directory))
    with pytest.raises(JsonlWriterError, match="directory"):
        writer.write(runtime_trace(RT))


def test_symlink_target_rejected(tmp_path: Path) -> None:
    target = tmp_path / "real.jsonl"
    link = tmp_path / "link.jsonl"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this platform")
    writer = JsonlWriter(str(link))
    with pytest.raises(JsonlWriterError, match="symlink"):
        writer.write(runtime_trace(RT))


def test_duplicate_active_writer_same_path_rejected(tmp_path: Path) -> None:
    path = str(tmp_path / "one.jsonl")
    first = JsonlWriter(path)
    with pytest.raises(JsonlWriterError, match="another active"):
        JsonlWriter(path)
    first.close()
    second = JsonlWriter(path)
    second.close()


def test_concurrent_appends_are_complete_and_ordered(tmp_path: Path) -> None:
    path = str(tmp_path / "conc.jsonl")
    writer = JsonlWriter(path)
    count = 64
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda index: writer.write(
                    runtime_trace({**RT, "tick": index, "runId": f"t-{index}"})
                ),
                range(count),
            )
        )
    writer.close()
    rows = _read_rows(path)
    assert len(rows) == count
    assert {row["tick"] for row in rows} == set(range(count))
    for row in rows:
        json.loads(json.dumps(row))  # every row is complete JSON
    assert len({row["runId"] for row in rows}) == count


def test_rotate_collision_fails_closed_and_counts(tmp_path: Path) -> None:
    path = str(tmp_path / "coll.jsonl")
    writer = JsonlWriter(path, JsonlRotationOptions(max_bytes=8, max_backups=2))
    writer.write(runtime_trace(RT))
    assert writer.dropped_count == 0
    (tmp_path / "coll.jsonl.1").mkdir()
    writer.write(runtime_trace({**RT, "tick": 2000}))
    assert writer.dropped_count == 1
    assert writer.last_error is not None
    # The active file keeps only the first complete record; the colliding backup
    # was not silently overwritten.
    rows = _read_rows(path)
    assert len(rows) == 1
    assert (tmp_path / "coll.jsonl.1").is_dir()
    writer.close()


def test_append_jsonl_line_low_level(tmp_path: Path) -> None:
    path = str(tmp_path / "low.jsonl")
    append_jsonl_line(str(path), '{"a":1}')
    append_jsonl_line(str(path), '{"a":2}\n')
    assert Path(path).read_text(encoding="utf-8") == '{"a":1}\n{"a":2}\n'
    with pytest.raises(JsonlWriterError, match="maxBytes"):
        append_jsonl_line(str(path), "x", JsonlRotationOptions(max_bytes=0, max_backups=1))


def test_flush_smoke(tmp_path: Path) -> None:
    path = str(tmp_path / "flush.jsonl")
    writer = JsonlWriter(path)
    writer.write(runtime_trace(RT))
    writer.flush()
    writer.close()
    assert len(_read_rows(path)) == 1


def test_default_rotation_is_bounded() -> None:
    assert DEFAULT_JSONL_ROTATION.max_bytes == 16 * 1024 * 1024
    assert DEFAULT_JSONL_ROTATION.max_backups == 4
