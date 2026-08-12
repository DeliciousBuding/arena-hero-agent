# Arena Hero Agent

Arena Hero Agent is a Python-first runtime for building deterministic game strategies, coordinating
multiple tenants, and exposing an operator-facing control plane. The repository keeps the decision
engine independent from transport, storage, and deployment details so it can be tested offline and
integrated through replaceable adapters.

> **Status:** v0.1.4, offline turn-to-decision adapter chain stable (1788 tests). Production
> game behavior and deployment integration are implemented but not yet promoted beyond the
> t4 canary; see `PROGRESS.md`.

## Current capabilities

- **SDK turn adaptation** (`adapt_async_turn`): converts an `arena-hero` 0.3.x `AsyncTurn` /
  `PlayerState` into immutable application DTOs (`TurnObservation`, `WorldProjection`). Malformed,
  duplicate, or future SDK shapes fail closed as `SdkContractViolationError`.
- **Decision construction** (`build_command_plan`): converts an immutable application `Decision`
  (unit and core intents) into a deterministic SDK `CommandPlan`. Tick mismatches, unknown units or
  targets, role-invalid actions, and duplicate intents fail closed before submission.
- **Canonical payloads** (`command_plan_payload`): an environment-neutral JSON projection of a
  `CommandPlan`, pinned by a versioned offline known-answer fixture with a deterministic SHA-256
  digest.
- Deterministic domain navigation, world projection, rules identity, and telemetry primitives.

The full SDK event stream, submission acknowledgement, and error classification are covered by the
existing adapter contract tests; all tests run offline without credentials or live endpoints.

## Runtime foundation

The offline runtime includes a single-tenant tick loop with deadline and reconnect handling,
JSONL/SQLite recording, failure-isolated telemetry, and the replay/health CLI below. Live game
submission, service units, and production promotion remain intentionally separate.

## Command-line interface

The `arena-hero-agent` console script provides the offline replay and health
contracts. It never reads credentials, never connects to a live game API, and
never accepts an API key.

```bash
# Replay canonical turn observations for one tenant, writing recorder,
# telemetry, and a health snapshot under the data root.
arena-hero-agent run --tenant tenant-a --input replay.json --data-root ./data

# Print the persisted health snapshot; exit code 0 = ready, 1 = not ready.
arena-hero-agent health --tenant tenant-a --data-root ./data

# Replay every file in a directory as one scenario with a stable
# scenario-<name>-seed-<n> run id; each scenario gets its own output
# directory under the data root.
arena-hero-agent batch --tenant tenant-a --input-dir ./scenarios --data-root ./data --seed 0
```

`run` accepts a JSON array/object or JSON Lines file of canonical turn
observations. See `arena-hero-agent run --help` and
`arena-hero-agent batch --help` for all options.

### Deterministic offline records

Every successful `run` (and every batch scenario) also writes
`manifest.json` next to the health snapshot: per-artifact and combined SHA-256
digests over health, telemetry, and ticks with non-semantic timestamps
(`recordedAtNs`, `updatedAtNs`, `startedAtNs`) stripped. Identical input plus
run id therefore produce identical digests; a different run id changes the
combined `run` digest (run id is carried by health and telemetry) while the
per-record decision content (`ticks`) digest stays identical. Re-using an
explicit run id inside the same tenant directory fails closed with a clear
error instead of appending ambiguous records.

## Capabilities

- deterministic strategy and planning pipelines;
- tenant-scoped lifecycle, lease fencing, recovery, and audit trails;
- alliance and migration coordination through versioned commands;
- SDK, filesystem, SQLite, subprocess, and HTTP adapters;
- a Python control API with generated browser types;
- a React-based Command Center for read models and authorized operations.

## Repository boundaries

- [`arena-hero`](https://github.com/DeliciousBuding/arena-hero-sdk-py) provides the pinned Python SDK and official
  wire/Turn/Action contracts used by this project.
- `arena-hero-lab` owns simulation, benchmarking, replay analysis, and research workloads.
- This repository does not import Lab internals and does not duplicate SDK-owned protocol models.
- TypeScript is reserved for browser applications, generated types, and frontend tooling.

See [`docs/architecture.md`](docs/architecture.md) for the dependency model and state-ownership
rules, and [`docs/sdk-adapter.md`](docs/sdk-adapter.md) for the turn/decision adapter boundaries.

## Development

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest -q
```

The current foundation test verifies that every architectural package is importable. New behavior
must add focused unit tests and, where applicable, replay or contract fixtures.

## Safety

This repository contains no credentials or production configuration. Development and tests must use
fixtures or isolated output directories. Live submission and production deployment are intentionally
outside the default development workflow.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a change. Security issues should follow
[`SECURITY.md`](SECURITY.md). All community members are expected to follow the
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
