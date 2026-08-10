from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest
from arena_hero import (
    Accepted,
    APIError,
    AsyncGameEvent,
    AsyncTurn,
    AuthenticationError,
    ChampionBeacon,
    CommandPlan,
    CommandSource,
    PlayerState,
    PlayerStatus,
    ProtocolError,
    Received,
    Tick,
    TransportError,
)
from arena_hero import (
    Direction as SdkDirection,
)

from arena_hero_agent.adapters.sdk import (
    ArenaHeroSdkGameClient,
    SdkContractViolationError,
    SdkPermanentError,
    SdkRetryableError,
    create_sdk_game_client,
    from_sdk_direction,
    load_sdk_bindings,
    to_sdk_direction,
)
from arena_hero_agent.domain import DecisionId, Direction
from arena_hero_agent.ports import GameClient


def _accepted(*, tick: int = 1, source: CommandSource = CommandSource.AGENT) -> Accepted:
    return Accepted(
        accepted=True,
        tick=tick,
        source=source,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _events(*items: object, error: BaseException | None = None) -> AsyncIterator[AsyncGameEvent]:
    async def iterate() -> AsyncIterator[AsyncGameEvent]:
        for item in items:
            yield cast(AsyncGameEvent, item)
        if error is not None:
            raise error

    return iterate()


class FakeSdkClient:
    def __init__(
        self,
        *,
        event_items: tuple[object, ...] = (),
        event_error: BaseException | None = None,
        accepted: object | None = None,
        submit_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.event_items = event_items
        self.event_error = event_error
        self.accepted = accepted or _accepted()
        self.submit_error = submit_error
        self.close_error = close_error
        self.submissions: list[tuple[CommandPlan, str | None]] = []
        self.close_calls = 0

    def events(self) -> AsyncIterator[AsyncGameEvent]:
        return _events(*self.event_items, error=self.event_error)

    async def submit(self, plan: CommandPlan, *, idempotency_key: str | None = None) -> Accepted:
        self.submissions.append((plan, idempotency_key))
        if self.submit_error is not None:
            raise self.submit_error
        return cast(Accepted, self.accepted)

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


async def _collect(client: ArenaHeroSdkGameClient) -> list[AsyncGameEvent]:
    return [event async for event in client.events()]


def test_importing_adapter_package_does_not_eagerly_import_sdk() -> None:
    code = """
import sys
assert 'arena_hero' not in sys.modules
import arena_hero_agent.adapters.sdk
assert 'arena_hero' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_load_bindings_uses_pinned_public_029_surface() -> None:
    bindings = load_sdk_bindings()

    assert bindings.version == "0.2.9"
    assert bindings.async_client_type.__name__ == "AsyncArenaHeroClient"
    assert {event_type.__name__ for event_type in bindings.event_types} == {
        "Tick",
        "AsyncTurn",
        "Received",
    }


def test_load_bindings_rejects_next_breaking_version(monkeypatch: pytest.MonkeyPatch) -> None:
    import arena_hero_agent.adapters.sdk.bindings as bindings_module

    monkeypatch.setattr(bindings_module.metadata, "version", lambda _: "0.3.0")

    with pytest.raises(SdkContractViolationError, match=r">=0\.2\.9,<0\.3"):
        bindings_module.load_sdk_bindings()


@pytest.mark.asyncio
async def test_event_stream_preserves_sdk_owned_events_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden in adapter tests")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    plan = CommandPlan(tick=1)

    async def submitter(plan: CommandPlan, key: str | None) -> Accepted:
        return _accepted(tick=plan.tick)

    state = PlayerState(
        status=PlayerStatus.ACTIVE,
        resources=0,
        population=0,
        champion_beacon=ChampionBeacon(position=(0, 0)),
        objects=(),
        events=(),
    )
    events = (
        Tick(tick=1),
        AsyncTurn(tick=1, state=state, submitter=submitter),
        Received(
            tick=1,
            source=CommandSource.AGENT,
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
            plan=plan,
        ),
    )
    adapter = ArenaHeroSdkGameClient(FakeSdkClient(event_items=events))

    assert await _collect(adapter) == list(events)


@pytest.mark.asyncio
async def test_event_transport_failure_is_retryable_and_keeps_cause() -> None:
    cause = TransportError("stream offline")
    adapter = ArenaHeroSdkGameClient(FakeSdkClient(event_error=cause))

    with pytest.raises(SdkRetryableError) as raised:
        await _collect(adapter)

    assert raised.value.__cause__ is cause


@pytest.mark.asyncio
async def test_event_protocol_failure_is_contract_violation() -> None:
    adapter = ArenaHeroSdkGameClient(
        FakeSdkClient(event_error=ProtocolError("invalid event envelope"))
    )

    with pytest.raises(SdkContractViolationError):
        await _collect(adapter)


@pytest.mark.asyncio
async def test_event_cancellation_is_not_wrapped() -> None:
    adapter = ArenaHeroSdkGameClient(FakeSdkClient(event_error=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await _collect(adapter)


@pytest.mark.asyncio
async def test_unknown_event_shape_fails_loudly() -> None:
    adapter = ArenaHeroSdkGameClient(FakeSdkClient(event_items=(object(),)))

    with pytest.raises(SdkContractViolationError, match="unexpected SDK event shape"):
        await _collect(adapter)


@pytest.mark.asyncio
async def test_submit_forwards_decision_id_and_returns_validated_ack() -> None:
    fake = FakeSdkClient()
    adapter = ArenaHeroSdkGameClient(fake)
    port: GameClient = adapter
    assert isinstance(port, GameClient)
    plan = CommandPlan(tick=1)
    decision_id = DecisionId.from_deterministic_input({"tick": 1, "state": "stable"})

    result = await adapter.submit(plan, decision_id=decision_id)

    assert result == _accepted()
    assert fake.submissions == [(plan, decision_id.value)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 503])
async def test_retryable_api_statuses_are_classified(status: int) -> None:
    cause = APIError(status_code=status, error="temporary")
    adapter = ArenaHeroSdkGameClient(FakeSdkClient(submit_error=cause))

    with pytest.raises(SdkRetryableError) as raised:
        await adapter.submit(CommandPlan(tick=1), decision_id=DecisionId("decision:retry"))

    assert raised.value.__cause__ is cause


@pytest.mark.asyncio
async def test_terminal_api_and_authentication_failures_are_permanent() -> None:
    failures = (
        APIError(status_code=400, error="bad_request"),
        AuthenticationError("unauthorized"),
    )
    for failure in failures:
        adapter = ArenaHeroSdkGameClient(FakeSdkClient(submit_error=failure))
        with pytest.raises(SdkPermanentError) as raised:
            await adapter.submit(CommandPlan(tick=1), decision_id=DecisionId("decision:valid"))
        assert raised.value.__cause__ is failure


@pytest.mark.asyncio
async def test_submit_cancellation_is_not_wrapped() -> None:
    adapter = ArenaHeroSdkGameClient(FakeSdkClient(submit_error=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await adapter.submit(CommandPlan(tick=1), decision_id=DecisionId("decision:cancel"))


@pytest.mark.asyncio
async def test_bad_plan_ack_and_short_decision_id_fail_loudly() -> None:
    adapter = ArenaHeroSdkGameClient(FakeSdkClient(accepted=object()))

    with pytest.raises(SdkContractViolationError, match="CommandPlan shape"):
        await adapter.submit(cast(CommandPlan, object()), decision_id=DecisionId("decision:plan"))
    with pytest.raises(SdkContractViolationError, match="8 to 128"):
        await adapter.submit(CommandPlan(tick=1), decision_id=DecisionId("short"))
    with pytest.raises(SdkContractViolationError, match="Accepted shape"):
        await adapter.submit(CommandPlan(tick=1), decision_id=DecisionId("decision:shape"))


@pytest.mark.asyncio
async def test_ack_tick_and_source_must_match_submission_contract() -> None:
    for ack in (
        _accepted(tick=2),
        _accepted(source=CommandSource.MANUAL),
    ):
        adapter = ArenaHeroSdkGameClient(FakeSdkClient(accepted=ack))
        with pytest.raises(SdkContractViolationError):
            await adapter.submit(CommandPlan(tick=1), decision_id=DecisionId("decision:ack"))


@pytest.mark.asyncio
async def test_close_is_idempotent_and_closed_adapter_rejects_work() -> None:
    fake = FakeSdkClient()
    adapter = ArenaHeroSdkGameClient(fake)

    await adapter.close()
    await adapter.close()

    assert fake.close_calls == 1
    with pytest.raises(SdkPermanentError, match="client is closed"):
        await adapter.submit(CommandPlan(tick=1), decision_id=DecisionId("decision:closed"))
    with pytest.raises(SdkPermanentError, match="client is closed"):
        await _collect(adapter)


@pytest.mark.asyncio
async def test_bad_client_shape_is_rejected_before_use() -> None:
    with pytest.raises(SdkContractViolationError, match="close"):
        ArenaHeroSdkGameClient(cast(FakeSdkClient, object()))


@pytest.mark.parametrize(
    ("domain", "sdk"),
    [
        (Direction.NORTH, SdkDirection.UP),
        (Direction.EAST, SdkDirection.RIGHT),
        (Direction.SOUTH, SdkDirection.DOWN),
        (Direction.WEST, SdkDirection.LEFT),
    ],
)
def test_direction_mapping_is_explicit_and_bidirectional(
    domain: Direction, sdk: SdkDirection
) -> None:
    assert to_sdk_direction(domain) is sdk
    assert from_sdk_direction(sdk) is domain


def test_unknown_direction_value_is_rejected() -> None:
    with pytest.raises(SdkContractViolationError, match="expected an SDK Direction"):
        from_sdk_direction("UP")


@pytest.mark.asyncio
async def test_composition_factory_passes_explicit_options_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden in adapter tests")

    monkeypatch.setattr(socket.socket, "connect", deny_network)

    class RecordingClient(FakeSdkClient):
        options: dict[str, object] = {}

        def __init__(self, **options: object) -> None:
            super().__init__()
            type(self).options = options

    bindings = replace(load_sdk_bindings(), async_client_type=RecordingClient)
    adapter = create_sdk_game_client(
        api_key="test-only-key",
        base_url="https://example.invalid",
        websocket_url="wss://example.invalid/events",
        request_retries=0,
        bindings=bindings,
    )

    assert RecordingClient.options["api_key"] == "test-only-key"
    assert RecordingClient.options["base_url"] == "https://example.invalid"
    assert RecordingClient.options["websocket_url"] == "wss://example.invalid/events"
    assert RecordingClient.options["request_retries"] == 0
    await adapter.close()
