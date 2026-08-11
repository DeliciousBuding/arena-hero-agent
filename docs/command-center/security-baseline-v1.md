# Command Center write API security baseline (P5-2 snapshot)

Current-state baseline of the legacy TypeScript Command Center write surface,
extracted 2026-08-12 from `arena-hero-agent-ts/packages/command-center`. This is
the "as-is" reference for P5-9 (default-deny, authorization, CSRF/replay tests),
not an endorsement of the current posture.

## Binding and transport

- Server binds loopback only: `127.0.0.1`; port from `COMMAND_CENTER_PORT`,
  default `8787`.
- No TLS on the Command Center server itself.
- A second local server (supervisor debug API) is probed read-only on
  `127.0.0.1:8120`; failures fail open (unavailable state, not an error).

## Authentication

- **Not present.** No authentication or authorization middleware on any route.
  The model is loopback trust: anything able to reach `127.0.0.1:8787` can call
  every route, including all write routes.
- Shop routes authenticate to the *external* shop via a cookie supplied by the
  caller in the `X-Shop-Cookie` header; the cookie is forwarded and never stored
  server-side.

## Default deny

- **Not implemented as a blanket policy.** Validation is per-route allowlist
  validation only. Invalid input returns `400`; unknown paths return `404`.
- Allowlists in effect:
  - tenant ∈ `t1..t4` on tenant-scoped routes (simulator namespace `sim-*` is
    additionally accepted by the ingest route);
  - human command action type ∈ `VALID_ACTION_TYPES` (14 values);
  - human goal kind ∈ `mine|goto`;
  - human command mode ∈ `override|disabled`;
  - ingest event kind ∈ `register|connection|tick_summary|disconnected`;
  - registry mode ∈ `production|simulation`;
  - audit trail source ∈ `human|command|arbitration|supervisor`.

## CSRF

- **No CSRF protection on Command Center write routes** (loopback trust).
- The official shop proxy extracts `arena_shop_csrf` from the forwarded shop
  cookie and sends it as `X-CSRF-Token` to the shop on order/me/orders calls;
  this protects the *external* shop, not the Command Center itself.

## Replay and idempotency

Partial, per-write-path only:

- Human goal writes are deduped within a 30s window for the same unit, kind, and
  target (no file rewrite, no duplicate audit).
- Survey sync triggering is debounced to 60s and guarded by a lock file
  (`sync-guard.lock`, stale after 5 minutes).
- Agent ingest is an idempotent SQLite upsert.
- No generic idempotency keys on other write routes.

## Write paths and persistence targets

All paths are relative to the shared data root (`ARENA_DATA_ROOT`).

| Endpoint | Target | Concurrency / durability | Audit |
|---|---|---|---|
| `POST /api/command` | `data/runtime/human-commands/<tenant>.json` | atomic tmp+rename; per-unit replace | `data/runtime/human-command-audit.jsonl` |
| `POST /api/command/goal` | `data/runtime/human-commands/<tenant>.json` | atomic tmp+rename; 30s dedupe | `data/runtime/human-command-audit.jsonl` |
| `DELETE /api/command` | `data/runtime/human-commands/<tenant>.json` | atomic tmp+rename | `data/runtime/human-command-audit.jsonl` |
| `POST /api/command/clear` | `data/runtime/human-commands/<tenant>.json` | atomic tmp+rename | `data/runtime/human-command-audit.jsonl` |
| `POST /api/command/mode` | `data/runtime/human-commands/<tenant>.json` | atomic tmp+rename | `data/runtime/human-command-audit.jsonl` |
| `POST /api/alliance/survey/arbitrate` | `data/runtime/survey/arbitration.jsonl` | append-only | readable via GET |
| `POST /api/alliance/survey/arbitrate/clear` | `data/runtime/survey/arbitration.jsonl` | append-only (tombstone) | readable via GET |
| `POST /api/ingest/agents` | `data/runtime/survey/<tenant>.db` | SQLite WAL; busy timeout; per-event isolation; idempotent upsert | `agents`/`agent_events` tables |
| `POST /api/registry/agents` | `data/runtime/registry.db` | SQLite WAL; single writer | `keys` table (hash only) |
| `POST /api/registry/keys` | `data/runtime/registry.db` | SQLite WAL; single writer | `keys` table (hash only) |
| `DELETE /api/registry/agents/:id` | `data/runtime/registry.db` | SQLite WAL; soft revoke | `revoked_at` markers |
| `POST /api/leaderboard/refresh` | `data/leaderboard/leaderboard-<date>.json` + `history.jsonl` | snapshot write + history append; prune | `history.jsonl` |
| `POST /api/shop/history/refresh` | `data/runtime/shop-history.jsonl` | append on change | `shop-history.jsonl` |
| `POST /api/shop/order` | external official shop (outbound) | outbound fetch; nothing local | none local |
| `POST /api/redeem` | `data/runtime/redeem-log.jsonl` | append-only; masked code | `redeem-log.jsonl` |
| `GET /api/health/pipeline` (side effect) | spawns survey sync CLI (`survey:sync --latest-only`) | debounce 60s; lock file (stale 5 min) | `sync-guard.log` |

## Credential hygiene (current)

- Registry stores SHA-256 hashes of simulation keys; plaintext is returned exactly
  once at issue time and cannot be recovered afterwards.
- Production agents register only a key tail (`api_key_tail`), never the full key.
- Shop cookie travels in `X-Shop-Cookie` and is never persisted server-side.
- Redeem log stores only the first 6 characters of a code.
- No list/query response returns plaintext keys.

## Gaps and P5-9 targets

1. Add default-deny middleware plus explicit authorization on every write route.
2. Add CSRF/replay protection for Command Center write routes.
3. Add rate limiting on write routes.
4. Add idempotency keys on write routes that lack dedupe today.
5. Keep loopback-only binding, or introduce authenticated remote access explicitly
   (never implicitly).
