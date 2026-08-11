# Arena Hero Agent

Arena Hero Agent is a Python-first runtime for building deterministic game strategies, coordinating
multiple tenants, and exposing an operator-facing control plane. The repository keeps the decision
engine independent from transport, storage, and deployment details so it can be tested offline and
integrated through replaceable adapters.

> **Status:** early architecture foundation with a working offline turn-to-decision adapter chain.
> Production game behavior, persistent storage, and deployment integration are still under
> development.

## Current capabilities

- **SDK turn adaptation** (`adapt_async_turn`): converts an `arena-hero` 0.2.x `AsyncTurn` /
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

## Not yet implemented

The following Phase 4 items are intentionally out of scope for the current milestone and will be
added in later waves:

- a single-tenant tick loop with deadline budgets and reconnect handling;
- a SQLite recorder with atomic write and recovery tests;
- telemetry wiring into the decision path (failure isolation);
- a CLI entrypoint, `doctor`, and systemd readiness/health integration.

## Planned capabilities

- deterministic strategy and planning pipelines;
- tenant-scoped lifecycle, lease fencing, recovery, and audit trails;
- alliance and migration coordination through versioned commands;
- SDK, filesystem, SQLite, subprocess, and HTTP adapters;
- a Python control API with generated browser types;
- a React-based Command Center for read models and authorized operations.

## Repository boundaries

- [`arena-hero`](https://pypi.org/project/arena-hero/) provides the Python SDK and official
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
