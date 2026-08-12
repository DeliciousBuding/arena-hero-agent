"""Offline batch entry, end-to-end determinism, and fail-closed tests (P4-20)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arena_hero_agent.cli.canonical import MANIFEST_FILENAME, run_artifacts_digest
from arena_hero_agent.cli.main import (
    _SAFE_RUN_ID,
    DEFAULT_DATA_ROOT,
    EXIT_ERROR,
    EXIT_OK,
    _derive_scenario_run_id,
    build_parser,
    main,
)

FIXTURE = Path(__file__).parent / "fixtures" / "replay_turns_v1.json"


def _scenario_dir(
    input_dir: Path,
    *names: str,
) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (input_dir / name).write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")


def _batch_args(input_dir: Path, data_root: Path, **overrides: object) -> list[str]:
    args = ["batch", "--tenant", "t1", "--input-dir", str(input_dir), "--data-root", str(data_root)]
    for key, value in overrides.items():
        args.extend([f"--{key}", str(value)])
    return args


def test_parser_batch_defaults() -> None:
    args = build_parser().parse_args(["batch", "--tenant", "t1", "--input-dir", "scenarios"])
    assert args.command == "batch"
    assert args.tenant == "t1"
    assert args.input_dir == "scenarios"
    assert args.data_root == DEFAULT_DATA_ROOT
    assert args.backend == "jsonl"
    assert args.seed == 0


def test_parser_batch_requires_input_dir() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["batch", "--tenant", "t1"])
    assert exc.value.code == 2


def test_derive_scenario_run_id_shape_and_constraint() -> None:
    run_id = _derive_scenario_run_id("replay_turns_v1", 0)
    assert run_id == "scenario-replay_turns_v1-seed-0"
    assert _SAFE_RUN_ID.fullmatch(run_id) is not None


def test_derive_scenario_run_id_sanitizes_unsafe_characters() -> None:
    run_id = _derive_scenario_run_id("my scenario (1)!", 7)
    assert run_id is not None
    assert _SAFE_RUN_ID.fullmatch(run_id) is not None
    assert " " not in run_id
    assert "(" not in run_id
    assert run_id.endswith("-seed-7")


def test_derive_scenario_run_id_stable_and_seed_sensitive() -> None:
    assert _derive_scenario_run_id("replay_turns_v1", 0) == _derive_scenario_run_id(
        "replay_turns_v1", 0
    )
    assert _derive_scenario_run_id("replay_turns_v1", 0) != _derive_scenario_run_id(
        "replay_turns_v1", 1
    )


def test_derive_scenario_run_id_rejects_unsafe_inputs() -> None:
    assert _derive_scenario_run_id("...", 0) is None
    assert _derive_scenario_run_id("-", 0) is None
    assert _derive_scenario_run_id("name", -1) is None


def test_batch_happy_path_runs_each_file_in_sorted_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_dir = tmp_path / "scenarios"
    _scenario_dir(input_dir, "b.json", "a.json")
    data_root = tmp_path / "data"
    exit_code = main(_batch_args(input_dir, data_root, seed=3))
    assert exit_code == EXIT_OK
    out = capsys.readouterr()
    assert out.err == ""
    assert out.out.index("scenario scenario-a-seed-3: ok") < out.out.index(
        "scenario scenario-b-seed-3: ok"
    )
    assert "batch: 2 scenario(s) completed" in out.out
    for name in ("scenario-a-seed-3", "scenario-b-seed-3"):
        tenant_dir = data_root / name / "t1"
        assert (tenant_dir / "health.json").exists()
        assert (tenant_dir / "telemetry.jsonl").exists()
        assert (tenant_dir / "ticks.jsonl").exists()
        manifest = json.loads((tenant_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        assert manifest["runId"] == name
        assert manifest["tenantId"] == "t1"


def test_batch_determinism_same_seed_same_digests(tmp_path: Path) -> None:
    input_dir = tmp_path / "scenarios"
    _scenario_dir(input_dir, "a.json", "b.json")
    root_one = tmp_path / "data-one"
    root_two = tmp_path / "data-two"
    assert main(_batch_args(input_dir, root_one, seed=0)) == EXIT_OK
    assert main(_batch_args(input_dir, root_two, seed=0)) == EXIT_OK
    for name in ("scenario-a-seed-0", "scenario-b-seed-0"):
        assert run_artifacts_digest(root_one / name / "t1") == run_artifacts_digest(
            root_two / name / "t1"
        )


def test_batch_different_seed_changes_run_digest_but_not_ticks(tmp_path: Path) -> None:
    input_dir = tmp_path / "scenarios"
    _scenario_dir(input_dir, "a.json")
    root_zero = tmp_path / "data-zero"
    root_one = tmp_path / "data-one"
    assert main(_batch_args(input_dir, root_zero, seed=0)) == EXIT_OK
    assert main(_batch_args(input_dir, root_one, seed=1)) == EXIT_OK
    digests_zero = run_artifacts_digest(root_zero / "scenario-a-seed-0" / "t1")
    digests_one = run_artifacts_digest(root_one / "scenario-a-seed-1" / "t1")
    assert digests_zero["run"] != digests_one["run"]
    assert digests_zero["ticks"] == digests_one["ticks"]


def test_batch_fail_closed_on_bad_input_creates_no_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_dir = tmp_path / "scenarios"
    _scenario_dir(input_dir, "a.json")
    (input_dir / "bad.json").write_text('{"version": 1, "observations": []}', encoding="utf-8")
    data_root = tmp_path / "data"
    exit_code = main(_batch_args(input_dir, data_root))
    assert exit_code == EXIT_ERROR
    assert "replay input could not be loaded: bad.json" in capsys.readouterr().err
    assert not data_root.exists()


def test_batch_fail_closed_on_run_id_collision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_dir = tmp_path / "scenarios"
    _scenario_dir(input_dir, "same.json", "same.jsonl")
    data_root = tmp_path / "data"
    exit_code = main(_batch_args(input_dir, data_root))
    assert exit_code == EXIT_ERROR
    assert "run id conflict: multiple scenario files" in capsys.readouterr().err
    assert not data_root.exists()


def test_batch_fail_closed_on_unsafe_scenario_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_dir = tmp_path / "scenarios"
    input_dir.mkdir()
    (input_dir / "_").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    data_root = tmp_path / "data"
    exit_code = main(_batch_args(input_dir, data_root))
    assert exit_code == EXIT_ERROR
    assert "scenario file cannot produce a safe run id" in capsys.readouterr().err
    assert not data_root.exists()


def test_batch_fail_closed_missing_input_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(_batch_args(tmp_path / "nope", tmp_path / "data"))
    assert exit_code == EXIT_ERROR
    assert "input directory not found" in capsys.readouterr().err


def test_batch_fail_closed_empty_input_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_dir = tmp_path / "scenarios"
    input_dir.mkdir()
    exit_code = main(_batch_args(input_dir, tmp_path / "data"))
    assert exit_code == EXIT_ERROR
    assert "no scenario inputs found" in capsys.readouterr().err


def test_batch_invalid_tenant(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    input_dir = tmp_path / "scenarios"
    _scenario_dir(input_dir, "a.json")
    args = [
        "batch",
        "--tenant",
        "Bad Tenant!",
        "--input-dir",
        str(input_dir),
        "--data-root",
        str(tmp_path / "data"),
    ]
    exit_code = main(args)
    assert exit_code == EXIT_ERROR
    assert "invalid tenant id" in capsys.readouterr().err


def test_batch_sqlite_backend_writes_manifest_without_ticks_digest(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "scenarios"
    _scenario_dir(input_dir, "a.json")
    data_root = tmp_path / "data"
    exit_code = main(_batch_args(input_dir, data_root, backend="sqlite"))
    assert exit_code == EXIT_OK
    tenant_dir = data_root / "scenario-a-seed-0" / "t1"
    assert (tenant_dir / "ticks.sqlite3").exists()
    assert not (tenant_dir / "ticks.jsonl").exists()
    manifest = json.loads((tenant_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["digests"]["ticks"] is None
    assert isinstance(manifest["digests"]["health"], str)
    assert isinstance(manifest["digests"]["telemetry"], str)
    assert isinstance(manifest["digests"]["run"], str)


def test_batch_errors_do_not_leak_absolute_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_batch_args(tmp_path / "nope", tmp_path / "data"))
    input_dir = tmp_path / "scenarios"
    _scenario_dir(input_dir, "....json")
    main(_batch_args(input_dir, tmp_path / "data"))
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert str(tmp_path) not in combined
    assert "C:\\" not in combined
    assert "D:\\" not in combined


def test_run_duplicate_run_id_conflict_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_root = tmp_path / "data"
    args = [
        "run",
        "--tenant",
        "t1",
        "--input",
        str(FIXTURE),
        "--data-root",
        str(data_root),
        "--run-id",
        "seam-fixed",
    ]
    assert main(args) == EXIT_OK
    capsys.readouterr()
    ticks_before = (data_root / "t1" / "ticks.jsonl").read_bytes()
    assert main(args) == EXIT_ERROR
    assert "run id conflict" in capsys.readouterr().err
    assert (data_root / "t1" / "ticks.jsonl").read_bytes() == ticks_before


def test_run_writes_content_addressed_manifest(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    args = [
        "run",
        "--tenant",
        "t1",
        "--input",
        str(FIXTURE),
        "--data-root",
        str(data_root),
        "--run-id",
        "seam-fixed",
    ]
    assert main(args) == EXIT_OK
    manifest = json.loads((data_root / "t1" / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == 1
    assert manifest["tenantId"] == "t1"
    assert manifest["runId"] == "seam-fixed"
    assert manifest["processRunId"] == "unknown"
    assert manifest["digests"] == run_artifacts_digest(data_root / "t1")


def test_run_deterministic_canonical_digest_across_fresh_roots(tmp_path: Path) -> None:
    root_one = tmp_path / "data-one"
    root_two = tmp_path / "data-two"
    root_other = tmp_path / "data-other"
    base = ["run", "--tenant", "t1", "--input", str(FIXTURE)]
    assert main(base + ["--data-root", str(root_one), "--run-id", "seam-fixed"]) == EXIT_OK
    assert main(base + ["--data-root", str(root_two), "--run-id", "seam-fixed"]) == EXIT_OK
    assert main(base + ["--data-root", str(root_other), "--run-id", "other-run"]) == EXIT_OK
    digests_one = run_artifacts_digest(root_one / "t1")
    digests_two = run_artifacts_digest(root_two / "t1")
    digests_other = run_artifacts_digest(root_other / "t1")
    assert digests_one == digests_two
    assert digests_one["run"] != digests_other["run"]
    assert digests_one["ticks"] == digests_other["ticks"]
