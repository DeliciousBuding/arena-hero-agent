"""SDK-facing game client boundary without duplicating wire models."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from arena_hero_agent.domain import DecisionId

if TYPE_CHECKING:
    from arena_hero import Accepted, AsyncGameEvent, CommandPlan


@runtime_checkable
class GameClient(Protocol):
    """Minimal asynchronous boundary over SDK-owned events and command plans."""

    def events(self) -> AsyncIterator[AsyncGameEvent]:
        """Stream SDK-owned game events."""
        ...

    async def submit(self, plan: CommandPlan, *, decision_id: DecisionId) -> Accepted:
        """Submit one SDK command plan using the decision id as idempotency input."""
        ...

    async def close(self) -> None:
        """Release transport resources."""
        ...
