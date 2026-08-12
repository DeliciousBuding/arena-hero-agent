"""Deterministic canonical serialization and run manifest tests (P4-20)."""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.cli.canonical import (
    MANIFEST_FILENAME,
    NON_SEMANTIC_KEYS,
    build_manifest,
    canonical_record_bytes,
    json_document_digest,
    jsonl_file_digest,
    read_manifest,
    run_artifacts_digest,
    strip_nonsemantic,
)


def _tick_record(recorded_at_ns: int, tick: int = 1) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "recordType": "tick",
        "tenantId": "t1",
        "recordedAtNs": recorded_at_ns,
        "tick": tick,
        "decisionId": f"decision:{tick}",
        "deadlineOutcome": "candidate",
        "submitResult": "accepted",
        "submitError": None,
    }


def test_strip_nonsemantic_removes_timestamp_metadata() -> None:
    record = {
        "recordedAtNs": 1,
        "updatedAtNs": 2,
        "startedAtNs": 3,
        "tick": 4,
        "runId": "r",
    }
    assert strip_nonsemantic(record) == {"tick": 4, "runId": "r"}
    assert all(key not in NON_SEMANTIC_KEYS for key in strip_nonsemantic(record))


def test_strip_nonsemantic_does_not_mutate_input() -> None:
    record = {"recordedAtNs": 1, "tick": 2}
    strip_nonsemantic(record)
    assert record == {"recordedAtNs": 1, "tick": 2}


def test_canonical_record_bytes_stable_across_key_order() -> None:
    first = canonical_record_bytes({"tick": 1, "runId": "r", "value": None})
    second = canonical_record_bytes({"value": None, "runId": "r", "tick": 1})
    assert first == second
    assert json.loads(first.decode("utf-8")) == {"tick": 1, "runId": "r", "value": None}


def test_jsonl_file_digest_ignores_recorded_at_ns(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text(
        json.dumps(_tick_record(1)) + "\n" + json.dumps(_tick_record(2, tick=2)) + "\n",
        encoding="utf-8",
    )
    second.write_text(
        json.dumps(_tick_record(999)) + "\n" + json.dumps(_tick_record(888, tick=2)) + "\n",
        encoding="utf-8",
    )
    assert jsonl_file_digest(first) == jsonl_file_digest(second)


def test_jsonl_file_digest_detects_semantic_change(tmp_path: Path) -> None:
    base = tmp_path / "base.jsonl"
    changed = tmp_path / "changed.jsonl"
    base.write_text(json.dumps(_tick_record(1, tick=1)) + "\n", encoding="utf-8")
    changed.write_text(json.dumps(_tick_record(2, tick=2)) + "\n", encoding="utf-8")
    assert jsonl_file_digest(base) != jsonl_file_digest(changed)


def test_jsonl_file_digest_missing_returns_none(tmp_path: Path) -> None:
    assert jsonl_file_digest(tmp_path / "nope.jsonl") is None


def test_json_document_digest_strips_health_timestamps() -> None:
    first = {"schemaVersion": 1, "ready": True, "startedAtNs": 1, "updatedAtNs": 2, "runId": "r"}
    second = {"schemaVersion": 1, "ready": True, "startedAtNs": 9, "updatedAtNs": 8, "runId": "r"}
    other = {
        "schemaVersion": 1,
        "ready": True,
        "startedAtNs": 1,
        "updatedAtNs": 2,
        "runId": "other",
    }
    assert json_document_digest(first) == json_document_digest(second)
    assert json_document_digest(first) != json_document_digest(other)


def _write_run_artifacts(tenant_dir: Path, *, run_id: str, tick_value: int = 1) -> None:
    tenant_dir.mkdir(parents=True, exist_ok=True)
    (tenant_dir / "health.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "ready": True,
                "runId": run_id,
                "startedAtNs": 1,
                "updatedAtNs": 2,
            }
        ),
        encoding="utf-8",
    )
    (tenant_dir / "telemetry.jsonl").write_text(
        json.dumps(
            {
                "processRunId": "unknown",
                "tenantId": "t1",
                "tick": 1,
                "runId": run_id,
                "deadlineOutcome": "candidate",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tenant_dir / "ticks.jsonl").write_text(
        json.dumps(_tick_record(10, tick=tick_value)) + "\n",
        encoding="utf-8",
    )


def test_run_artifacts_digest_stable_for_same_input_and_run_id(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    _write_run_artifacts(first, run_id="seam-fixed")
    _write_run_artifacts(second, run_id="seam-fixed")
    assert run_artifacts_digest(first) == run_artifacts_digest(second)


def test_run_artifacts_digest_differs_by_run_id(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    _write_run_artifacts(first, run_id="seam-a")
    _write_run_artifacts(second, run_id="seam-b")
    digests_a = run_artifacts_digest(first)
    digests_b = run_artifacts_digest(second)
    assert digests_a["run"] != digests_b["run"]
    # Decision content (ticks) does not depend on run id.
    assert digests_a["ticks"] == digests_b["ticks"]


def test_build_manifest_binds_run_id_and_digests(tmp_path: Path) -> None:
    tenant_dir = tmp_path / "t1"
    _write_run_artifacts(tenant_dir, run_id="seam-fixed")
    manifest = build_manifest(
        tenant_dir,
        tenant_id="t1",
        run_id="seam-fixed",
        process_run_id="unknown",
    )
    assert manifest["schemaVersion"] == 1
    assert manifest["tenantId"] == "t1"
    assert manifest["runId"] == "seam-fixed"
    digests = manifest["digests"]
    assert isinstance(digests, dict)
    assert digests == run_artifacts_digest(tenant_dir)
    assert isinstance(digests["health"], str)
    assert isinstance(digests["telemetry"], str)
    assert isinstance(digests["ticks"], str)


def test_read_manifest_round_trip_and_failures(tmp_path: Path) -> None:
    path = tmp_path / "t1" / MANIFEST_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text('{"schemaVersion": 1, "runId": "seam-fixed", "digests": {}}', encoding="utf-8")
    assert read_manifest(path) == {"schemaVersion": 1, "runId": "seam-fixed", "digests": {}}
    assert read_manifest(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert read_manifest(bad) is None
