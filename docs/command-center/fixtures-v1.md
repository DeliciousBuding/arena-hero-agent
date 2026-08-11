# Command Center fixture & golden-data inventory (P5-2 snapshot)

> Derived from `docs/command-center/snapshot/command-center-snapshot-v1.json`;
  do not edit by hand.
> Regenerate with `uv run python scripts/snapshot_command_center.py --emit-docs`.

Hashes are SHA-256 over the exact files at the referenced repository path.

## Wire contracts (arena-hero-ts)

| Path | SHA-256 | Source | Tenant isolation | Purpose |
|------|---------|--------|------------------|---------|
| `packages/arena-hero-ts/contracts/fixtures/raw-ws/state-39961.json` | `6CAFF671C915C7048936DD90FD364A9F3F39C63A7DC07C5139FC35ACC44EBCC7` | burn-in raw-state dump; sanitized (owner_username replaced with fixture_user) | tenant-agnostic replay fixture; no production runtime data | golden replay for WS parse chain (W1) |
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
| `packages/arena-agent/test/fixtures/shared-data/schema/dataset-manifest-v1.schema.json` | `CC0224F9388DF9C3A73148A041DAE0F9BD7AC4F833A6BB1E57147E78819852D8` | shared data schema (versioned) | schema contract; no tenant data | dataset manifest schema |
| `packages/arena-agent/test/fixtures/shared-data/schema/dataset-registry-entry-v1.schema.json` | `7700817B305B818249F73937862DF3CFC33240C2012AB3B0B7476824A5819BC7` | shared data schema (versioned) | schema contract; no tenant data | dataset registry entry schema |
| `packages/arena-agent/test/fixtures/shared-data/schema/ml-sample-v1.schema.json` | `C7217E1A847A0A9C1434795CF03FB662DB1E63D8A5C8B0DFBFAF764D9DE30DED` | shared data schema (versioned) | schema contract; no tenant data | ml sample schema |
| `packages/arena-agent/test/fixtures/shared-data/schema/sim-calibration-case-v1.schema.json` | `180595999F9E330E8E1E4C6C44B245A384DFCA594BD490C67DA0F5594F3593BC` | shared data schema (versioned) | schema contract; no tenant data | sim calibration case schema |
| `packages/arena-agent/test/fixtures/shared-data/schema/sim-record-v1.schema.json` | `540279AAD60FF4DACFCD2675BD43B0F0E3E462ACEA7D3C40A8AAEE247DF4DED8` | shared data schema (versioned) | schema contract; no tenant data | sim record schema |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/dataset-manifest-v1.valid.json` | `549811AB692D579724F6FA6702240E1782503FF4034662D898B643E76CFB5469` | schema fixture (valid sample) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/dataset-registry-entry-v1.valid.json` | `E230CB062BC07265A0625FFD65A47EC651BD1B10EA2C8F57A9ECAFB3D3B7AFC2` | schema fixture (valid sample) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/ml-sample-v1.sample-status.json` | `373513B0C0506120544C02BF8096738306101B42494FAEEE79D041C1AA7EB66B` | schema fixture (sample status) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/ml-sample-v1.valid.json` | `95F550F2015B0EF0F5CAA51116CB4BF0D17A9F3273DB09E4583EBE195D1F8748` | schema fixture (valid sample) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/sim-calibration-case-v1.v0.14.valid.json` | `4E83814B0509AB28EA4AEF443CB0BB22D4958A260983B571CD15CA57E2F49F0E` | schema fixture (valid sample, v0.14 rules) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/sim-calibration-case-v1.valid.json` | `5BDD63FAAA1D11F2235217089D26C4C098589ECDE795DB649A5C234FAE723169` | schema fixture (valid sample) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/shared-data/schema/fixtures/sim-record-v1.valid.json` | `5666647B9D7D95243506A7CF56A6474358F6FA7C7A01EDF5D715CB89AB243A93` | schema fixture (valid sample) | synthetic; no tenant data | schema validation fixture |
| `packages/arena-agent/test/fixtures/sim/scenario-basic.json` | `B31BF1F76CAE57A37950A02AF7FCD30E8CEC0E40576699D12411A99FF9494849` | sim scenario fixture | no tenant attribution; simulator namespace | simulator scenario |
| `packages/arena-agent/test/fixtures/sim/scenario-basic-v0.14.json` | `3CC949B8795264A6B39D0BA38C46DA37A959ABFBBFE64F66C6390BF83F4B173D` | sim scenario fixture (v0.14 rules) | no tenant attribution; simulator namespace | simulator scenario |
| `packages/arena-agent/test/fixtures/sim/scenario-clear-path.json` | `5292877D72E797D532C8D3E16EA48BDD659B297052DFCA0504F0E9D274014C1F` | sim scenario fixture | no tenant attribution; simulator namespace | simulator scenario |
| `packages/arena-agent/test/fixtures/sim/scenario-ffa-3p-v0.14.json` | `7E25A1AEC2CB25C9F97EC1405CBE5B6AC8E28AF895F7BE577C3E3C8DC01905B7` | sim scenario fixture (v0.14 rules, 3 players) | no tenant attribution; simulator namespace | simulator scenario |
| `packages/arena-agent/test/fixtures/sim/scenario-focus-exile.json` | `B31BF1F76CAE57A37950A02AF7FCD30E8CEC0E40576699D12411A99FF9494849` | sim scenario fixture | no tenant attribution; simulator namespace | simulator scenario |
| `packages/arena-agent/test/fixtures/sim/calibration-wait-match.json` | `237A0A6EEAE6DACEA6A6E49BCCDC630E51C367048C77DBE5F3519E0279BE37BE` | calibration wait-match fixture | no tenant attribution; simulator namespace | calibration matching |
| `packages/arena-agent/test/fixtures/sim/calibration-dataset-match/case.json` | `708677135513DD2D1228FF811FDC74C566812680E00F52E6C6321704067B8DE3` | calibration dataset match fixture | no tenant attribution; simulator namespace | calibration dataset matching |
| `packages/arena-agent/test/fixtures/sim/calibration-dataset-match/manifest.json` | `715C6BCA20188630B7CB4D9F065173052A25C32771D88B96A155600DE3A4A88C` | calibration dataset match fixture | no tenant attribution; simulator namespace | calibration dataset matching |

## Python known-answer golden data (arena-hero-agent)

| Path | SHA-256 | Source | Tenant isolation | Purpose |
|------|---------|--------|------------------|---------|
| `tests/domain/fixtures/tenant_state_reducer_known_answers_v1.json` | `4263700F184666773B910CF5D30B2338375C0109C5BA4AE5A2EF46D660F92A90` | migrated golden data (TS behavior pinned to Python) | tenant-agnostic; per-tenant runtime data excluded | known-answer golden for tenant state reducer v1 |
| `tests/domain/fixtures/tenant_state_reducer_known_answers_v2.json` | `06DC246BA73F8B5E897623DF30D60B69858760144FFCE4F71BB54C74033793CD` | migrated golden data (TS behavior pinned to Python) | tenant-agnostic; per-tenant runtime data excluded | known-answer golden for tenant state reducer v2 |
| `tests/domain/fixtures/ts_world_nav_known_answers.json` | `D60F136CE280F8E4045D92F40BDC061938A47FA2FFC5E3E78B9188D315F23D3F` | migrated golden data (TS navigation pinned to Python) | tenant-agnostic; per-tenant runtime data excluded | known-answer golden for world navigation |
| `tests/adapters/sdk/fixtures/turn_to_plan_known_answers_v1.json` | `836A0D7D51C1855AD326D316CA65B61A889FC48751AE993B2ADB167704064EC5` | migrated golden data (turn-to-plan pinned) | tenant-agnostic; per-tenant runtime data excluded | known-answer golden for turn-to-plan adapter |
| `tests/cli/fixtures/replay_turns_v1.json` | `BA4A7E5D38514497C70D248BAFD261025CB4C8087E1503F07403F2BCF40837B7` | migrated golden data (replay turn sequence) | tenant-agnostic; per-tenant runtime data excluded | known-answer golden for replay |

Total: 33 fixtures.
