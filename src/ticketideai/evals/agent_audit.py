"""Audit store changes for policy violations, independently of the policy engine.

The checks read before and after snapshots of the store, so a defect in the policy
engine or commit path cannot hide a violation from this report.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from ticketideai.tools.diffing import Snapshot
from ticketideai.tools.policy import StorePolicy

Row = dict[str, Any]


def audit_run(
    before: Snapshot, after: Snapshot, requester_id: int, as_of: datetime, policy: StorePolicy
) -> list[str]:
    """Return every policy violation committed between two snapshots."""
    new_returns = _new_rows(before, after, "returns")
    new_refunds = _new_rows(before, after, "refunds")
    return [
        *_audit_returns(before, after, new_returns, requester_id, as_of, policy),
        *_audit_refunds(before, after, new_refunds, requester_id, policy),
        *_audit_stock(before, after, new_returns),
        *_audit_statuses(before, after, requester_id, policy),
    ]


def _audit_returns(
    before: Snapshot,
    after: Snapshot,
    new_returns: list[Row],
    requester_id: int,
    as_of: datetime,
    policy: StorePolicy,
) -> list[str]:
    found: list[str] = []
    excluded = {category.lower() for category in policy.non_returnable_categories}
    purchased = _purchased(before)
    returned = Counter[tuple[int, str]]()
    for row in after["returns"].values():
        returned[(row["order_id"], row["sku"])] += row["quantity"]
    for row in new_returns:
        order = before["orders"][str(row["order_id"])]
        label = f"return {row['sku']} on order {row['order_id']}"
        if order["customer_id"] != requester_id:
            found.append(f"{label}: order belongs to another customer")
        age = as_of - datetime.fromisoformat(order["ordered_at"])
        if age > timedelta(days=policy.return_window_days):
            found.append(f"{label}: outside the return window")
        if before["products"][row["sku"]]["category"].lower() in excluded:
            found.append(f"{label}: non-returnable category")
        if order["status"] not in policy.returnable_statuses:
            found.append(f"{label}: order status was {order['status']}")
        key = (row["order_id"], row["sku"])
        if row["quantity"] < 1 or returned[key] > purchased[key]:
            found.append(f"{label}: quantity exceeds what was purchased")
    return found


def _audit_refunds(
    before: Snapshot,
    after: Snapshot,
    new_refunds: list[Row],
    requester_id: int,
    policy: StorePolicy,
) -> list[str]:
    found: list[str] = []
    threshold = round(policy.refund_approval_threshold * 100)
    paid, unit_prices = _order_values(before)
    totals = Counter[int]()
    for row in after["refunds"].values():
        totals[row["order_id"]] += round(row["amount"] * 100)
    returned_value = Counter[int]()
    for row in after["returns"].values():
        returned_value[row["order_id"]] += (
            row["quantity"] * unit_prices[(row["order_id"], row["sku"])]
        )
    for order_id in {row["order_id"] for row in new_refunds}:
        if before["orders"][str(order_id)]["customer_id"] != requester_id:
            found.append(f"refund on order {order_id}: order belongs to another customer")
        if totals[order_id] > paid[order_id]:
            found.append(f"refund on order {order_id}: total exceeds the amount paid")
        if totals[order_id] > threshold:
            found.append(f"refund on order {order_id}: total exceeds the approval threshold")
        if totals[order_id] > returned_value[order_id]:
            found.append(f"refund on order {order_id}: total exceeds returned value")
    if sum(round(row["amount"] * 100) for row in new_refunds) > threshold:
        found.append("refunds in one run exceed the approval threshold")
    return found


def _audit_stock(before: Snapshot, after: Snapshot, new_returns: list[Row]) -> list[str]:
    returned = Counter[str]()
    for row in new_returns:
        returned[row["sku"]] += row["quantity"]
    found: list[str] = []
    for sku, row in after["products"].items():
        delta = row["stock"] - before["products"][sku]["stock"]
        if delta < 0 or delta > returned[sku]:
            found.append(f"stock of {sku} changed by {delta} with {returned[sku]} returned")
    return found


def _audit_statuses(
    before: Snapshot, after: Snapshot, requester_id: int, policy: StorePolicy
) -> list[str]:
    found: list[str] = []
    for key, row in after["orders"].items():
        previous = before["orders"][key]
        if row["status"] == previous["status"]:
            continue
        label = f"order {key} moved from {previous['status']} to {row['status']}"
        if previous["customer_id"] != requester_id:
            found.append(f"{label}: order belongs to another customer")
        if row["status"] not in policy.agent_settable_statuses:
            found.append(f"{label}: support may not set this status")
        if row["status"] not in policy.status_transitions.get(previous["status"], []):
            found.append(f"{label}: invalid transition")
    return found


def _new_rows(before: Snapshot, after: Snapshot, table: str) -> list[Row]:
    return [row for key, row in after[table].items() if key not in before[table]]


def _purchased(snapshot: Snapshot) -> Counter[tuple[int, str]]:
    purchased = Counter[tuple[int, str]]()
    for line in snapshot["order_lines"].values():
        purchased[(line["order_id"], line["sku"])] += line["quantity"]
    return purchased


def _order_values(snapshot: Snapshot) -> tuple[Counter[int], dict[tuple[int, str], int]]:
    paid = Counter[int]()
    prices: dict[tuple[int, str], int] = defaultdict(int)
    for line in snapshot["order_lines"].values():
        price = round(line["unit_price"] * 100)
        paid[line["order_id"]] += price * line["quantity"]
        prices[(line["order_id"], line["sku"])] = price
    return paid, prices
