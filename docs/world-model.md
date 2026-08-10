# World and navigation domain model

This document defines the public, I/O-free world model used by `arena-hero-agent`.
The model is intentionally smaller than the official SDK wire schema: adapters translate wire
objects into these semantic values, while application and strategy layers consume only the domain
projection.

## Ownership and boundaries

The domain owns:

- immutable coordinates, entity identifiers, world observations, and rules identities;
- deterministic terrain knowledge and bounded path search;
- validation of contradictory or ambiguous observations;
- a typed canonical digest for a normalized `WorldProjection`.

The domain does not own SDK transport objects, persistence, telemetry, process lifecycle,
simulation settlement, planning, or strategy policy. It performs no filesystem, database,
network, clock, random, or framework access.

## Coordinate and cell-key contract

`Coordinate` is a signed 32-bit Cartesian value. Positive `y` is south, matching the compatibility
oracle. Cardinal neighbor order is north, east, south, west; path-search tie breaking uses east,
south, west, north after target-axis preference.

A cell key is exactly `x,y` using canonical decimal integers. Parsing is a strict inverse of
formatting. Whitespace, positive signs, leading zeroes, decimal notation, hexadecimal notation,
and exponent notation are rejected rather than normalized.

## Immutable world projection

`WorldProjection` contains a tick, explicit `RulesVersion`, optional controlled core and beacon,
and normalized tuples of units, visible entities, resources, and terrain observations.
Construction applies stable ordering and rejects:

- duplicate entity identifiers across units, visible entities, and the controlled core;
- duplicate resource or terrain coordinates;
- a resource observed on blocked terrain;
- invalid unit/core/beacon variants;
- negative or greater-than-JavaScript-safe counters;
- mutable or unsupported value shapes.

Unknown terrain is represented by absence from both the open and blocked sets and remains
observable as `CellState.UNKNOWN`; it is never silently rewritten to open terrain.

## Canonical digest

`WorldProjection.state_digest` uses the repository's typed canonical serializer. Collections are
normalized before hashing, enum values have stable product-level canonical names, and insertion
order does not affect the digest. The digest is a domain-state identity, not a byte-for-byte hash
of an SDK payload. Wire-level acknowledgement and replay hashes belong at the adapter/application
seam and require their own compatibility vectors.

## Deterministic navigation

`NavigationGrid` is an immutable three-state projection with optional finite bounds. Every search
requires an explicit `UnknownTraversalPolicy`:

- `BLOCK` treats unknown cells as non-traversable;
- `ALLOW` permits exploration through unknown cells.

Unknown traversal on an unbounded grid also requires explicit `SearchLimits`. A proven disconnected
graph raises `UnreachableError`; node-budget or radius exhaustion raises `SearchLimitExceeded` so
callers cannot mistake incomplete knowledge for permanent unreachability.

Breadth-first search uses deterministic target-axis preference and fixed cardinal tie breaking. It
never uses Python object hashes, random choices, or locale-sensitive text ordering. The line-of-sight
helper implements integer supercover behavior, including diagonal corner cells, while excluding the
target cell itself from obstruction.

## Rules and phase identity

Recognized rules versions are `v0.11` and `v0.14`; the current version is explicitly `v0.14`.
Versions are parsed exactly and never inferred from payload shape. Phase classification is a pure,
non-latching transition using immutable thresholds; a forced phase is an explicit input rather than
hidden mutable state.

## Adapter and future-layer seams

A future SDK mapper should:

1. map SDK directions and enums explicitly;
2. preserve unknown beacon state instead of collapsing it to ground or absent;
3. distinguish omitted transport fields from semantic unknowns before constructing domain values;
4. validate identifier uniqueness before handing a projection to reducers or planners.

Reducers may build new immutable projections but must not mutate existing observations. Planners may
consume `NavigationGrid` and select the unknown policy and search limits explicitly. Simulator code
remains in `arena-hero-lab` and must not be imported by this package.

## Compatibility decisions

The compatibility baseline preserves known movement, distance, path tie-breaking, ring-radius, and
visibility behavior. The Python domain deliberately tightens legacy ambiguity by requiring canonical
cell keys, explicit unknown traversal, bounded unbounded-grid searches, duplicate-ID rejection, and
cross-language safe-integer limits.
