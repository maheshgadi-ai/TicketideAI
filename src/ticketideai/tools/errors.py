"""Errors raised by store tools."""

from __future__ import annotations


class StoreError(ValueError):
    """Raised when a store operation is malformed or cannot find a record."""


class PreconditionFailed(StoreError):
    """Raised when the store no longer matches what a change set relied on."""


class SandboxConfigurationError(RuntimeError):
    """Raised when the sandbox cannot run with its safety guarantees intact."""
