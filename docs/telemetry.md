# Telemetry primitives

The `arena_hero_agent.telemetry` package provides the agent-side, locally persisted
audit trail. It is self-contained: it depends only on the Python standard library
and sibling modules, and it never imports the `arena-hero` SDK telemetry sink, an
HTTP client, a database, or any adapter/framework code. Official wire and telemetry
contracts remain owned by the `arena-hero` SDK; this package implements the local
JSONL record layer that decision and outcome telemetry is written through.

## Scope

- Strict versioned record schemas (`schema.py`, `SCHEMA_VERSION = 1`).
- Three trace record families: runtime, decision, and outcome.
- Deterministic plan hashing (`plan_hash_of`).
- Append-only JSONL writer with flush, bounded rotation, and torn-tail recovery
  (`jsonl_writer.py`).
- Recursive credential redaction before anything reaches disk.

Non-goals (by design, matching the Python-first spec):

- No HTTP endpoint, SDK sink, database, or real runtime collection.
- No wall-clock-based identifiers: timestamps may be recorded as evidence
  elsewhere, but they never enter `plan_hash_of` or any deterministic identity.
- No cross-process file locking: production single-writer discipline is enforced
  by the release-control lease/fencing layer, not by this package. In-process
  serialization is guaranteed (see below).

## Records

`RuntimeTraceRecord`, `DecisionTraceRecord`, and `OutcomeTraceRecord` are immutable
frozen dataclasses whose field names are camelCase because they are wire-level JSON
keys. Construction is strict: factories reject unknown fields. Validation is
TypeBox-compatible: `validate_trace_record` accepts plain mappings as well as record
objects, and unknown/extra fields are tolerated (TypeBox `Type.Object` allows
additional properties by default). This is what lets extended records such as stall
telemetry pass validation without schema churn.

Optional fields that were not provided use the `UNSET` sentinel and are omitted from
canonical JSON output; an explicitly set `None` is emitted as `null` exactly where
the schema permits it (`agentLatencyMs`, `threatReason`, `coreState`, beacon
`status`/`carrierId`, ...).

`to_json_object` / `to_json` produce deterministic JSON: absent optional fields are
omitted, key order follows field declaration order, and repeated serialization of
the same record yields identical bytes.

## Deterministic plan hash

`plan_hash_of(value)` replicates the TypeScript `planHashOf` exactly:

1. stable, key-sorted JSON serialization (`_stable_stringify`), with JavaScript
   `JSON.stringify` semantics for strings, numbers, and escape sequences;
2. FNV-1a 32-bit over UTF-16 code units (surrogate pairs included), formatted as
   8 lowercase hex digits.

Known answers in `tests/telemetry/test_decision_trace.py` were computed with
Node.js against the TypeScript oracle, so hash parity is verified cross-language.
Key insertion order, float formatting (`1.0` -> `1`, `1e-7` -> `1e-7`), and
non-ASCII characters are all covered.

## JsonlWriter

`JsonlWriter` is append-only and single-writer within a process:

- Every record is validated before any IO; invalid records raise and never create
  or touch a file (no dirty data in the audit chain).
- Values are redacted recursively before serialization (`configHash` /
  `strategyHash` / `planHash` are kept verbatim because they are audit-chain
  identity keys).
- Appends and rotations are serialized with a per-writer lock; a process-wide
  registry refuses a second active writer for the same resolved path.
- `write()` raises for closed writers and invalid records. Best-effort IO failures
  during append/rotate increment `dropped_count` and record `last_error` without
  raising, so the decision path is never blocked (TypeScript parity). Open-time
  safety errors — symlink/directory targets and torn tails — always raise.

### Path hardening

The write path is always supplied by the caller and validated:

- NUL bytes and `..` traversal components are rejected at construction. Both
  `/` and `\` are parsed as separators on every host, so Windows-spelled paths
  cannot bypass traversal checks on POSIX systems.
- Empty paths, POSIX/Windows root-only paths, drive-only paths, UNC share roots,
  and paths ending in a separator fail closed because they do not name a file.
  Valid POSIX paths and Windows root-relative, drive-relative, drive-absolute,
  and UNC file paths remain constructible.
- The final component is never followed if it is a symlink; a directory target
  is rejected.

### Rotation

`DEFAULT_JSONL_ROTATION` is 16 MiB per active file with up to 4 backups
(`<path>.1`, `<path>.2`, ...). Rotation happens only at complete-line boundaries
before an append. Rotation is fail-closed on collision: a non-regular backup target
(a directory or symlink) is never silently overwritten — the low-level
`append_jsonl_line` raises, and `JsonlWriter.write` counts the drop.

### Torn-tail recovery

A file that does not end in a newline indicates a torn/partial write (for example
after a crash mid-append). The writer fails closed on the first write with
`TornTailError` unless `recover_torn_tail=True` is passed, which truncates the
active file to the last complete line and counts the partial record as dropped.

### Flush

`flush()` explicitly fsyncs the active file for durable boundaries. Regular writes
are append-only like TypeScript and are not fsynced per write.

## Redaction

`sanitize_text` follows the TypeScript text patterns for `sk-` prefixes,
`Bearer` tokens, credential assignments, `ARENA_HERO_API_KEY_*` assignments, and
bare 32+ character alphanumeric runs. UUIDs (hyphenated), normal text, and
`sha256:`-prefixed identifiers pass through unchanged.

`sanitize_value` adds mapping-aware, recursive fail-closed protection before a
record reaches disk:

- Mapping keys are split into lowercase semantic tokens across camelCase,
  snake_case, kebab-case, and capitalization variants.
- Direct credential keys (`authorization`, `apiKey`, `token`, `cookie`, `secret`,
  `password`, `credential`) and credential compounds such as `accessToken`,
  `clientSecret`, or `serviceCredential` have their entire value replaced by
  `[REDACTED]`, regardless of whether that value is a string, mapping, list,
  number, boolean, or null. The key remains visible for diagnostics.
- Payload-qualified credential keys such as `authorizationHeader`, `tokenValue`,
  and `apiTokenHash` are also redacted. Metadata concepts with a different
  semantic suffix, including `tokenCount` and `passwordPolicy`, are preserved.
- Only the exact `configHash`, `strategyHash`, and `planHash` mapping keys bypass
  recursive value sanitization. Similar credential-bearing names such as
  `apiTokenHash` do not inherit that exception.

## Import boundaries

`tests/telemetry/test_telemetry_import_contracts.py` enforces that telemetry
modules import only the standard library and sibling telemetry modules — never
adapters, frameworks, the SDK, sockets, subprocess, or HTTP clients. Telemetry
tests never open network connections.

## Known differences from the TypeScript oracle

- Python dataclasses require required fields before optional ones, so the canonical
  JSON key order groups required fields first (e.g. `planHash` before
  `intentCounts`). JSON object key order is semantically irrelevant; values and
  validation behavior are identical.
- Python factories are strict (unknown fields raise at construction); TypeScript
  factories spread any keys. Extended records can be written through
  `JsonlWriter.write`/`validate_trace_record` directly as mappings, where extra
  fields are tolerated exactly like TypeScript.
- The writer adds explicit path hardening, in-process single-writer enforcement,
  torn-tail recovery, and fail-closed rotation collisions; the TypeScript writer
  has none of these. IO-error counting (`dropped_count`) matches TypeScript.
- Python mapping sanitization is intentionally stricter than the TypeScript
  writer: it classifies credential-bearing key variants before recursively
  sanitizing values, while retaining the TypeScript-compatible text patterns.
- `validate_trace_record` messages are informative and include the first failing
  field path per schema, but are not byte-identical to TypeBox messages.
