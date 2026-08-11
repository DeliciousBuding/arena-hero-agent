"""Shared errors for the Python Command Center data base (P5-3)."""

from __future__ import annotations


class CommandCenterError(Exception):
    """Configuration, contention, corruption, or IO failure in Command Center data access."""
