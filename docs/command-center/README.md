# Command Center migration snapshot (P5-2)

Read-only snapshot of the legacy TypeScript Command Center surface, produced as the
pre-migration baseline for the Python Command Center (P5-3 onward). It extracts
contracts and facts only; no TypeScript implementation source is copied into this
repository.

## Scope

| Package | Role in snapshot |
|---|---|
| `arena-hero-agent-ts/packages/command-center` | HTTP routes, write-permission semantics, security posture |
| `arena-hero-agent-ts/packages/arena-hero-ts` | wire contract schemas and golden replay fixture |

Provenance:

- TS checkout commit: `8cf5cbbcccf396a8feee94404af44969c5388e15` (command-center package clean at extraction).
- Agent baseline: `main@0cd1580` (branch `w8/p5-2`).
- Extraction date: 2026-08-12.

## Files

| File | Content |
|---|---|
| `snapshot/command-center-snapshot-v1.json` | machine-readable manifest (single source of truth) |
| `endpoints-v1.md` | derived route/endpoint inventory (66 API + 5 static) |
| `fixtures-v1.md` | derived fixture & golden-data inventory with SHA-256 |
| `security-baseline-v1.md` | write API security baseline and P5-9 targets |

## Regeneration

```bash
uv run python scripts/snapshot_command_center.py --check    # verify reproducibility (exit 1 on drift)
uv run python scripts/snapshot_command_center.py --refresh  # recompute hashes + rewrite manifest
uv run python scripts/snapshot_command_center.py --emit-docs  # rewrite derived markdown views
```

The checker re-reads the referenced legacy sources (read-only) and:

- cross-checks the route inventory against `server.ts` route registrations;
- recomputes every fixture SHA-256;
- recomputes the deterministic manifest hash.

The manifest hash is stable: it excludes the `manifest_hash` field and uses
canonical JSON (sorted keys, compact separators). No timestamps enter the hash.

## Sanitization

The snapshot intentionally contains no private paths, credentials, model names,
session text, or copied TypeScript source. Paths are expressed relative to the
referenced repository roots (for example `data/runtime/human-commands/<tenant>.json`).

## Consumers

- P5-3 JSONL/SQLite/cache base migration: use the fixture inventory for field-by-field
  comparison sources.
- P5-5 Python API/OpenAPI/ETag/stream: use the endpoint inventory for route
  compatibility and the stream characteristics section.
- P5-9 write API security: use `security-baseline-v1.md` as the current-state
  baseline for default-deny, authorization, and CSRF/replay tests.
