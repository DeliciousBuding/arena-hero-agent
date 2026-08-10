# Domain model and port contracts

The domain package contains immutable value objects and deterministic canonicalization. It has no
filesystem, network, database, framework, adapter, or SDK dependency. Official game events,
command plans, acknowledgements, and other wire models remain owned by the `arena-hero` Python SDK;
the `GameClient` and `DecisionRecorder` ports refer to those types only during static type checking.

## Deterministic values

- `TenantId`, `EntityId`, and `DecisionId` reject whitespace and noncanonical spellings instead of
  normalizing input silently.
- `Coordinate` uses signed 32-bit components and lexicographic `(x, y)` ordering. Game-map bounds
  remain an application or rules concern.
- `Generation` starts at zero. `FencingToken` starts at one. Both expose explicit successor and
  strict supersession operations.
- `DeadlineBudget` stores remaining nanoseconds. It is a duration, not an absolute timestamp.
- `StateDigest` is a lowercase SHA-256 digest of the canonical representation.

Canonical serialization is a typed JSON tree. Map keys are sorted, set members are sorted by their
canonical bytes, and list order remains semantic. Floats and wall-clock values are rejected. A
wall-clock observation may be recorded as evidence outside deterministic state, but it must not be
used to derive a decision identifier, state digest, generation, or fencing token.

## Lease authority

Decision, writer, and migration acquisition are separate protocols:

- a decision lease coordinates one decision and does not authorize durable state writes;
- a writer lease carries a fencing token and generation for durable tenant writes;
- a migration lease coordinates a target generation and does not imply decision authority.

The common `LeaseHandle` only defines lifecycle operations. Purpose-specific handle protocols carry
the additional identifiers required by their operation.

## Runtime protocol checks

Public ports use `typing.runtime_checkable`. This supports inexpensive adapter smoke checks and
composition-root diagnostics. Python runtime checks verify attribute presence only; they do not
validate method signatures, async behavior, generic arguments, fencing correctness, or durability.
Static type checking, contract tests, and adapter-specific integration tests remain authoritative.
