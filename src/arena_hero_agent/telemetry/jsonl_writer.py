"""JSONL telemetry writer: append, flush, rotate, torn-tail recovery, redaction.

Port of the TypeScript oracle ``packages/arena-agent/src/telemetry/jsonl-writer.ts``
plus explicit hardening required by the Python-first spec:

- Write paths are always supplied by the caller and validated: NUL bytes and
  ``..`` traversal components are rejected, and the final component must never
  be a symlink or a directory.
- Appends are serialized in-process with a per-writer lock; a process-wide
  registry refuses two active writers for the same resolved path.
- Rotation is fail-closed on collision: non-regular backup targets (directories,
  symlinks) raise instead of being silently overwritten.
- A torn tail (file not ending in a newline) fails closed on the first write
  unless ``recover_torn_tail=True``, which truncates to the last complete line
  and counts the partial record as dropped.
- Best-effort IO errors increment ``dropped_count`` and never block the decision
  path, mirroring TypeScript. Validation failures always raise before any IO.

Text redaction follows the TypeScript secret patterns. Mapping redaction is
deliberately stricter: credential-bearing keys are recognized across common
camelCase, snake_case, and kebab-case variants before their values can reach
disk. Only the exact ``configHash`` / ``strategyHash`` / ``planHash`` fields
bypass value sanitization.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
import threading
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final

from arena_hero_agent.telemetry.schema import TraceRecord, to_json_object, validate_trace_record


class JsonlWriterError(Exception):
    """Raised for invalid writer configuration, closed writers, path violations,
    rotation collisions, and torn tails."""


class TornTailError(JsonlWriterError):
    """Raised when the active file ends mid-record and recovery is not enabled."""


# ---------------------------------------------------------------------------
# Rotation policy
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class JsonlRotationOptions:
    max_bytes: int
    max_backups: int


DEFAULT_JSONL_ROTATION: Final = JsonlRotationOptions(
    max_bytes=16 * 1024 * 1024,
    max_backups=4,
)


def validate_rotation(rotation: JsonlRotationOptions) -> None:
    if (
        not isinstance(rotation.max_bytes, int)
        or isinstance(rotation.max_bytes, bool)
        or rotation.max_bytes < 1
    ):
        raise JsonlWriterError(f"maxBytes must be a safe integer >= 1; actual={rotation.max_bytes}")
    if (
        not isinstance(rotation.max_backups, int)
        or isinstance(rotation.max_backups, bool)
        or rotation.max_backups < 0
    ):
        raise JsonlWriterError(
            f"maxBackups must be a safe integer >= 0; actual={rotation.max_backups}"
        )


def rotated_jsonl_paths(
    path: str, max_backups: int = DEFAULT_JSONL_ROTATION.max_backups
) -> list[str]:
    """Paths ordered newest backup first: ``<path>.1``, ``<path>.2``, ..."""
    if not isinstance(max_backups, int) or isinstance(max_backups, bool) or max_backups < 0:
        raise JsonlWriterError(f"maxBackups must be a safe integer >= 0; actual={max_backups}")
    return [f"{path}.{index + 1}" for index in range(max_backups)]


# ---------------------------------------------------------------------------
# Path hardening
# ---------------------------------------------------------------------------


def validate_write_path(path: str | os.PathLike[str]) -> Path:
    r"""Validate a caller-supplied write path with portable input semantics.

    Both ``/`` and ``\`` are treated as separators for validation on every OS,
    so a Windows-spelled path cannot hide traversal from a POSIX host (or vice
    versa). The returned ``Path`` keeps the host's native filesystem semantics.
    Final-component symlink/directory checks happen at open time (see
    ``_assert_regular_target``).
    """
    if not isinstance(path, (str, os.PathLike)):
        raise JsonlWriterError(f"path must be str or PathLike; actual={type(path).__name__}")
    text = os.fspath(path)
    if not isinstance(text, str):
        raise JsonlWriterError(f"path must resolve to str; actual={type(text).__name__}")
    if not text:
        raise JsonlWriterError("path must not be empty")
    if "\x00" in text:
        raise JsonlWriterError("path must not contain NUL bytes")

    windows_view = PureWindowsPath(text)
    posix_view = PurePosixPath(text)
    if any(part == ".." for part in windows_view.parts) or any(
        part == ".." for part in posix_view.parts
    ):
        raise JsonlWriterError("path must not contain '..' traversal components")
    if text.endswith(("/", "\\")) or not windows_view.name or not posix_view.name:
        raise JsonlWriterError("path must name a concrete file")
    return Path(text)


def _lstat_mode(path: Path) -> int | None:
    try:
        return path.lstat().st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise JsonlWriterError(f"cannot inspect {path}: {exc}") from exc


def _assert_regular_target(path: Path) -> None:
    """Refuse symlink and directory targets (fail closed on escape/collision)."""
    mode = _lstat_mode(path)
    if mode is None:
        return
    if stat.S_ISLNK(mode):
        raise JsonlWriterError(f"refusing symlink target: {path}")
    if stat.S_ISDIR(mode):
        raise JsonlWriterError(f"refusing directory target: {path}")


def _remove_regular(path: Path) -> None:
    mode = _lstat_mode(path)
    if mode is None:
        return
    if stat.S_ISLNK(mode) or stat.S_ISDIR(mode):
        raise JsonlWriterError(f"refusing to remove non-regular file: {path}")
    try:
        path.unlink()
    except FileNotFoundError:
        return


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

HASH_FIELD_KEYS: Final = frozenset({"configHash", "strategyHash", "planHash"})

_CREDENTIAL_KEY_MARKERS: Final = frozenset(
    {
        "authorization",
        "apikey",
        "token",
        "cookie",
        "secret",
        "password",
        "credential",
        "credentials",
    }
)
_CREDENTIAL_VALUE_SUFFIXES: Final = frozenset(
    {"value", "hash", "data", "text", "bytes", "raw", "header"}
)
_CAMEL_ACRONYM_BOUNDARY_RE: Final = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY_RE: Final = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM_RE: Final = re.compile(r"[^A-Za-z0-9]+")

_SECRET_PATTERNS: Final = [
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(
        r"(authorization|api[-_]?key|token|cookie|secret|password)\s*[:=]\s*[\"']?[^\s\"',}]+",
        re.IGNORECASE,
    ),
    re.compile(r"ARENA_HERO_API_KEY(?:_\d+)?=\S+"),
]

_SHA256_PREFIX_RE: Final = re.compile(r"sha256:[0-9a-f]*$")
_LONG_RUN_RE: Final = re.compile(r"[A-Za-z0-9]{32,}")


def _redact_match(match: str) -> str:
    """Preserve the key-name prefix (up to ``=`` / ``:``) and redact the value."""
    eq = match.find("=")
    if 0 <= eq < 24:
        return f"{match[: eq + 1]}[REDACTED]"
    colon = match.find(":")
    if 0 <= colon < 16:
        return f"{match[: colon + 1]}[REDACTED]"
    return "[REDACTED]"


def _redact_long_runs(text: str) -> str:
    """Replicate JS ``/(?<!sha256:[0-9a-f]*)[A-Za-z0-9]{32,}/g``.

    Python's ``re`` cannot express the variable-length negative lookbehind, so
    each long alphanumeric run is checked against a ``sha256:`` + hex suffix of
    the text that precedes it.
    """
    out: list[str] = []
    pos = 0
    for match in _LONG_RUN_RE.finditer(text):
        prefix = text[: match.start()]
        if _SHA256_PREFIX_RE.search(prefix):
            out.append(text[pos : match.end()])
        else:
            out.append(text[pos : match.start()])
            out.append(_redact_match(match.group()))
        pos = match.end()
    out.append(text[pos:])
    return "".join(out)


def redact_text(text: str) -> str:
    """Recursively redact suspected credentials from arbitrary text."""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda match: _redact_match(match.group()), out)
    return _redact_long_runs(out)


def sanitize_text(text: str) -> str:
    """Public single-string sanitizer (test surface, mirrors TS)."""
    return redact_text(text)


def _normalized_mapping_key_tokens(key: str) -> tuple[str, ...]:
    """Split a mapping key into lowercase semantic tokens.

    The two camel-case boundaries handle both ``apiToken`` and ``APIKey``;
    punctuation then makes snake_case and kebab-case equivalent.
    """
    separated = _CAMEL_ACRONYM_BOUNDARY_RE.sub(r"\1 \2", key)
    separated = _CAMEL_WORD_BOUNDARY_RE.sub(r"\1 \2", separated)
    return tuple(part.casefold() for part in _NON_ALNUM_RE.split(separated) if part)


def _is_sensitive_mapping_key(key: object) -> bool:
    """Return whether ``key`` denotes a credential value.

    Direct credential nouns and compounds ending in one (``apiToken``,
    ``clientSecret``) are sensitive. A marker followed only by payload-like
    qualifiers (``apiTokenHash``, ``authorizationHeader``) is also sensitive,
    preventing a cosmetic ``Hash`` suffix from bypassing redaction. Metadata
    concepts such as ``tokenCount`` and ``passwordPolicy`` are not matched.
    """
    if not isinstance(key, str):
        return False
    tokens = _normalized_mapping_key_tokens(key)
    if not tokens:
        return False

    for index, token in enumerate(tokens):
        is_api_key = token == "api" and index + 1 < len(tokens) and tokens[index + 1] == "key"
        marker_width = 2 if is_api_key else 1
        if not is_api_key and token not in _CREDENTIAL_KEY_MARKERS:
            continue
        suffix = tokens[index + marker_width :]
        if not suffix or all(part in _CREDENTIAL_VALUE_SUFFIXES for part in suffix):
            return True
    return False


def sanitize_value(value: object) -> object:
    """Recursively sanitize a JSON value.

    Credential-key values are replaced wholesale regardless of their type;
    keys remain present for diagnostics. The exact ``configHash`` /
    ``strategyHash`` / ``planHash`` fields are kept verbatim because they are
    audit-chain identity keys, not credentials.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key, item in value.items():
            if key in HASH_FIELD_KEYS:
                out[key] = item
            elif _is_sensitive_mapping_key(key):
                out[key] = "[REDACTED]"
            else:
                out[key] = sanitize_value(item)
        return out
    return value


# ---------------------------------------------------------------------------
# Rotation and append
# ---------------------------------------------------------------------------


def _rotate_jsonl(path: Path, max_backups: int) -> None:
    if max_backups == 0:
        _remove_regular(path)
        return
    _remove_regular(Path(f"{path}.{max_backups}"))
    for index in range(max_backups - 1, 0, -1):
        source = Path(f"{path}.{index}")
        if _lstat_mode(source) is None:
            continue
        _assert_regular_target(source)
        os.replace(source, Path(f"{path}.{index + 1}"))
    _assert_regular_target(path)
    os.replace(path, Path(f"{path}.1"))


def append_jsonl_line(
    path: str | os.PathLike[str],
    line: str,
    rotation: JsonlRotationOptions = DEFAULT_JSONL_ROTATION,
) -> None:
    """Append exactly one complete JSONL row, rotating before the append.

    Raises ``JsonlWriterError`` on invalid rotation, path violations, symlink or
    directory targets, or rotation collisions. This is the low-level function;
    ``JsonlWriter.write`` catches IO errors and counts them as drops.
    """
    validate_rotation(rotation)
    candidate = validate_write_path(path)
    complete_line = line if line.endswith("\n") else f"{line}\n"
    line_bytes = len(complete_line.encode("utf-8"))
    if _lstat_mode(candidate) is not None:
        _assert_regular_target(candidate)
        size = candidate.stat().st_size
        if size > 0 and size + line_bytes > rotation.max_bytes:
            _rotate_jsonl(candidate, rotation.max_backups)
    with candidate.open("ab") as handle:
        handle.write(complete_line.encode("utf-8"))


# ---------------------------------------------------------------------------
# In-process single-writer registry
# ---------------------------------------------------------------------------

_active_writers: dict[str, JsonlWriter] = {}
_registry_lock = threading.Lock()


def _register_writer(key: str, writer: JsonlWriter) -> None:
    with _registry_lock:
        existing = _active_writers.get(key)
        if existing is not None and not existing.closed:
            raise JsonlWriterError(f"another active JsonlWriter already owns {key}")
        _active_writers[key] = writer


def _unregister_writer(key: str, writer: JsonlWriter) -> None:
    with _registry_lock:
        if _active_writers.get(key) is writer:
            del _active_writers[key]


# ---------------------------------------------------------------------------
# JsonlWriter
# ---------------------------------------------------------------------------


class JsonlWriter:
    """Append-only, single-writer JSONL telemetry file.

    - Validation failures raise before any IO (fail fast, no dirty data).
    - IO failures during append/rotate increment ``dropped_count`` and record
      ``last_error`` without raising, so the decision path is never blocked.
    - A torn tail fails closed on the first write unless ``recover_torn_tail``
      is enabled (explicit recovery policy: truncate to last complete line).
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        rotation: JsonlRotationOptions = DEFAULT_JSONL_ROTATION,
        *,
        recover_torn_tail: bool = False,
    ) -> None:
        validate_rotation(rotation)
        self._path = validate_write_path(path)
        self._rotation = rotation
        self._recover_torn_tail = bool(recover_torn_tail)
        self._closed = False
        self._opened = False
        self._error_count = 0
        self._last_error: BaseException | None = None
        self._lock = threading.Lock()
        self._registry_key = str(self._path.resolve())
        _register_writer(self._registry_key, self)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def dropped_count(self) -> int:
        return self._error_count

    @property
    def last_error(self) -> BaseException | None:
        return self._last_error

    def _ensure_open(self) -> None:
        if self._opened:
            return
        self._opened = True
        _assert_regular_target(self._path)
        if not self._path.exists():
            return
        size = self._path.stat().st_size
        if size == 0:
            return
        with self._path.open("rb") as handle:
            handle.seek(max(0, size - 1), os.SEEK_SET)
            last = handle.read(1)
        if last == b"\n":
            return
        if not self._recover_torn_tail:
            raise TornTailError(
                f"torn tail detected in {self._path}: file does not end with a newline; "
                "pass recover_torn_tail=True to truncate to the last complete line"
            )
        truncate_to = _last_newline_boundary(self._path)
        with self._path.open("r+b") as handle:
            handle.truncate(truncate_to)
        self._error_count += 1

    def write(self, record: TraceRecord | Mapping[str, object]) -> None:
        """Validate, sanitize, and append one record.

        Raises for closed writers, invalid records, and (unless recovery is
        enabled) torn tails. Other IO errors are counted, not raised.
        """
        if self._closed:
            raise JsonlWriterError("JsonlWriter is closed")
        validate_trace_record(record)
        sanitized = sanitize_value(to_json_object(record))
        line = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with self._lock:
            # Open-time safety errors (symlink/directory target, torn tail) fail
            # loudly and are never counted as best-effort drops.
            self._ensure_open()
            try:
                append_jsonl_line(self._path, line, self._rotation)
            except (OSError, JsonlWriterError) as exc:
                self._error_count += 1
                self._last_error = exc

    def flush(self) -> None:
        """Explicitly fsync the active file.

        Regular writes are append-only like TypeScript; call ``flush`` when a
        caller needs a durable boundary (for example before a handoff).
        """
        with self._lock:
            if self._closed:
                raise JsonlWriterError("JsonlWriter is closed")
            self._ensure_open()
            if not self._path.exists():
                return
            with self._path.open("ab") as handle:
                handle.flush()
                os.fsync(handle.fileno())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _unregister_writer(self._registry_key, self)


def _last_newline_boundary(path: Path) -> int:
    """Byte offset just past the final newline, or 0 when none exists."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        pos = end
        chunk_size = 8192
        while pos > 0:
            start = max(0, pos - chunk_size)
            handle.seek(start)
            chunk = handle.read(pos - start)
            index = chunk.rfind(b"\n")
            if index != -1:
                return start + index + 1
            pos = start
    return 0
