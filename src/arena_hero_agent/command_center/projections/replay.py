"""Replay trajectory reconstruction (port of legacy ``streams.ts``).

Ports ``loadReplay`` from the TypeScript oracle: from the latest calibration
run, build per-unit and per-core position trails (one point per case tick,
read from ``before.state.objects``) plus compact per-tick event frames
(``after.state`` events with a position, resolved against the same tick's
object map for shot/sweep endpoints). Pure read; nothing is written.

``/api/replay?tenant=tN``. Empty root returns ``None`` (the route wraps it as
``{tenant, generatedAt, replay: null}``), never a 500.

Registered differences from the TS oracle: the 45 s replay cache is not
ported (Python recomputes per request, same output shape); ``generatedAt`` is
injectable via ``now_ms``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

from ..jsonl import calibration_dir, latest_run_dir, list_cases, parse_tick
from ..paths import validate_data_root

__all__ = ["load_replay"]


def _read_case(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _state(raw: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    section = raw.get(key) if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return None
    state = section.get("state")
    return state if isinstance(state, dict) else None


def load_replay(
    data_root: str | os.PathLike[str],
    tenant: str,
    *,
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    """Latest run's compact unit/core replay trajectory (TS ``loadReplay``)."""
    del now_ms
    root = validate_data_root(data_root)
    run_dir = latest_run_dir(root, tenant)
    if run_dir is None:
        return None
    case_files = list_cases(root, tenant, run_dir)
    if not case_files:
        return None

    units: dict[str, dict[str, Any]] = {}
    cores: dict[str, dict[str, Any]] = {}
    ticks: list[int] = []

    for case_file in case_files:
        tick = parse_tick(case_file)
        ticks.append(tick)
        path = calibration_dir(root, tenant) / run_dir / "cases" / case_file
        raw = _read_case(path)
        if raw is None:
            continue
        state = _state(raw, "before")
        objects = state.get("objects") if state is not None else None
        if not isinstance(objects, list):
            continue
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            position = obj.get("position")
            if not isinstance(position, list) or not obj.get("id"):
                continue
            obj_id = obj["id"]
            if obj.get("kind") == "UNIT":
                unit = units.get(str(obj_id))
                if unit is None:
                    unit_trail: list[dict[str, Any]] = []
                    unit = {
                        "type": (
                            obj.get("unit_type") if obj.get("unit_type") is not None else "WORKER"
                        ),
                        "trail": unit_trail,
                    }
                    if "controlled" in obj:
                        unit["controlled"] = obj.get("controlled")
                    units[str(obj_id)] = unit
                unit_trail = cast(list[dict[str, Any]], unit["trail"])
                unit_trail.append(
                    {
                        "t": tick,
                        "x": position[0],
                        "y": position[1],
                        "hp": obj.get("hp") if obj.get("hp") is not None else 0,
                        "cargo": obj.get("cargo") if obj.get("cargo") is not None else 0,
                    }
                )
            elif obj.get("kind") == "CORE":
                core = cores.get(str(obj_id))
                if core is None:
                    core_trail: list[dict[str, Any]] = []
                    core = {
                        "owner": (
                            obj.get("owner_username")
                            if isinstance(obj.get("owner_username"), str)
                            else None
                        ),
                        "trail": core_trail,
                    }
                    if "controlled" in obj:
                        core["controlled"] = obj.get("controlled")
                    cores[str(obj_id)] = core
                core_trail = cast(list[dict[str, Any]], core["trail"])
                core_trail.append(
                    {
                        "t": tick,
                        "x": position[0],
                        "y": position[1],
                        "hp": obj.get("hp") if obj.get("hp") is not None else 0,
                        "shield": obj.get("shield") if obj.get("shield") is not None else 0,
                    }
                )

    event_frames: list[dict[str, Any]] = []
    for case_file in case_files:
        tick = parse_tick(case_file)
        path = calibration_dir(root, tenant) / run_dir / "cases" / case_file
        raw = _read_case(path)
        if raw is None:
            continue
        after_state = _state(raw, "after")
        before_state = _state(raw, "before")
        state = after_state if after_state is not None else before_state
        objects = state.get("objects") if state is not None else None
        events = state.get("events") if state is not None else None
        by_id: dict[str, dict[str, Any]] = {}
        if isinstance(objects, list):
            for obj in objects:
                if isinstance(obj, dict) and obj.get("id"):
                    by_id[str(obj["id"])] = obj
        if not isinstance(events, list):
            continue
        frames: list[dict[str, Any]] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if not ev.get("event_type") or ev.get("position") is None:
                continue
            actor_id = ev.get("actor_id")
            target_id = ev.get("target_id")
            actor = by_id.get(str(actor_id)) if actor_id is not None else None
            target = by_id.get(str(target_id)) if target_id is not None else None
            frames.append(
                {
                    "t": ev.get("event_type"),
                    "p": ev.get("position"),
                    "f": actor.get("position") if actor is not None else None,
                    "q": target.get("position") if target is not None else None,
                    "a": str(actor_id)[:8] if actor_id is not None else None,
                    "g": str(target_id)[:8] if target_id is not None else None,
                    "v": ev.get("values"),
                }
            )
        if frames:
            event_frames.append({"tick": tick, "events": frames})

    return {
        "tenant": tenant,
        "runId": run_dir,
        "ticks": ticks,
        "units": [{"id": unit_id, **entry} for unit_id, entry in units.items()],
        "cores": [{"id": core_id, **entry} for core_id, entry in cores.items()],
        "eventFrames": event_frames,
    }
