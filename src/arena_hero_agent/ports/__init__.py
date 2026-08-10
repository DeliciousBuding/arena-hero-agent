"""Application-owned structural interfaces; concrete implementations live in adapters."""

from .clock import Clock
from .control import CommandBus, SnapshotReader
from .game import GameClient
from .leases import (
    DecisionLease,
    DecisionLeaseHandle,
    LeaseHandle,
    MigrationLease,
    MigrationLeaseHandle,
    WriterLease,
    WriterLeaseHandle,
)
from .observability import HealthReporter, TelemetrySink
from .persistence import DecisionRecorder, EventJournal, TenantStateStore

__all__ = [
    "Clock",
    "CommandBus",
    "DecisionLease",
    "DecisionLeaseHandle",
    "DecisionRecorder",
    "EventJournal",
    "GameClient",
    "HealthReporter",
    "LeaseHandle",
    "MigrationLease",
    "MigrationLeaseHandle",
    "SnapshotReader",
    "TelemetrySink",
    "TenantStateStore",
    "WriterLease",
    "WriterLeaseHandle",
]
