"""LiveStatusWriter tests: resource observability snapshot."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arena_hero_agent.adapters.runtime.live_status import (
    LIVE_STATUS_FILENAME,
    LiveStatusWriter,
    LiveStatusWriterConfig,
)
from arena_hero_agent.application import PlayerLifecycle, TurnObservation
from arena_hero_agent.domain import (
    BeaconObservation,
    BeaconStatus,
    Coordinate,
    CoreObservation,
    CoreState,
    EntityId,
    RulesVersion,
    TenantId,
    WorldProjection,
)


def _observation(
    *,
    tick: int = 1,
    resources: int = 0,
    population: int = 0,
    lifecycle: PlayerLifecycle = PlayerLifecycle.ACTIVE,
    respawn_at_tick: int | None = None,
) -> TurnObservation:
    return TurnObservation(
        tick=tick,
        lifecycle=lifecycle,
        resources=resources,
        population=population,
        respawn_at_tick=respawn_at_tick,
        projection=WorldProjection(
            tick=tick,
            rules_version=RulesVersion.V0_14,
            core=CoreObservation(
                id=EntityId("core-a"),
                position=Coordinate(0, 0),
                health=5,
                shield=4,
                state=CoreState.NORMAL,
                owner="player",
            ),
            units=(),
            entities=(),
            resources=(),
            terrain=(),
            beacon=BeaconObservation(
                position=Coordinate(0, 1),
                status=BeaconStatus.UNKNOWN,
            ),
        ),
    )


def _writer(tmp_path: Path, tenant: str = "t1") -> LiveStatusWriter:
    return LiveStatusWriter(LiveStatusWriterConfig(data_root=tmp_path, tenant_id=TenantId(tenant)))


def test_writes_compact_snapshot(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.write(_observation(tick=104950, resources=7, population=12))

    path = tmp_path / "t1" / LIVE_STATUS_FILENAME
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["kind"] == "live"
    assert data["tenantId"] == "t1"
    assert data["tick"] == 104950
    assert data["player_status"] == "ACTIVE"
    assert data["core"]["resources"] == 7
    assert data["population"] == 12
    assert "uptime" in data
    assert "written_at" in data


def test_respawning_lifecycle_is_reflected(tmp_path: Path) -> None:
    writer = _writer(tmp_path, tenant="t2")
    writer.write(_observation(tick=9, lifecycle=PlayerLifecycle.RESPAWNING, respawn_at_tick=12))

    data = json.loads((tmp_path / "t2" / LIVE_STATUS_FILENAME).read_text(encoding="utf-8"))
    assert data["player_status"] == "RESPAWNING"


def test_overwrites_previous_snapshot_atomically(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.write(_observation(tick=1, resources=1, population=1))
    writer.write(_observation(tick=2, resources=2, population=2))

    data = json.loads((tmp_path / "t1" / LIVE_STATUS_FILENAME).read_text(encoding="utf-8"))
    assert data["tick"] == 2
    assert data["core"]["resources"] == 2
    assert not list(tmp_path.glob("**/*.tmp"))


def test_io_failure_is_swallowed(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    target = tmp_path / "t1" / LIVE_STATUS_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()  # turning the target into a directory makes the write fail
    writer.write(_observation(tick=1, resources=1, population=1))  # must not raise


def test_rejects_non_observation(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    with pytest.raises(TypeError):
        writer.write("not-an-observation")  # type: ignore
