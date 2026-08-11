# Command Center endpoint inventory (P5-2 snapshot)

> Derived from `docs/command-center/snapshot/command-center-snapshot-v1.json`;
  do not edit by hand.
> Regenerate with `uv run python scripts/snapshot_command_center.py --emit-docs`.

## API routes

| # | Method | Path | Tenant | ETag | Stream | Cache | Write semantics |
|---|--------|------|--------|------|--------|-------|-----------------|
| 1 | GET | `/api/tenants` | - | - | poll-json | supervisor 5s ttl | read-only |
| 2 | GET | `/api/overview` | - | - | poll-json | supervisor 5s ttl | read-only |
| 3 | GET | `/api/map` | - | W/<map-sig> | poll-json | public, max-age=2 | read-only |
| 4 | GET | `/api/map/lod` | all|tN | - | poll-json | 30s ttl + warmup | read-only |
| 5 | GET | `/api/stream` | t1 | - | poll-json | direct tail read | read-only |
| 6 | GET | `/api/world` | t1 | - | poll-json | direct read | read-only |
| 7 | GET | `/api/replay` | t1 | - | poll-json | direct read | read-only |
| 8 | GET | `/api/plan` | t1 | - | poll-json | direct read | read-only |
| 9 | GET | `/api/exploration` | t1 | - | poll-json | survey cache 30s | read-only |
| 10 | GET | `/api/survey` | all|tN | - | poll-json | survey cache 30s | read-only |
| 11 | GET | `/api/survey/mine` | t1 | - | poll-json | direct survey-db read | read-only |
| 12 | GET | `/api/survey/enemy-cores` | - | - | poll-json | direct db + snapshot | read-only |
| 13 | GET | `/api/survey/decision-input` | tN | - | poll-json | 30s ttl + warmup | read-only |
| 14 | GET | `/api/survey/mine-patterns` | all|tN | - | poll-json | 30s ttl + warmup | read-only |
| 15 | GET | `/api/deeds` | all|tN | - | poll-json | 45s ttl + warmup | read-only |
| 16 | GET | `/api/deeds/journal` | all|tN | - | poll-json | 30s ttl | read-only |
| 17 | GET | `/api/alliance/survey` | - | - | poll-json | 30s aggregate | read-only |
| 18 | GET | `/api/alliance/survey/arbitrations` | - | - | poll-json | direct read | read-only |
| 19 | POST | `/api/alliance/survey/arbitrate` | - | - | poll-json | none | write: append arbitration.jsonl |
| 20 | POST | `/api/alliance/survey/arbitrate/clear` | - | - | poll-json | none | write: remove arbitration entry |
| 21 | GET | `/api/alliance/cluster` | - | - | poll-json | 30s ttl | read-only |
| 22 | GET | `/api/alliance/snapshot` | - | - | poll-json | 30s ttl | read-only |
| 23 | GET | `/api/alliance/advice` | - | - | poll-json | 30s ttl | read-only |
| 24 | GET | `/api/alliance/defense` | - | - | poll-json | 30s (snapshot ttl) | read-only |
| 25 | GET | `/api/alliance/director` | - | - | poll-json | supervisor 5s ttl | read-only |
| 26 | GET | `/api/alliance/exploration` | - | - | poll-json | 30s ttl + warmup | read-only |
| 27 | GET | `/api/events` | t1 | - | poll-json | direct read | read-only |
| 28 | GET | `/api/leaderboard` | - | - | poll-json | lazy refresh check | read-only |
| 29 | POST | `/api/leaderboard/refresh` | - | - | poll-json | none | write: external fetch -> snapshot json + history.jsonl |
| 30 | GET | `/api/intel` | - | - | poll-json | 30s lazy cache | read-only |
| 31 | GET | `/api/health/pipeline` | - | - | poll-json | 15s ttl | read-only |
| 32 | GET | `/api/intel/heat` | all|tN | - | poll-json | 30s ttl | read-only |
| 33 | GET | `/api/commands` | tN | - | poll-json | direct + reconcile | read + reconcile (may write-back cleanup) |
| 34 | GET | `/api/audit/decisions` | all|tN | - | poll-json | 30s ttl + warmup | read-only |
| 35 | GET | `/api/audit/decisions/trend` | tN | - | poll-json | 30s ttl | read-only |
| 36 | GET | `/api/audit/workers` | all|tN | - | poll-json | 5s ttl | read-only |
| 37 | GET | `/api/audit/lifecycle` | all|tN | - | poll-json | 30s ttl + warmup | read-only |
| 38 | GET | `/api/alliance/mining` | - | - | poll-json | 30s ttl + warmup | read-only |
| 39 | GET | `/api/audit/alignment` | - | - | poll-json | 30s ttl | read-only |
| 40 | GET | `/api/audit/trail` | all|tN | - | poll-json | 30s lazy ttl | read-only |
| 41 | GET | `/api/alliance/survey/mining` | - | - | poll-json | 30s ttl | read-only |
| 42 | GET | `/api/audit/mining-effectiveness` | - | - | poll-json | 30s ttl | read-only |
| 43 | GET | `/api/audit/overview` | - | - | poll-json | 30s ttl | read-only |
| 44 | GET | `/api/audit/mines/trend` | tN | - | poll-json | 30s ttl | read-only |
| 45 | GET | `/api/audit/mines` | all|tN | - | poll-json | 30s ttl + warmup | read-only |
| 46 | GET | `/api/audit/human/conflicts` | all|tN | - | poll-json | 30s ttl + warmup | read-only |
| 47 | GET | `/api/audit/human` | tN | - | poll-json | direct read | read-only |
| 48 | POST | `/api/command` | tN | - | poll-json | none | write: human-commands/<tenant>.json (atomic) |
| 49 | POST | `/api/command/goal` | tN | - | poll-json | none | write: human-commands/<tenant>.json (atomic) |
| 50 | DELETE | `/api/command` | tN | - | poll-json | none | write: human-commands/<tenant>.json (atomic) |
| 51 | POST | `/api/command/clear` | tN | - | poll-json | none | write: human-commands/<tenant>.json (atomic) |
| 52 | POST | `/api/command/mode` | tN | - | poll-json | none | write: human-commands/<tenant>.json (atomic) |
| 53 | GET | `/api/shop/history` | - | - | poll-json | 30s ttl | read-only |
| 54 | POST | `/api/shop/history/refresh` | - | - | poll-json | none | write: external fetch -> shop-history.jsonl append on change |
| 55 | GET | `/api/shop` | - | - | poll-json | external 20s cache | read-only |
| 56 | GET | `/api/shop/me` | - | - | poll-json | external | read-only |
| 57 | GET | `/api/shop/orders` | - | - | poll-json | external | read-only |
| 58 | POST | `/api/shop/order` | - | - | poll-json | none | write: external order via official shop |
| 59 | POST | `/api/redeem` | - | - | poll-json | none | write: redeem-log.jsonl append (masked code) |
| 60 | GET | `/api/redeem/history` | - | - | poll-json | direct read | read-only |
| 61 | POST | `/api/ingest/agents` | - | - | poll-json | none | write: survey/<tenant>.db (agents + mapping tables) |
| 62 | GET | `/api/agents` | - | - | poll-json | direct read | read-only |
| 63 | POST | `/api/registry/agents` | - | - | poll-json | none | write: registry.db (SQLite WAL) |
| 64 | GET | `/api/registry/agents` | - | - | poll-json | direct read | read-only |
| 65 | POST | `/api/registry/keys` | - | - | poll-json | none | write: registry.db (SQLite WAL) |
| 66 | DELETE | `/api/registry/agents/:id` | - | - | poll-json | none | write: registry.db (SQLite WAL) |

## Static routes

| Method | Path |
|--------|------|
| GET | `/` |
| GET | `/app` |
| GET | `/app/*` |
| GET | `/assets/*` |
| GET | `/style.css` |

## Stream characteristics

- All Command Center routes are request/response JSON polling (`poll-json`);
  there is no SSE or WebSocket endpoint in the Command Center server.
- `/api/stream` returns a bounded tail of the per-tenant decision stream
  (`n` clamped to 1..200); the name is legacy, the transport is plain JSON polling.
- The WebSocket wire contract lives in the `arena-hero-ts` package
  (`stream-envelope` schema, tick/state/received messages); see the fixture
  inventory for the schema hashes.
- Only `/api/map` uses an HTTP-level weak ETag (`W/<map-sig>`) with
  `cache-control: public, max-age=2` and 304 handling; all other endpoints rely
  on in-memory TTL caches or direct reads.

Counts: 66 API routes, 5 static routes.
