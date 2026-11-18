"""SQLAlchemy models and engines for the TicketideAI store database."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import NullPool

SQLITE_PREFIX = "sqlite:///"


class Base(DeclarativeBase):
    pass


class ProductRow(Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CustomerRow(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="placed")
    ordered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    lines: Mapped[list[OrderLineRow]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderLineRow(Base):
    __tablename__ = "order_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    sku: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    order: Mapped[OrderRow] = relationship(back_populates="lines")


class ReturnRow(Base):
    __tablename__ = "returns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    sku: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="approved")


class RefundRow(Base):
    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="issued")


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)


class RestockRow(Base):
    __tablename__ = "restocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_after: Mapped[int] = mapped_column(Integer, nullable=False)


class ApprovalRow(Base):
    __tablename__ = "approval_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_email: Mapped[str] = mapped_column(String(256), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    plan_source: Mapped[str] = mapped_column(Text, nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    change_set_json: Mapped[str] = mapped_column(Text, nullable=False)
    change_set_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    diff_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    diff_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    customer_notice: Mapped[str | None] = mapped_column(Text, nullable=True)


def sqlite_url(value: str | Path) -> str:
    """Return a SQLAlchemy URL for a SQLite path or an existing SQLite URL."""
    text = str(value)
    return text if text.startswith("sqlite:") else f"{SQLITE_PREFIX}{text}"


def sqlite_path(database_url: str) -> Path:
    """Return the file path behind a SQLite URL or plain path."""
    url = sqlite_url(database_url)
    if not url.startswith(SQLITE_PREFIX):
        raise ValueError(f"only file-backed SQLite databases are supported: {url}")
    return Path(url.removeprefix(SQLITE_PREFIX))


def read_engine(database_url: str) -> Engine:
    """Create an engine for ordinary reads and short writes."""
    return create_engine(sqlite_url(database_url), poolclass=NullPool)


def write_engine(database_url: str) -> Engine:
    """Create an engine whose transactions take SQLite's write lock before reading.

    Commit checks preconditions and then writes; holding the lock from the first read
    means no other writer can change the store between the check and the write.
    """
    engine = create_engine(sqlite_url(database_url), poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def _manual_transactions(connection: Any, _record: Any) -> None:
        connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_immediate(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine
