"""Failure semantics exposed by the Arena Hero SDK adapter."""

from __future__ import annotations

from enum import StrEnum


class SdkFailureKind(StrEnum):
    """Stable categories for failures crossing the SDK adapter boundary."""

    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    CANCELLED = "cancelled"
    CONTRACT_VIOLATION = "contract-violation"


class SdkAdapterError(Exception):
    """Base error for a failed SDK adapter operation."""

    kind: SdkFailureKind

    def __init__(self, operation: str, message: str) -> None:
        self.operation = operation
        super().__init__(f"{operation}: {message}")


class SdkRetryableError(SdkAdapterError):
    """A transient SDK failure that a higher application layer may retry."""

    kind = SdkFailureKind.RETRYABLE


class SdkPermanentError(SdkAdapterError):
    """A terminal SDK failure that must not be retried without changed input/config."""

    kind = SdkFailureKind.PERMANENT


class SdkContractViolationError(SdkAdapterError):
    """The SDK or injected client did not satisfy the pinned public contract."""

    kind = SdkFailureKind.CONTRACT_VIOLATION
