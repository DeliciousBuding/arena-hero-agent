"""Deterministic, content-addressed serialization for offline run artifacts.

P4-20: the offline ``run`` and ``batch`` commands emit JSONL records whose only
non-semantic variation is wall-clock metadata (``recordedAtNs`` in tick/loop
records; ``updatedAtNs`` and ``startedAtNs`` in the health document). This
module provides the canonical view of those artifacts:

- ``strip_nonsemantic`` removes the timestamp metadata keys from one record.
- ``canonical_record_bytes`` encodes one stripped record with sorted keys and
  compact separators, so bytes are stable across processes and platforms.
- ``jsonl_file_digest`` / ``json_document_digest`` hash one artifact file.
- ``run_artifacts_digest`` hashes a complete run (health + telemetry + ticks)
  in a fixed order and returns per-artifact plus combined digests.

The combined ``run`` digest binds ``runId`` (carried by the health document and
by every telemetry record), so the same input with a different run id produces
a different run digest, while the per-record decision content (the ``ticks``
digest) stays identical across run ids: run id is declared not to affect
decision content.

Record field names and record types are untouched: Lab's ``import_agent_run``
consumes the existing JSONL contracts, so nothing here renames wire keys or
changes the emitted record schema.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

#: Wall-clock metadata keys that carry no decision semantics and are stripped
#: before hashing so identical runs hash identically.
NON_SEMANTIC_KEYS: Final = frozenset({"recordedAtNs", "updatedAtNs", "startedAtNs"})

MANIFEST_SCHEMA_VERSION: Final = 1
MANIFEST_FILENAME: Final = "manifest.json"

_DIGEST_COMPONENT_ORDER: Final = ("health", "telemetry", "ticks")


def strip_nonsemantic(record: Mapping[str, object]) -> dict[str, object]:
    """Return a copy of ``record`` without non-semantic timestamp metadata."""
    return {key: value for key, value in record.items() if key not in NON_SEMANTIC_KEYS}


def canonical_record_bytes(record: Mapping[str, object]) -> bytes:
    """Deterministic UTF-8 encoding of one record (timestamps stripped)."""
    return json.dumps(
        strip_nonsemantic(record),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_document_digest(data: Mapping[str, object]) -> str:
    """sha256 of one JSON object in canonical form (timestamps stripped)."""
    return _sha256(canonical_record_bytes(data))


def jsonl_file_digest(path: Path) -> str | None:
    """sha256 of a JSONL artifact in canonical form, or ``None`` if absent.

    Each non-empty line is parsed as a JSON object and re-encoded canonically;
    lines are joined with ``\\n`` and a trailing newline terminates the payload
    so the digest is stable and unambiguous.
    """
    if not path.exists():
        return None
    records: list[Mapping[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object per line in {path}")
        records.append(data)
    payload = b"\n".join(canonical_record_bytes(record) for record in records) + b"\n"
    return _sha256(payload)


def run_artifacts_digest(tenant_dir: Path) -> dict[str, str | None]:
    """Return per-artifact and combined digests for one completed run.

    ``health`` uses the persisted health document, ``telemetry`` and ``ticks``
    use the JSONL artifacts. Missing artifacts (for example ``ticks.jsonl``
    under the SQLite backend) digest to ``None`` and are excluded from the
    combined ``run`` digest.
    """
    health = json_document_digest(
        json.loads((tenant_dir / "health.json").read_text(encoding="utf-8"))
    )
    telemetry = jsonl_file_digest(tenant_dir / "telemetry.jsonl")
    ticks = jsonl_file_digest(tenant_dir / "ticks.jsonl")
    components = {
        "health": health,
        "telemetry": telemetry,
        "ticks": ticks,
    }
    present = [
        f"{name}:{components[name]}\n".encode()
        for name in _DIGEST_COMPONENT_ORDER
        if components[name] is not None
    ]
    combined = _sha256(b"".join(present))
    return {**components, "run": combined}


def build_manifest(
    tenant_dir: Path,
    *,
    tenant_id: str,
    run_id: str,
    process_run_id: str,
) -> dict[str, object]:
    """Build the content-addressed manifest for one completed run."""
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "tenantId": tenant_id,
        "runId": run_id,
        "processRunId": process_run_id,
        "digests": run_artifacts_digest(tenant_dir),
    }


def read_manifest(path: Path) -> dict[str, object] | None:
    """Read a manifest, returning ``None`` when absent or malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "NON_SEMANTIC_KEYS",
    "build_manifest",
    "canonical_record_bytes",
    "json_document_digest",
    "jsonl_file_digest",
    "read_manifest",
    "run_artifacts_digest",
    "strip_nonsemantic",
]
