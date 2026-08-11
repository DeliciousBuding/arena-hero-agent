"""Canonical replay decoding and ReplayTickSource contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arena_hero_agent.adapters.replay import (
    ReplayError,
    ReplayTickSource,
    decode_observation,
    load_observations,
)
from arena_hero_agent.application.turns import PlayerLifecycle
from arena_hero_agent.domain import RulesVersion

SDK_FIXTURE = (
    Path(__file__).parent.parent
    / "adapters"
    / "sdk"
    / "fixtures"
    / "turn_to_plan_known_answers_v1.json"
)


def _minimal_payload(tick: int, resources: int = 10) -> dict[str, object]:
    return {
        "tick": tick,
        "lifecycle": "active",
        "resources": resources,
        "population": 0,
        "projection": {
            "tick": tick,
            "rules_version": "v0.14",
            "core": None,
            "units": [],
            "entities": [],
            "resources": [],
            "terrain": [],
            "beacon": None,
        },
        "events": [],
        "respawn_at_tick": None,
    }


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_decode_golden_sdk_observation() -> None:
    document = json.loads(SDK_FIXTURE.read_text(encoding="utf-8"))
    observation = decode_observation(document["observation"])

    assert observation.tick == 7
    assert observation.lifecycle is PlayerLifecycle.ACTIVE
    assert observation.resources == 3
    assert observation.population == 2
    assert observation.projection.tick == 7
    assert observation.projection.rules_version is RulesVersion.V0_14
    assert observation.projection.core is not None
    assert len(observation.projection.units) == 2
    assert len(observation.projection.entities) == 1
    assert len(observation.projection.resources) == 1
    assert len(observation.projection.terrain) == 2
    assert len(observation.events) == 1


def test_decode_minimal_observation() -> None:
    observation = decode_observation(_minimal_payload(3, resources=14))

    assert observation.tick == 3
    assert observation.lifecycle is PlayerLifecycle.ACTIVE
    assert observation.resources == 14
    assert observation.projection.core is None
    assert observation.events == ()


def test_decode_rejects_unknown_keys() -> None:
    payload = _minimal_payload(1)
    payload["surprise"] = True
    with pytest.raises(ReplayError, match="unknown keys"):
        decode_observation(payload)


def test_decode_rejects_missing_key() -> None:
    payload = _minimal_payload(1)
    del payload["events"]
    with pytest.raises(ReplayError, match="missing required key"):
        decode_observation(payload)


def test_decode_rejects_unknown_enum() -> None:
    payload = _minimal_payload(1)
    payload["lifecycle"] = "activex"
    with pytest.raises(ReplayError, match="unknown value"):
        decode_observation(payload)


def test_decode_rejects_bad_coordinate() -> None:
    payload = _minimal_payload(1)
    projection = payload["projection"]
    assert isinstance(projection, dict)
    projection["core"] = {
        "id": "cccccccc-0000-0000-0000-000000000004",
        "position": ["a", 1],
        "state": "normal",
        "health": 10,
        "shield": 4,
        "owner": "player",
        "destination": None,
    }
    with pytest.raises(ReplayError, match="coordinate"):
        decode_observation(payload)


def test_decode_rejects_wrong_type() -> None:
    payload = _minimal_payload(1)
    payload["resources"] = "10"
    with pytest.raises(ReplayError, match="must be an integer"):
        decode_observation(payload)


def test_load_json_array(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "replay.json",
        json.dumps([_minimal_payload(1), _minimal_payload(2)]),
    )
    observations = load_observations(path)
    assert [observation.tick for observation in observations] == [1, 2]


def test_load_json_object_uses_fixture() -> None:
    observations = load_observations(
        Path(__file__).parent.parent / "cli" / "fixtures" / "replay_turns_v1.json"
    )
    assert [observation.tick for observation in observations] == [1, 2, 3]


def test_load_json_object_rejects_unsupported_version(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "replay.json",
        json.dumps({"version": 2, "observations": [_minimal_payload(1)]}),
    )
    with pytest.raises(ReplayError, match="version is not supported"):
        load_observations(path)


def test_load_json_object_requires_observations(tmp_path: Path) -> None:
    path = _write(tmp_path, "replay.json", json.dumps({"version": 1}))
    with pytest.raises(ReplayError, match="missing required key"):
        load_observations(path)


def test_load_jsonl(tmp_path: Path) -> None:
    lines = "\n".join(
        [
            json.dumps(_minimal_payload(1)),
            "",
            json.dumps(_minimal_payload(2)),
            json.dumps(_minimal_payload(3)),
        ]
    )
    path = _write(tmp_path, "replay.jsonl", lines)
    observations = load_observations(path)
    assert [observation.tick for observation in observations] == [1, 2, 3]


def test_load_jsonl_invalid_line(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "replay.jsonl",
        json.dumps(_minimal_payload(1)) + "\n{not json}\n",
    )
    with pytest.raises(ReplayError, match="line 2 is not valid JSON"):
        load_observations(path)


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="could not be read"):
        load_observations(tmp_path / "nope.json")


def test_load_invalid_json(tmp_path: Path) -> None:
    path = _write(tmp_path, "replay.json", "not json")
    with pytest.raises(ReplayError, match="not valid JSON"):
        load_observations(path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "empty"),
        ("   \n\t", "empty"),
        ("[]", "no observations"),
        ('{"version": 1, "observations": []}', "no observations"),
        ("{}\n", "missing required key"),
    ],
)
def test_load_rejects_empty_documents(tmp_path: Path, text: str, message: str) -> None:
    path = _write(tmp_path, "replay.json", text)
    with pytest.raises(ReplayError, match=message):
        load_observations(path)


async def test_replay_source_iterates_and_closes() -> None:
    source = ReplayTickSource(
        [decode_observation(_minimal_payload(1)), decode_observation(_minimal_payload(2))]
    )
    assert source.closed is False

    stream = source.stream()
    assert (await stream.__anext__()).tick == 1
    assert (await stream.__anext__()).tick == 2
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    await stream.aclose()

    source.close()
    assert source.closed is True
    source.close()  # idempotent
    assert source.closed is True

    with pytest.raises(ReplayError, match="closed"):
        source.stream()


async def test_replay_source_reopens_independently() -> None:
    source = ReplayTickSource([decode_observation(_minimal_payload(1))])
    first = source.stream()
    second = source.stream()
    assert (await first.__anext__()).tick == 1
    assert (await second.__anext__()).tick == 1
