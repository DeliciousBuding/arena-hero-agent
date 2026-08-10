# Arena Hero Agent

Arena Hero Agent is a Python-first runtime for building deterministic game strategies, coordinating
multiple tenants, and exposing an operator-facing control plane. The repository keeps the decision
engine independent from transport, storage, and deployment details so it can be tested offline and
integrated through replaceable adapters.

> **Status:** early architecture foundation. Package boundaries are available, but production game
> behavior and stable public APIs are still under development.

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
rules.

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
[`SECURITY.md`](SECURITY.md).
