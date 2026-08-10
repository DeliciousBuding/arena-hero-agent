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
from .mapping import from_sdk_direction, to_sdk_direction

__all__ = [
    "ArenaHeroSdkGameClient",
    "SdkAdapterError",
    "SdkBindings",
    "SdkContractViolationError",
    "SdkFailureKind",
    "SdkPermanentError",
    "SdkRetryableError",
    "create_sdk_game_client",
    "from_sdk_direction",
    "load_sdk_bindings",
    "to_sdk_direction",
]
