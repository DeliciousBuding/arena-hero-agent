# Arena Hero SDK adapter

## Scope

The SDK adapter implements the application-owned `GameClient` port with the public
`arena-hero` Python distribution. It keeps SDK event, command, and acknowledgement models owned by
the SDK; the Agent does not copy `Turn`, action, event, or wire schemas.

Supported dependency range is `arena-hero>=0.2.9,<0.3`. The lock file currently resolves exactly
`0.2.9`. At composition time, the adapter validates these public names:

- `AsyncArenaHeroClient`, whose `events()` iterator yields `Tick`, `AsyncTurn`, and `Received`;
- `CommandPlan` submitted by `AsyncArenaHeroClient.submit(..., idempotency_key=...)`;
- `Accepted`, including matching `tick` and `CommandSource.AGENT` acknowledgement fields;
- the public SDK error hierarchy and `Direction` enum.

The evidence above comes from the installed PyPI wheel's exported `arena_hero.__all__`, public
signatures, and runtime version metadata. Private SDK modules are not imported by the adapter.

## Composition

A process composition root passes credentials and endpoints explicitly:

```python
from arena_hero_agent.adapters.sdk import create_sdk_game_client

client = create_sdk_game_client(
    api_key=settings.api_key,
    base_url=settings.base_url,
    websocket_url=settings.websocket_url,
)
```

Importing `arena_hero_agent.adapters.sdk` does not import the SDK. The SDK is loaded when
`load_sdk_bindings()` or `create_sdk_game_client()` is called. Constructing a client does not itself
start the event stream; network work begins only when the application consumes `events()` or calls
`submit()`.

Tests inject an SDK-shaped async client into `ArenaHeroSdkGameClient`, use SDK-owned Pydantic
models, and prohibit socket connections. They do not use credentials or live endpoints.

## Turn and decision adaptation

Two pure functions close the loop between SDK-owned turns and SDK-owned command plans without
touching the wire:

- `adapt_async_turn(turn)` converts one exact `AsyncTurn` into an immutable
  `TurnObservation` (lifecycle, resources, population, `WorldProjection`, and resolution events).
- `build_command_plan(decision, observation)` converts an immutable application `Decision`
  (unit and core intents) into an SDK `CommandPlan` after validating it against the observed tick.
- `command_plan_payload(plan)` returns an environment-neutral JSON payload of a `CommandPlan` for
  fixtures and deterministic digests.

Both functions are offline, deterministic, and fail closed with `SdkContractViolationError` on:

- non-exact SDK or application shapes (for example a `Tick` instead of an `AsyncTurn`);
- malformed or non-positive ticks;
- unknown player status, unit type, core state, beacon status, or object kinds;
- duplicate object, terrain-cell, or event identities;
- resolution events whose tick does not match the turn tick;
- tick mismatches between a decision and its observation;
- intents referencing unknown controlled units or unknown shoot targets;
- actions that are invalid for the observed unit role;
- duplicate unit intents or intents targeting a missing controlled core;
- SDK releases that drop an action constructor or add an enum member the adapter does not
  recognize.

A versioned known-answer fixture (`tests/adapters/sdk/fixtures/`) pins the complete
Turn -> projection -> decision -> plan payload chain and its deterministic SHA-256 digest against
`arena-hero==0.2.9`. The fixture is environment-neutral and contains no local paths.

## Boundary semantics

- Events remain SDK-owned objects and are checked against `Tick | AsyncTurn | Received` before
  crossing the port.
- A domain `DecisionId` is forwarded as the SDK idempotency key. IDs that do not satisfy the SDK's
  8-to-128 visible ASCII requirement fail as contract violations before submission.
- `Accepted` must match the submitted plan tick and have source `AGENT`.
- Domain directions map explicitly: north/up, east/right, south/down, west/left. Unknown values are
  rejected; the Agent never relies on matching enum values by accident.
- `close()` is idempotent. Cleanup runs in one shielded task, so cancelling a caller does not cancel
  the underlying close. Work requested while closing or after close fails permanently.
- `asyncio.CancelledError` is re-raised unchanged.

Adapter failures preserve four meanings without choosing an application retry policy:

| Meaning | Adapter behavior |
|---|---|
| retryable | `SdkRetryableError` for transport failures and HTTP 408/409/425/429/5xx |
| permanent | `SdkPermanentError` for authentication, configuration, policy, action, turn, and other terminal API failures |
| cancelled | the original `asyncio.CancelledError` propagates unchanged |
| contract violation | `SdkContractViolationError` for protocol errors, missing public API, bad SDK shapes, mismatched acknowledgements, or unknown enums |

The SDK performs its own documented safe HTTP retries and event-stream reconnects. The adapter does
not add a second retry loop; application orchestration decides whether and when to retry a
`SdkRetryableError`.

## Compatibility changes

Any upgrade within `0.2.x` must rerun the contract tests and the versioned known-answer fixture
test. A release that removes required exports, changes event classes, changes submission or
acknowledgement semantics, or adds a direction is rejected loudly. `0.3` and later require an
explicit adapter review before the accepted version range changes.
