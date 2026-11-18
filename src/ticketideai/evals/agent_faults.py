"""Inject harness faults into one agent-eval run on its scratch database."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy.exc import OperationalError

from ticketideai.schemas.agent_evals import FaultKind
from ticketideai.schemas.operations import ChangeSet, Restock
from ticketideai.tools.diffing import Snapshot, snapshot
from ticketideai.tools.effects import commit_change_set
from ticketideai.tools.sandbox import SandboxExecutor, SandboxResult

INSTANT_TIMEOUT_SECONDS = 0.001
WAREHOUSE_ADJUSTMENT = 2
_ADJUST_STOCK = "UPDATE products SET stock = stock + ? WHERE sku = ?"
_TOUCH_ORDERS = (
    "UPDATE orders SET ordered_at = strftime('%Y-%m-%d %H:%M:%f', ordered_at, '+1 second') || '000'"
)


class PersistentContentionSandbox(SandboxExecutor):
    """Simulate a writer that changes every order after each proposed dry run."""

    def __init__(self, database: Path) -> None:
        super().__init__()
        self.database = database
        self.injections = 0

    def execute(self, source: str, database_url: str, customer_email: str) -> SandboxResult:
        result = super().execute(source, database_url, customer_email)
        if result.success and not result.change_set.is_empty:
            with closing(sqlite3.connect(self.database)) as connection:
                connection.execute(_TOUCH_ORDERS)
                connection.commit()
            self.injections += 1
        return result


class SingleStockChangeSandbox(SandboxExecutor):
    """Simulate one warehouse stock adjustment between the first dry run and its commit."""

    def __init__(self, database: Path) -> None:
        super().__init__()
        self.database = database
        self.injections = 0
        self.baseline: Snapshot | None = None

    def execute(self, source: str, database_url: str, customer_email: str) -> SandboxResult:
        result = super().execute(source, database_url, customer_email)
        restocks = [op for op in result.change_set.operations if isinstance(op, Restock)]
        if result.success and restocks and not self.injections:
            with closing(sqlite3.connect(self.database)) as connection:
                connection.execute(_ADJUST_STOCK, (WAREHOUSE_ADJUSTMENT, restocks[0].sku))
                connection.commit()
            self.injections = 1
            self.baseline = snapshot(self.database)
        return result


class FirstRunTimeoutSandbox(SandboxExecutor):
    """Run the first valid plan under a time limit it cannot meet."""

    def __init__(self) -> None:
        super().__init__()
        self.injections = 0

    def execute(self, source: str, database_url: str, customer_email: str) -> SandboxResult:
        if self.injections:
            return super().execute(source, database_url, customer_email)
        limits = replace(self.limits, wall_seconds=INSTANT_TIMEOUT_SECONDS)
        result = SandboxExecutor(limits).execute(source, database_url, customer_email)
        self.injections += result.timed_out
        return result


class FlakyCommitter:
    """Fail the first commit as a locked database would, before anything is written."""

    def __init__(self) -> None:
        self.injections = 0

    def __call__(self, database_url: str, change_set: ChangeSet) -> None:
        if not self.injections:
            self.injections += 1
            raise OperationalError(
                "BEGIN IMMEDIATE", None, sqlite3.OperationalError("database is locked")
            )
        commit_change_set(database_url, change_set)


@dataclass(frozen=True)
class FaultHooks:
    """Workflow collaborators for one run, and how to tell whether the fault fired."""

    sandbox: SandboxExecutor | None = None
    committer: Callable[[str, ChangeSet], None] = commit_change_set
    injector: (
        PersistentContentionSandbox
        | SingleStockChangeSandbox
        | FirstRunTimeoutSandbox
        | FlakyCommitter
        | None
    ) = None

    @property
    def fired(self) -> bool:
        """True when the fault was injected at least once."""
        return self.injector is not None and self.injector.injections > 0

    @property
    def baseline(self) -> Snapshot | None:
        """Return the store after an injected change, so it is not scored as the agent's."""
        return getattr(self.injector, "baseline", None)


def build_fault(kind: FaultKind | None, database: Path) -> FaultHooks:
    """Return the hooks for a fault, or plain defaults when there is none."""
    if kind == "persistent_contention":
        sandbox = PersistentContentionSandbox(database)
        return FaultHooks(sandbox=sandbox, injector=sandbox)
    if kind == "single_concurrent_change":
        adjusted = SingleStockChangeSandbox(database)
        return FaultHooks(sandbox=adjusted, injector=adjusted)
    if kind == "sandbox_limit":
        limited = FirstRunTimeoutSandbox()
        return FaultHooks(sandbox=limited, injector=limited)
    if kind == "transient_database_error":
        committer = FlakyCommitter()
        return FaultHooks(committer=committer, injector=committer)
    return FaultHooks()
