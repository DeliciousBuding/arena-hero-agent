"""Public Arena Hero SDK adapter surface.

Importing this module does not import ``arena_hero``. The SDK is loaded only when
bindings are requested or a concrete adapter is composed.
"""

from .bindings import SdkBindings, load_sdk_bindings
from .client import ArenaHeroSdkGameClient, create_sdk_game_client
from .errors import (
    SdkAdapterError,
    SdkContractViolationError,
    SdkFailureKind,
    SdkPermanentError,
    SdkRetryableError,
)
from .live import LiveSubmitter, LiveTurnSource
from .mapping import from_sdk_direction, to_sdk_direction
from .plans import build_command_plan, command_plan_payload
from .turns import adapt_async_turn

__all__ = [
    "ArenaHeroSdkGameClient",
    "SdkAdapterError",
    "SdkBindings",
    "SdkContractViolationError",
    "SdkFailureKind",
    "SdkPermanentError",
    "SdkRetryableError",
    "adapt_async_turn",
    "build_command_plan",
    "command_plan_payload",
    "create_sdk_game_client",
    "from_sdk_direction",
    "LiveSubmitter",
    "LiveTurnSource",
    "load_sdk_bindings",
    "to_sdk_direction",
]
