"""Compute table diffs and render change sets for human reviewers."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from ticketideai.schemas.operations import (
    AnyOperation,
    ChangeSet,
    CreateReturn,
    IssueRefund,
    Restock,
    UpdateOrderStatus,
)

Snapshot = dict[str, dict[str, dict[str, Any]]]
IGNORED_TABLES = {"approval_queue"}


def snapshot(path: Path) -> Snapshot:
    """Read every store table into `{table: {primary_key: row}}`."""
    tables: Snapshot = {}
    with closing(sqlite3.connect(path)) as connection:
        names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (table,) in names:
            if table not in IGNORED_TABLES:
                tables[table] = _read_table(connection, table)
    return tables


def table_diff(before: Snapshot, after: Snapshot) -> dict[str, Any]:
    """Return the rows added, removed, or changed between two snapshots."""
    changed: dict[str, Any] = {}
    for table in sorted(set(before) | set(after)):
        entry = _diff_rows(before.get(table, {}), after.get(table, {}))
        if entry:
            changed[table] = entry
    return {"tables": changed}


def render_change_set(change_set: ChangeSet) -> str:
    """Describe each proposed operation in one plain-English line."""
    if change_set.is_empty:
        return "No changes proposed."
    lines = [_describe(operation) for operation in change_set.operations]
    return "\n".join(f"{index}. {line}" for index, line in enumerate(lines, start=1))


def _read_table(connection: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    columns = [row[1] for row in info]
    keys = [row[1] for row in info if row[5]] or columns
    rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
    records = [dict(zip(columns, row, strict=True)) for row in rows]
    return {"|".join(str(record[key]) for key in keys): record for record in records}


def _diff_rows(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    added = [new[key] for key in sorted(new.keys() - old.keys())]
    removed = [old[key] for key in sorted(old.keys() - new.keys())]
    changed = [
        {"key": key, "before": old[key], "after": new[key]}
        for key in sorted(old.keys() & new.keys())
        if old[key] != new[key]
    ]
    for name, rows in (("added", added), ("removed", removed), ("changed", changed)):
        if rows:
            entry[name] = rows
    return entry


def _describe(operation: AnyOperation) -> str:
    if isinstance(operation, CreateReturn):
        return (
            f"Return {operation.quantity} x {operation.sku} from order {operation.order_id} "
            f"({operation.facts.already_returned} of "
            f"{operation.facts.purchased_quantity} already returned)"
        )
    if isinstance(operation, IssueRefund):
        return (
            f"Refund {_money(operation.amount_cents)} on order {operation.order_id} "
            f"({_money(operation.facts.refunded_cents)} of "
            f"{_money(operation.facts.paid_cents)} paid already refunded)"
        )
    if isinstance(operation, Restock):
        after = operation.facts.stock_before + operation.quantity
        return (
            f"Restock {operation.quantity} x {operation.sku} "
            f"(stock {operation.facts.stock_before} -> {after})"
        )
    if isinstance(operation, UpdateOrderStatus):
        return (
            f"Change order {operation.order_id} status "
            f"from {operation.facts.status} to {operation.new_status}"
        )
    return f"Message {operation.customer_email}: {operation.body}"


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"
