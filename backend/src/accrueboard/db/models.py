"""Database schema (SQLAlchemy 2 ORM).

Postgres is the single source of truth for task state (see ADR 0001). Integrity rules that
must hold regardless of application bugs are enforced in the database itself by the
migration: the audit log is append-only, and every journal entry balances at commit.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIMENSIONS = 384
Money = Numeric(14, 2)


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    business: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    """Per-client settings: materiality cap, capitalization threshold, auto-post threshold."""


class Account(Base):
    __tablename__ = "accounts"

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(Text, default="")
    capitalizable: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[str | None] = mapped_column(String(32))


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16))
    """reviewer or senior."""


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    filename: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(32))
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    content: Mapped[bytes | None] = mapped_column(LargeBinary)
    """File bytes (None for seeded history, which exists only as structured data)."""
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    doc_type: Mapped[str | None] = mapped_column(String(16))
    extracted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """The ExtractedDocument (as confirmed by a reviewer, once reviewed)."""
    # Fingerprint columns for duplicate detection and amount history.
    vendor_key: Mapped[str | None] = mapped_column(Text, index=True)
    number_key: Mapped[str | None] = mapped_column(Text)
    reference_key: Mapped[str | None] = mapped_column(Text)
    total: Mapped[Decimal | None] = mapped_column(Money)
    issue_date: Mapped[date | None] = mapped_column(Date)

    task: Mapped["Task"] = relationship(back_populates="document")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), unique=True)
    state: Mapped[str] = mapped_column(String(16), index=True)
    state_entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    coding: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    routing: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    line_accounts: Mapped[list[str] | None] = mapped_column(JSONB)
    """Accounts per line after the capitalization rule (what gets posted)."""

    document: Mapped[Document] = relationship(back_populates="task")


class AuditEvent(Base):
    """Append-only (enforced by a trigger) and hash-chained per task."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(64))
    actor_kind: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))
    from_state: Mapped[str | None] = mapped_column(String(16))
    to_state: Mapped[str | None] = mapped_column(String(16))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))

    __table_args__ = (UniqueConstraint("task_id", "seq"),)


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    entry_date: Mapped[date] = mapped_column(Date)
    memo: Mapped[str] = mapped_column(Text)
    reverses: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"))
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry", order_by="JournalLine.line_no", cascade="all, delete-orphan"
    )


class JournalLine(Base):
    __tablename__ = "journal_lines"

    entry_id: Mapped[str] = mapped_column(ForeignKey("journal_entries.id"), primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_code: Mapped[str] = mapped_column(String(16))
    debit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    credit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    memo: Mapped[str] = mapped_column(Text, default="")

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")


class KnowledgeRow(Base):
    __tablename__ = "knowledge_entries"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    vendor_key: Mapped[str] = mapped_column(Text)
    vendor_name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Money)
    account: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16))
    document_ref: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    """Insertion order, used for stable ordering."""
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', vendor_name || ' ' || description)", persisted=True),
    )

    __table_args__ = (
        Index("ix_knowledge_entries_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_knowledge_entries_vendor_trgm",
            "vendor_key",
            postgresql_using="gin",
            postgresql_ops={"vendor_key": "gin_trgm_ops"},
        ),
    )


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(64))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    replayed: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
