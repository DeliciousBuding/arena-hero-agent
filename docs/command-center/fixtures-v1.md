# Command Center fixture & golden-data inventory (P5-2 snapshot)

> Derived from `docs/command-center/snapshot/command-center-snapshot-v1.json`;
  do not edit by hand.
> Regenerate with `uv run python scripts/snapshot_command_center.py --emit-docs`.

Hashes are SHA-256 over the exact files at the referenced repository path.

## Wire contracts (arena-hero-ts)

| Path | SHA-256 | Source | Tenant isolation | Purpose |
|------|---------|--------|------------------|---------|
| `packages/arena-hero-ts/contracts/fixtures/raw-ws/state-39961.json` | `9E04689B14D3FB04CD0DEECD1B7F2FDE1D405C590CD385D62F59AA2A9585E60E` | burn-in raw-state dump; sanitized (owner_username replaced with fixture_user) | tenant-agnostic replay fixture; no production runtime data | golden replay for WS parse chain (W1) |
| `packages/arena-hero-ts/contracts/generated/accepted.schema.json` | `646B32C52BD33002E0E42ADA5CBD1FD073621E795883F60AF0EB2EB17B46AA37` | generated wire schema (TypeBox source) | protocol-level; no tenant attribution | wire contract: accepted envelope |
| `packages/arena-hero-ts/contracts/generated/command-plan.schema.json` | `2C46209C09471E232A203187B33E034C2D746A3BBE7083CC8B448342C0F0F3D5` | generated wire schema (TypeBox source) | protocol-level; no tenant attribution | wire contract: command plan |
| `packages/arena-hero-ts/contracts/generated/player-state.schema.json` | `01B7040869A38FB6F40CE127F23A21171A3E846EBB6FD57A2AF26E6C65D0A706` | generated wire schema (TypeBox source) | protocol-level; no tenant attribution | wire contract: player state |
| `packages/arena-hero-ts/contracts/generated/received.schema.json` | `C0EDB80FE803BEE205FBC1F006A9C4C91B77452B6CE34EAF955259BCA0FC747D` | generated wire schema (TypeBox source) | protocol-level; no tenant attribution | wire contract: received plan envelope |
| `packages/arena-hero-ts/contracts/generated/stream-envelope.schema.json` | `A3B6A578CD4C1C3A738D236E9281235E60BB43E125058DF7250884C99ECAEF24` | generated wire schema (TypeBox source) | protocol-level; no tenant attribution | wire contract: WS stream envelope (tick/state/received) |
| `packages/arena-hero-ts/contracts/generated/world-object.schema.json` | `14C8D28EA155E510E7F4CD1FE2444A90F8F4DA6F39DD04D1F670A8CE48379F5E` | generated wire schema (TypeBox source) | protocol-level; no tenant attribution | wire contract: world object discriminated union |

## TS agent fixtures (arena-agent)

| Path | SHA-256 | Source | Tenant isolation | Purpose |
|------|---------|--------|------------------|---------|
| `packages/arena-agent/test/fixtures/synthetic-match-opponent-plan.json` | `8A85C8D2F1EF402A69CA642B5812E956B5174358B1010820AB0ABE4E67991FDA` | synthetic opponent plan fixture | no tenant attribution; simulator namespace | agent decision/plan tests |
| `packages/arena-agent/test/fixtures/shared-data/schema/dataset-manifest-v1.schema.json` | `6A8CE042E9605C1D293C82A6B0BE42D187B9E1907B50E2326080DE7031C83A61` | shared data schema (versioned) | schema contract; no tenant data | dataset manifest schema |
| `packages/arena-agent/test/fixtures/shared-data/schema/dataset-registry-entry-v1.schema.json` | `4AF6E4B92B4DCF11A9A11CBB2049E9ADD8B47BD9CE5ABF252441859DDD9B1CBE` | shared data schema (versioned) | schema contract; no tenant data | dataset registry entry schema |
| `packages/arena-agent/test/fixtures/shared-data/schema/ml-sample-v1.schema.json` | `F93C71CFAE36A9710FCCE484A4153376EF63D4DF9878B116F9A13C23C0369546` | shared data schema (versioned) | schema contract; no tenant data | ml sample schema |
| `packages/arena-agent/test/fixtures/shared-data/schema/sim-calibration-case-v1.schema.json` | `567D7C51869039A03CD9362092261E8C18DBDCEC3BFAB50A5D556065C11B8A81` | shared data schema (versioned) | schema contract; no tenant data | sim calibration case schema |
| `packages/arena-agent/test/fixtures/shared-data/schema/sim-record-v1.schema.json` | `0ABB435297662A0A58A986990BD602E88302B1EB29B86AB89348448BA736F12E` | shared data schema (versioned) | schema contract; no tenant data | sim record schema |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/dataset-manifest-v1.valid.json` | `8C48E94A0C53BA95B10B583EF36040292BB0154E5A4893C447E8D325018E870A` | schema fixture (valid sample) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/dataset-registry-entry-v1.valid.json` | `F4EC335A29CADB8D960B7A80FD0A6BE6349171E4F0C3B0133ED66425DF728CBA` | schema fixture (valid sample) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/ml-sample-v1.sample-status.json` | `A33D4568A5A87A47B3BECCFB8C9D03EC5C9DD8579A85B8A31C2EB64E1E6346F8` | schema fixture (sample status) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/ml-sample-v1.valid.json` | `DFF2DABDB9C857691DA2AB3928812C9E557F7B3797B43238BE49995256CE8442` | schema fixture (valid sample) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/sim-calibration-case-v1.v0.14.valid.json` | `D39E9332533C16C85232F21AD4AEE25A9BFEF9C3BEB877D45426DAA4B4AED063` | schema fixture (valid sample, v0.14 rules) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/sim-calibration-case-v1.valid.json` | `B4C315DE002D92A8B6532BC6763F2566040BD7AD68BDF2473DF3C958FCFA9922` | schema fixture (valid sample) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/sim-record-v1.valid.json` | `245BB4F3DF69E3192371BE55410A1CF9152C2F720A82F902EC50FDB2411C9CB8` | schema fixture (valid sample) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/sim/scenario-basic.json` | `45DEF943B32D96F50A2968956160E692BE3D52870CA30DD83251FFBFA4B0EEB6` | sim scenario fixture | no tenant attribution; simulator namespace | simulator scenario |
| `packages/arena-agent/test/fixtures/sim/scenario-basic-v0.14.json` | `71966A5E2B7F85943342C30ABE7825025A2E6CC290AA637E79C12DEB69E84555` | sim scenario fixture (v0.14 rules) | no tenant attribution; simulator namespace | simulator scenario |
| `packages/arena-agent/test/fixtures/sim/scenario-clear-path.json` | `38CEEFAC09C1274425D0EF6EA6783762C2CBBD61225C8DB5B9FCBACFE3FC8D44` | sim scenario fixture | no tenant attribution; simulator namespace | simulator scenario |
| `packages/arena-agent/test/fixtures/sim/scenario-ffa-3p-v0.14.json` | `7E25A1AEC2CB25C9F97EC1405CBE5B6AC8E28AF895F7BE577C3E3C8DC01905B7` | sim scenario fixture (v0.14 rules, 3 players) | no tenant attribution; simulator namespace | simulator scenario |
| `packages/arena-agent/test/fixtures/sim/scenario-focus-exile.json` | `45DEF943B32D96F50A2968956160E692BE3D52870CA30DD83251FFBFA4B0EEB6` | sim scenario fixture | no tenant attribution; simulator namespace | simulator scenario |
| `packages/arena-agent/test/fixtures/sim/calibration-wait-match.json` | `237A0A6EEAE6DACEA6A6E49BCCDC630E51C367048C77DBE5F3519E0279BE37BE` | calibration wait-match fixture | no tenant attribution; simulator namespace | calibration matching |
| `packages/arena-agent/test/fixtures/sim/calibration-dataset-match/case.json` | `708677135513DD2D1228FF811FDC74C566812680E00F52E6C6321704067B8DE3` | calibration dataset match fixture | no tenant attribution; simulator namespace | calibration dataset matching |
| `packages/arena-agent/test/fixtures/sim/calibration-dataset-match/manifest.json` | `715C6BCA20188630B7CB4D9F065173052A25C32771D88B96A155600DE3A4A88C` | calibration dataset match fixture | no tenant attribution; simulator namespace | calibration dataset matching |

## Python known-answer golden data (arena-hero-agent)

| Path | SHA-256 | Source | Tenant isolation | Purpose |
|------|---------|--------|------------------|---------|
| `tests/domain/fixtures/tenant_state_reducer_known_answers_v1.json` | `4263700F184666773B910CF5D30B2338375C0109C5BA4AE5A2EF46D660F92A90` | migrated golden data (TS behavior pinned to Python) | tenant-agnostic; per-tenant runtime data excluded | known-answer golden for tenant state reducer v1 |
| `tests/domain/fixtures/tenant_state_reducer_known_answers_v2.json` | `06DC246BA73F8B5E897623DF30D60B69858760144FFCE4F71BB54C74033793CD` | migrated golden data (TS behavior pinned to Python) | tenant-agnostic; per-tenant runtime data excluded | known-answer golden for tenant state reducer v2 |
| `tests/domain/fixtures/ts_world_nav_known_answers.json` | `D50A2667571810D0C78477DF9B3286A6F383BE3DADE3047E04CCB694136AAE78` | migrated golden data (TS navigation pinned to Python) | tenant-agnostic; per-tenant runtime data excluded | known-answer golden for world navigation |
| `tests/adapters/sdk/fixtures/turn_to_plan_known_answers_v1.json` | `836A0D7D51C1855AD326D316CA65B61A889FC48751AE993B2ADB167704064EC5` | migrated golden data (turn-to-plan pinned) | tenant-agnostic; per-tenant runtime data excluded | known-answer golden for turn-to-plan adapter |
| `tests/cli/fixtures/replay_turns_v1.json` | `BA4A7E5D38514497C70D248BAFD261025CB4C8087E1503F07403F2BCF40837B7` | migrated golden data (replay turn sequence) | tenant-agnostic; per-tenant runtime data excluded | known-answer golden for replay |

Total: 33 fixtures.
