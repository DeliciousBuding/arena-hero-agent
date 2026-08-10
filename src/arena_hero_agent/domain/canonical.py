"""Deterministic canonical serialization for domain state and identifiers."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence, Set
from datetime import date, datetime, time
from enum import Enum

CanonicalNode = object


def _type_name(value: object) -> str:
    explicit = getattr(type(value), "__canonical_name__", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def canonicalize(value: object) -> CanonicalNode:
    """Convert supported values into an unambiguous, deterministic JSON tree.

    Maps require string keys and are sorted by key. Sets are sorted by each member's
    canonical byte encoding. Sequences retain order. Floats and wall-clock values are
    intentionally rejected because their platform formatting or observation time must
    not enter deterministic state keys.
    """

    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, Enum):
        return ["enum", _type_name(value), canonicalize(value.value)]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, bytes):
        encoded = base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
        return ["bytes", encoded]
    if isinstance(value, float):
        raise TypeError("floats are not supported in deterministic canonical values")
    if isinstance(value, datetime | date | time):
        raise TypeError("wall-clock values are not supported in deterministic canonical values")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = sorted(dataclasses.fields(value), key=lambda field: field.name)
        return [
            "record",
            _type_name(value),
            [[field.name, canonicalize(getattr(value, field.name))] for field in fields],
        ]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical mappings require string keys")
        return [
            "map",
            [[key, canonicalize(value[key])] for key in sorted(value)],
        ]
    if isinstance(value, Set):
        members = [canonicalize(member) for member in value]
        members.sort(key=_encode_node)
        encoded = [_encode_node(member) for member in members]
        if len(encoded) != len(set(encoded)):
            raise ValueError("set members collapse to duplicate canonical values")
        return ["set", members]
    if isinstance(value, Sequence):
        return ["list", [canonicalize(member) for member in value]]
    raise TypeError(f"unsupported canonical value type: {type(value).__qualname__}")


def _encode_node(node: CanonicalNode) -> bytes:
    return json.dumps(
        node,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_bytes(value: object) -> bytes:
    """Return the UTF-8 canonical encoding used for state digests."""

    return _encode_node(canonicalize(value))


def canonical_sha256(value: object) -> str:
    """Return a lowercase SHA-256 digest of the canonical encoding."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
