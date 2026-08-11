"""Fail-closed path and tenant validation for the Command Center data base."""

from __future__ import annotations

from pathlib import Path

import pytest

from arena_hero_agent.command_center import (
    TENANTS,
    CommandCenterError,
    calibration_dir,
    human_commands_dir,
    human_commands_file,
    normalize_tenant,
    outcome_jsonl_path,
    redeem_log_path,
    registry_db_path,
    resolve_data_root,
    runtime_dir,
    survey_db_path,
    telemetry_dir,
    validate_data_root,
    validate_survey_tenant,
    validate_tenant,
)


def test_tenants_frozen_to_production_set() -> None:
    assert TENANTS == ("t1", "t2", "t3", "t4")


def test_validate_data_root_rejects_empty() -> None:
    with pytest.raises(CommandCenterError, match="must not be empty"):
        validate_data_root("")


def test_validate_data_root_rejects_nul() -> None:
    with pytest.raises(CommandCenterError, match="NUL"):
        validate_data_root("data\x00root")


@pytest.mark.parametrize(
    "bad",
    [
        "data/../escape",
        "data\\..\\escape",
        "..",
        "..\\escape",
        "/tmp/../escape",
    ],
)
def test_validate_data_root_rejects_traversal(bad: str) -> None:
    with pytest.raises(CommandCenterError, match="traversal"):
        validate_data_root(bad)


def test_validate_data_root_accepts_normal_paths(tmp_path: Path) -> None:
    assert validate_data_root(tmp_path) == tmp_path
    assert validate_data_root(str(tmp_path)) == tmp_path


def test_resolve_data_root_override_wins(tmp_path: Path) -> None:
    assert resolve_data_root(override=tmp_path) == tmp_path


def test_resolve_data_root_reads_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARENA_DATA_ROOT", str(tmp_path))
    assert resolve_data_root() == tmp_path


def test_validate_tenant_accepts_production_tenants() -> None:
    for tenant in TENANTS:
        assert validate_tenant(tenant) == tenant


@pytest.mark.parametrize("bad", ["t5", "sim-1", "tenant-a"])
def test_validate_tenant_rejects_non_production(bad: str) -> None:
    with pytest.raises(CommandCenterError, match="not a runtime tenant"):
        validate_tenant(bad)


@pytest.mark.parametrize("bad", ["T1", "", "t1/x", "t 1"])
def test_validate_tenant_rejects_invalid_identifier(bad: str) -> None:
    with pytest.raises(CommandCenterError, match="invalid tenant"):
        validate_tenant(bad)


def test_validate_survey_tenant_accepts_sim_namespace() -> None:
    assert validate_survey_tenant("sim-1") == "sim-1"
    assert validate_survey_tenant("sim-burn-in") == "sim-burn-in"


@pytest.mark.parametrize("bad", ["other", "tenant-a"])
def test_validate_survey_tenant_rejects_unknown(bad: str) -> None:
    with pytest.raises(CommandCenterError, match="not a survey tenant"):
        validate_survey_tenant(bad)


@pytest.mark.parametrize("bad", ["", "T1", "t1/x"])
def test_validate_survey_tenant_rejects_invalid_identifier(bad: str) -> None:
    with pytest.raises(CommandCenterError, match="invalid tenant"):
        validate_survey_tenant(bad)


def test_normalize_tenant_accepts_tenant_id() -> None:
    from arena_hero_agent.domain import TenantId

    assert normalize_tenant(TenantId("t1")) == "t1"


def test_path_helpers_stay_under_data_root(tmp_path: Path) -> None:
    assert runtime_dir(tmp_path, "t1") == tmp_path / "runtime" / "t1"
    assert calibration_dir(tmp_path, "t1") == tmp_path / "runtime" / "t1" / "calibration"
    assert telemetry_dir(tmp_path, "t1") == tmp_path / "runtime" / "t1" / "telemetry"
    assert human_commands_dir(tmp_path) == tmp_path / "runtime" / "human-commands"
    assert (
        human_commands_file(tmp_path, "t1") == tmp_path / "runtime" / "human-commands" / "t1.json"
    )
    assert survey_db_path(tmp_path, "t1") == tmp_path / "runtime" / "survey" / "t1.db"
    assert survey_db_path(tmp_path, "sim-x") == tmp_path / "runtime" / "survey" / "sim-x.db"
    assert registry_db_path(tmp_path) == tmp_path / "runtime" / "registry.db"
    assert redeem_log_path(tmp_path) == tmp_path / "runtime" / "redeem-log.jsonl"
    assert outcome_jsonl_path(tmp_path, "t1") == (
        tmp_path / "runtime" / "t1" / "telemetry" / "outcome.jsonl"
    )
