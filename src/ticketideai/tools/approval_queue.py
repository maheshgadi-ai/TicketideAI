"""Persistent queue of change sets waiting for a human decision."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ticketideai.schemas.operations import ChangeSet
from ticketideai.schemas.store import ApprovalItem
from ticketideai.tools.database import ApprovalRow, Base, read_engine, write_engine
from ticketideai.tools.effects import apply_change_set, record_customer_notice
from ticketideai.tools.errors import StoreError
from ticketideai.tools.notifications import approved_notice, rejected_notice, stale_notice


class ApprovalError(StoreError):
    """Raised when a queue item cannot be resolved as requested."""


class ApprovalQueue:
    """Store escalated change sets and apply exactly what a reviewer approved."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.engine = read_engine(database_url)

    def initialize(self) -> None:
        """Create the queue table when it does not exist."""
        Base.metadata.create_all(self.engine)

    def enqueue(
        self,
        *,
        customer_email: str,
        message: str,
        plan_source: str,
        reasons: list[str],
        change_set: ChangeSet,
        diff: dict[str, Any],
        diff_text: str,
    ) -> ApprovalItem:
        """Add one escalated change set with its human-readable diff."""
        with Session(self.engine) as session, session.begin():
            row = ApprovalRow(
                customer_email=customer_email,
                message=message,
                plan_source=plan_source,
                reasons_json=json.dumps(reasons),
                change_set_json=change_set.model_dump_json(),
                change_set_digest=change_set.digest(),
                diff_json=json.dumps(diff),
                diff_text=diff_text,
                status="pending",
            )
            session.add(row)
            session.flush()
            return self._item(row)

    def list_pending(self) -> list[ApprovalItem]:
        """Return unresolved items, oldest first."""
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ApprovalRow).where(ApprovalRow.status == "pending").order_by(ApprovalRow.id)
            ).all()
            return [self._item(row) for row in rows]

    def get(self, item_id: int) -> ApprovalItem:
        """Return one queue item."""
        with Session(self.engine) as session:
            return self._item(self._row(session, item_id))

    def commit_approved(self, item_id: int, reviewer: str) -> ApprovalItem:
        """Apply the stored change set and mark it approved in one transaction."""
        engine = write_engine(self.database_url)
        try:
            with Session(engine) as session, session.begin():
                row = self._pending_row(session, item_id)
                item = self._item(row)
                if item.change_set.digest() != item.change_set_digest:
                    raise ApprovalError(f"approval item {item_id} failed its integrity check")
                apply_change_set(session, item.change_set)
                notice = approved_notice(item.change_set)
                record_customer_notice(session, item.customer_email, notice)
                row.status, row.reviewer, row.customer_notice = "approved", reviewer, notice
                session.flush()
                return self._item(row)
        finally:
            engine.dispose()

    def reject(self, item_id: int, reviewer: str) -> ApprovalItem:
        """Close a pending item without applying it."""
        return self._resolve(item_id, "rejected", reviewer, rejected_notice())

    def mark_stale(self, item_id: int, reviewer: str) -> ApprovalItem:
        """Close a pending item whose preconditions no longer hold."""
        return self._resolve(item_id, "stale", reviewer, stale_notice())

    def _resolve(self, item_id: int, status: str, reviewer: str, notice: str) -> ApprovalItem:
        engine = write_engine(self.database_url)
        try:
            with Session(engine) as session, session.begin():
                row = self._pending_row(session, item_id)
                record_customer_notice(session, row.customer_email, notice)
                row.status, row.reviewer, row.customer_notice = status, reviewer, notice
                session.flush()
                return self._item(row)
        finally:
            engine.dispose()

    def _pending_row(self, session: Session, item_id: int) -> ApprovalRow:
        row = self._row(session, item_id)
        if row.status != "pending":
            raise ApprovalError(f"approval item {item_id} is already {row.status}")
        return row

    @staticmethod
    def _row(session: Session, item_id: int) -> ApprovalRow:
        row = session.get(ApprovalRow, item_id)
        if row is None:
            raise ApprovalError(f"approval item not found: {item_id}")
        return row

    @staticmethod
    def _item(row: ApprovalRow) -> ApprovalItem:
        return ApprovalItem(
            id=row.id,
            customer_email=row.customer_email,
            message=row.message,
            plan_source=row.plan_source,
            reasons=json.loads(row.reasons_json),
            change_set=ChangeSet.model_validate_json(row.change_set_json),
            change_set_digest=row.change_set_digest,
            diff=json.loads(row.diff_json),
            diff_text=row.diff_text,
            status=row.status,
            reviewer=row.reviewer,
            customer_notice=row.customer_notice,
        )
