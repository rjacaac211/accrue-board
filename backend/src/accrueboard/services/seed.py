"""Load a client, its staff and its coded history into the database.

History documents become posted tasks (with an audit trail and a journal entry) and their line
items become knowledge-store entries, so a fresh system starts where the client's books are:
duplicate checks and amount outliers see a year of prior documents, and coding has examples.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import ClientSpec, Split
from accrueboard.db.models import Account, Client, Document, User
from accrueboard.domain.capitalization import apply_capitalization
from accrueboard.domain.documents import ExtractedDocument
from accrueboard.domain.duplicates import normalize_document_number, normalize_vendor
from accrueboard.domain.journal import build_entry
from accrueboard.domain.lifecycle import Actor, TaskState
from accrueboard.retrieval.embeddings import Embedder
from accrueboard.retrieval.pg_store import PgKnowledgeStore
from accrueboard.retrieval.seed import history_entries
from accrueboard.services.ledger import post_entry
from accrueboard.services.tasks import create_task, record_event

DEFAULT_AUTO_POST_THRESHOLD = 0.9
"""Placeholder until calibration on the validation split sets the real value."""

STAFF = (
    ("u_alex", "Alex Rivera", "reviewer"),
    ("u_sam", "Sam Okafor", "reviewer"),
    ("u_jordan", "Jordan Lee", "senior"),
)


def fingerprint_columns(doc: ExtractedDocument) -> dict[str, Any]:
    """Document columns used for duplicate detection and amount history."""
    return {
        "doc_type": doc.doc_type.value,
        "extracted": doc.model_dump(mode="json"),
        "vendor_key": normalize_vendor(doc.vendor_name),
        "number_key": normalize_document_number(doc.document_number),
        "reference_key": normalize_document_number(doc.referenced_document_number),
        "total": doc.total,
        "issue_date": doc.issue_date,
    }


@dataclass(frozen=True)
class SeedSummary:
    client_id: str
    created: bool
    documents: int = 0
    knowledge_entries: int = 0
    posted_total: Decimal = Decimal("0.00")


def seed_client(
    session: Session,
    spec: ClientSpec,
    records: list[GroundTruth],
    embedder: Embedder,
    *,
    now: datetime,
) -> SeedSummary:
    """Create the client and import its history. Does nothing if the client already exists."""
    if session.get(Client, spec.id) is not None:
        return SeedSummary(client_id=spec.id, created=False)

    session.add(
        Client(
            id=spec.id,
            name=spec.name,
            business=spec.business,
            config={
                "materiality_cap": str(spec.materiality_cap),
                "capitalization_threshold": str(spec.capitalization_threshold),
                "auto_post_threshold": DEFAULT_AUTO_POST_THRESHOLD,
            },
        )
    )
    session.flush()  # the client row must exist before rows that reference it
    roles = {code: role.value for role, code in spec.roles.items()}
    session.add_all(
        Account(
            client_id=spec.id,
            code=a.code,
            name=a.name,
            type=a.type.value,
            description=a.description,
            capitalizable=a.capitalizable,
            role=roles.get(a.code),
        )
        for a in spec.accounts
    )
    existing_users = set(session.execute(select(User.id)).scalars())
    session.add_all(
        User(id=uid, name=name, role=role) for uid, name, role in STAFF if uid not in existing_users
    )
    session.flush()

    chart = spec.chart
    history = [r for r in records if r.split is Split.HISTORY]
    posted = Decimal("0.00")
    for record in history:
        doc = record.document
        document = Document(
            id=record.doc_id,
            client_id=spec.id,
            filename=f"{record.doc_id}.pdf",
            media_type=None,
            sha256=None,
            content=None,
            received_at=record.received_at,
            **fingerprint_columns(doc),
        )
        session.add(document)
        session.flush()
        task = create_task(
            session,
            document,
            now=record.received_at,
            actor="history-import",
            task_id=f"task_{record.doc_id}",
            state=TaskState.POSTED,
        )
        accounts, _ = apply_capitalization(
            doc, record.line_accounts, chart, threshold=spec.capitalization_threshold
        )
        task.line_accounts = list(accounts)
        entry = post_entry(
            session,
            build_entry(doc, accounts, chart),
            client_id=spec.id,
            task_id=task.id,
            now=record.received_at,
            entry_id=f"je_{record.doc_id}",
        )
        posted += doc.total or Decimal("0.00")
        record_event(
            session,
            task,
            now=record.received_at,
            actor="history-import",
            actor_kind=Actor.MACHINE,
            action="imported_posted",
            details={"journal_entry": entry.id},
        )

    store = PgKnowledgeStore(session, embedder, now=lambda: now)
    entries = history_entries(history)
    store.add(entries)
    session.flush()
    return SeedSummary(
        client_id=spec.id,
        created=True,
        documents=len(history),
        knowledge_entries=len(entries),
        posted_total=posted,
    )
