# Arena Hero Agent contributor rules

## Scope

This repository contains the Python Agent runtime, its control-plane API, and browser-based
operator interfaces. Simulation, benchmarking, and research belong to `arena-hero-lab`; official
wire and telemetry contracts belong to the `arena-hero` Python SDK.

## Architecture boundaries

- Keep domain models and invariants independent of filesystems, databases, HTTP, and SDK clients.
- Define replaceable interfaces in `ports`; concrete integrations belong in `adapters`.
- Keep orchestration in `application` and process composition in `cli`.
- Do not import simulator or research implementation code into the Agent runtime.
- Cross-tenant coordination may publish versioned commands, but it must not mutate another
  tenant's runtime state directly.
- Browser code may render and submit commands, but it must not implement authoritative game
  rules, strategy decisions, or KPI aggregation.
- TypeScript is limited to browser applications, generated API/schema types, and frontend build
  tooling.

## Safety boundaries

- Never commit API keys, tokens, cookies, private endpoints, local absolute paths, or production
  runtime data.
- Live submission must be explicit and separately authorized; tests and local development must use
  fixtures, fakes, or isolated output directories.
- A durable write or game submission requires a tenant-scoped lease with fencing.
- Do not use destructive Git operations to discard work. Use repository-local
  `.worktrees/<task-id>` directories for parallel changes.

## Public repository hygiene

- Write documentation for external users and contributors, not for a private session or a specific
  workstation.
- Use product terminology and repository-relative paths.
- Put historical migration notes in clearly marked archival documents; keep active docs focused on
  supported behavior and current interfaces.

## Quality gates

```bash
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest -q
```

Run all gates from a clean worktree before integration.
