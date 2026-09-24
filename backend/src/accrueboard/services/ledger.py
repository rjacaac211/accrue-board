"""Ledger persistence: store balanced entries, reverse them, and report balances.

Balance is checked twice: by the domain model when the entry is built, and by the
database (a deferred constraint trigger) when the transaction commits. Posted rows can never be
updated or deleted (another trigger); a correction is a reversing entry.
"""

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from accrueboard.db.models import JournalEntry as EntryRow
from accrueboard.db.models import JournalLine as LineRow
from accrueboard.domain.journal import JournalEntry, JournalLine, reverse_entry
from accrueboard.services.tasks import new_id


def post_entry(
    session: Session,
    entry: JournalEntry,
    *,
    client_id: str,
    task_id: str | None,
    now: datetime,
    entry_id: str | None = None,
) -> EntryRow:
    if not entry.is_balanced:  # defensive: the domain model already refuses this
        raise ValueError("refusing to post an unbalanced entry")
    row = EntryRow(
        id=entry_id or new_id("je"),
        client_id=client_id,
        task_id=task_id,
        entry_date=entry.entry_date,
        memo=entry.memo,
        reverses=entry.reverses,
        posted_at=now,
        lines=[
            LineRow(
                line_no=i,
                account_code=line.account_code,
                debit=line.debit,
                credit=line.credit,
                memo=line.memo,
            )
            for i, line in enumerate(entry.lines, start=1)
        ],
    )
    session.add(row)
    session.flush()
    return row


def to_domain(row: EntryRow) -> JournalEntry:
    return JournalEntry(
        entry_date=row.entry_date,
        memo=row.memo,
        reverses=row.reverses,
        lines=tuple(
            JournalLine(
                account_code=line.account_code, debit=line.debit, credit=line.credit, memo=line.memo
            )
            for line in row.lines
        ),
    )


def reverse(
    session: Session, original: EntryRow, *, now: datetime, entry_date: date, task_id: str | None
) -> EntryRow:
    reversal = reverse_entry(to_domain(original), entry_date=entry_date, original_id=original.id)
    return post_entry(session, reversal, client_id=original.client_id, task_id=task_id, now=now)


def entries_for_task(session: Session, task_id: str) -> list[EntryRow]:
    return list(
        session.execute(
            select(EntryRow).where(EntryRow.task_id == task_id).order_by(EntryRow.posted_at)
        ).scalars()
    )


def trial_balance(session: Session, client_id: str) -> dict[str, Decimal]:
    """Net debit balance per account (credits negative). Sums to zero for a sound ledger."""
    rows = session.execute(
        select(LineRow.account_code, LineRow.debit, LineRow.credit)
        .join(EntryRow, EntryRow.id == LineRow.entry_id)
        .where(EntryRow.client_id == client_id)
    )
    balances: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for code, debit, credit in rows:
        balances[code] += debit - credit
    return dict(sorted(balances.items()))
