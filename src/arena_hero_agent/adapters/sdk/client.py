"""Concrete ``GameClient`` adapter for the public Arena Hero Python SDK."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol

from arena_hero_agent.domain import DecisionId

from .bindings import SdkBindings, load_sdk_bindings
from .errors import (
    SdkAdapterError,
    SdkContractViolationError,
    SdkPermanentError,
    SdkRetryableError,
)

if TYPE_CHECKING:
    from arena_hero import Accepted, AsyncGameEvent, CommandPlan


class _AsyncSdkClient(Protocol):
    def events(self) -> AsyncIterator[AsyncGameEvent]: ...

    async def submit(
        self,
        plan: CommandPlan,
        *,
        idempotency_key: str | None = None,
    ) -> Accepted: ...

    async def close(self) -> None: ...


def _translate_sdk_exception(
    exc: Exception,
    *,
    operation: str,
    bindings: SdkBindings,
) -> SdkAdapterError:
    if isinstance(exc, bindings.protocol_error_type):
        return SdkContractViolationError(operation, str(exc))
    if isinstance(exc, bindings.transport_error_type):
        return SdkRetryableError(operation, str(exc))
    if isinstance(exc, bindings.api_error_type):
        status = getattr(exc, "status_code", None)
        if status in {408, 409, 425, 429} or isinstance(status, int) and status >= 500:
            return SdkRetryableError(operation, str(exc))
        return SdkPermanentError(operation, str(exc))
    terminal_types = (
        bindings.authentication_error_type,
        bindings.configuration_error_type,
        bindings.invalid_action_error_type,
        bindings.policy_violation_error_type,
        bindings.turn_closed_error_type,
    )
    if isinstance(exc, terminal_types):
        return SdkPermanentError(operation, str(exc))
    if isinstance(exc, bindings.arena_error_type):
        return SdkPermanentError(operation, str(exc))
    return SdkContractViolationError(
        operation,
        f"unexpected exception from SDK boundary: {type(exc).__name__}",
    )


def _validate_client_shape(client: object) -> None:
    missing = [
        name for name in ("events", "submit", "close") if not callable(getattr(client, name, None))
    ]
    if missing:
        raise SdkContractViolationError(
            "compose", f"SDK client is missing callable member(s): {', '.join(missing)}"
        )


def _validate_idempotency_key(decision_id: DecisionId) -> str:
    key = decision_id.value
    try:
        encoded = key.encode("ascii", errors="strict")
    except UnicodeEncodeError as exc:
        raise SdkContractViolationError(
            "submit", "DecisionId cannot be represented as an SDK idempotency key"
        ) from exc
    if not 8 <= len(encoded) <= 128 or any(byte < 0x21 or byte > 0x7E for byte in encoded):
        raise SdkContractViolationError(
            "submit",
            "DecisionId must contain 8 to 128 visible ASCII bytes for SDK submission",
        )
    return key


class ArenaHeroSdkGameClient:
    """Adapt an SDK async client to the application-owned ``GameClient`` port.

    The wrapped client is injected, so construction and tests do not perform network
    access. Use :func:`create_sdk_game_client` only in a composition root.
    """

    def __init__(
        self,
        client: _AsyncSdkClient,
        *,
        bindings: SdkBindings | None = None,
    ) -> None:
        _validate_client_shape(client)
        self._client = client
        self._bindings = bindings or load_sdk_bindings()
        self._close_lock = asyncio.Lock()
        self._closed = False

    def events(self) -> AsyncIterator[AsyncGameEvent]:
        """Stream validated SDK events while preserving cancellation."""

        return self._events()

    async def _events(self) -> AsyncIterator[AsyncGameEvent]:
        if self._closed:
            raise SdkPermanentError("events", "client is closed")
        try:
            async for event in self._client.events():
                if not isinstance(event, self._bindings.event_types):
                    raise SdkContractViolationError(
                        "events",
                        f"unexpected SDK event shape: {type(event).__name__}",
                    )
                yield event
        except asyncio.CancelledError:
            raise
        except SdkAdapterError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(
                exc, operation="events", bindings=self._bindings
            ) from exc

    async def submit(self, plan: CommandPlan, *, decision_id: DecisionId) -> Accepted:
        """Submit an SDK-owned plan using ``DecisionId`` as the idempotency key."""

        if self._closed:
            raise SdkPermanentError("submit", "client is closed")
        if not isinstance(plan, self._bindings.command_plan_type):
            raise SdkContractViolationError(
                "submit", f"unexpected SDK CommandPlan shape: {type(plan).__name__}"
            )
        key = _validate_idempotency_key(decision_id)
        try:
            accepted = await self._client.submit(plan, idempotency_key=key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(
                exc, operation="submit", bindings=self._bindings
            ) from exc
        self._validate_ack(accepted, expected_tick=plan.tick)
        return accepted

    def _validate_ack(self, accepted: object, *, expected_tick: int) -> None:
        if not isinstance(accepted, self._bindings.accepted_type):
            raise SdkContractViolationError(
                "submit", f"unexpected SDK Accepted shape: {type(accepted).__name__}"
            )
        expected_source = getattr(self._bindings.command_source_type, "AGENT", None)
        if getattr(accepted, "accepted", None) is not True:
            raise SdkContractViolationError("submit", "SDK acknowledgement was not accepted")
        if getattr(accepted, "tick", None) != expected_tick:
            raise SdkContractViolationError(
                "submit", "SDK acknowledgement tick does not match the submitted plan"
            )
        if expected_source is None or getattr(accepted, "source", None) is not expected_source:
            raise SdkContractViolationError("submit", "SDK acknowledgement source is not AGENT")

    async def close(self) -> None:
        """Close the underlying client once; cancellation remains cancellation."""

        async with self._close_lock:
            if self._closed:
                return
            try:
                await self._client.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _translate_sdk_exception(
                    exc, operation="close", bindings=self._bindings
                ) from exc
            self._closed = True


def create_sdk_game_client(
    *,
    api_key: str,
    base_url: str | None = None,
    websocket_url: str | None = None,
    request_timeout: float = 5.0,
    request_retries: int = 2,
    reconnect_min_delay: float = 0.25,
    reconnect_max_delay: float = 5.0,
    max_message_size: int = 2 * 1024 * 1024,
    bindings: SdkBindings | None = None,
) -> ArenaHeroSdkGameClient:
    """Build the adapter from explicit settings without reading environment variables."""

    sdk = bindings or load_sdk_bindings()
    options: dict[str, Any] = {
        "api_key": api_key,
        "request_timeout": request_timeout,
        "request_retries": request_retries,
        "reconnect_min_delay": reconnect_min_delay,
        "reconnect_max_delay": reconnect_max_delay,
        "max_message_size": max_message_size,
    }
    if base_url is not None:
        options["base_url"] = base_url
    if websocket_url is not None:
        options["websocket_url"] = websocket_url
    client = sdk.async_client_type(**options)
    return ArenaHeroSdkGameClient(client, bindings=sdk)
