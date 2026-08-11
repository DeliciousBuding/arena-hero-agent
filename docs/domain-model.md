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

## Tenant state ownership

`TenantState` is the single authoritative semantic identity for one tenant partition. It composes
the immutable `WorldProjection` (world), `TenantId` (partition), and the decision journal identity
(`decision_count` plus `last_decision_id`). Its `state_digest` is the canonical SHA-256 identity
used by compare-and-set storage; generation and fencing tokens are durable-envelope concerns owned
by the writer lease and are intentionally not part of the digest.

State advances are pure, fail-closed reducers:

- `observe(world)` folds in a newer world projection. A tick regression or a conflicting
  observation for an already-seen tick is rejected; an identical projection is a no-op.
- `record_decision(decision_id)` commits one decision. Re-committing the current last decision is
  rejected so the lease/arbiter can treat it as a duplicate signal.

`TenantState` is the `StateT` consumed by the `TenantStateStore` compare-and-set port; the P4-9
lease/arbiter layer builds on this seam.

## Runtime protocol checks

Public ports use `typing.runtime_checkable`. This supports inexpensive adapter smoke checks and
composition-root diagnostics. Python runtime checks verify attribute presence only; they do not
validate method signatures, async behavior, generic arguments, fencing correctness, or durability.
Static type checking, contract tests, and adapter-specific integration tests remain authoritative.
