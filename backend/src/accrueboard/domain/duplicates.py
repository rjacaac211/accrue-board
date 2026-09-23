"""Duplicate detection.

Hard duplicates (always sent to review):
- EXACT_FILE: the same file bytes (SHA-256) seen before.
- SAME_NUMBER: same vendor and same normalized document number, within the same category
  (bills = invoices and receipts; credit notes are compared only with credit notes, so a credit
  note is never a duplicate of the invoice it refers to).
- DUPLICATE_CREDIT_NOTE: a second credit note from the same vendor against the same invoice.

Soft signal (lowers confidence, does not force review):
- NEAR_DUPLICATE: same vendor, same total, issue dates within a short window, and a different
  or missing document number. Typical of a re-keyed or re-sent bill, or an invoice and its
  payment receipt both submitted. Monthly recurring charges fall outside the window.
"""

import re
from collections.abc import Iterable
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from accrueboard.domain.documents import DocumentType, ExtractedDocument
from accrueboard.domain.money import Money

NEAR_DUPLICATE_WINDOW_DAYS = 10

_NUMBER_PREFIXES = ("INVOICE", "INV")
_LEGAL_SUFFIXES = frozenset(
    {"inc", "incorporated", "llc", "llp", "ltd", "limited", "co", "corp", "corporation",
     "company", "plc", "pty", "lp"}
)  # fmt: skip


def normalize_document_number(raw: str | None) -> str | None:
    """Canonical form of a document number: upper-case alphanumerics, with common prefixes
    ('INVOICE', 'INV', and 'NO' before a digit) and leading zeros removed. Idempotent."""
    if raw is None:
        return None
    value = re.sub(r"[^0-9A-Z]", "", raw.upper())
    while True:
        before = value
        for prefix in _NUMBER_PREFIXES:
            if value.startswith(prefix) and len(value) > len(prefix):
                value = value[len(prefix) :]
        if value.startswith("NO") and value[2:3].isdigit():
            value = value[2:]
        if value.startswith("0"):
            value = value.lstrip("0") or "0"
        if value == before:
            break
    if not value or value in _NUMBER_PREFIXES:
        return None
    return value


def normalize_vendor(name: str | None) -> str | None:
    """Canonical vendor key: lower-case words, '&' as 'and', without punctuation, a leading
    'the', or trailing legal suffixes (Inc, LLC, Ltd, ...)."""
    if name is None:
        return None
    words = re.sub(r"[^0-9a-z]+", " ", name.lower().replace("&", " and ")).split()
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words) or None


class Fingerprint(BaseModel):
    """The facts about a document that duplicate detection compares."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    file_sha256: str
    doc_type: DocumentType
    vendor_key: str | None
    number_key: str | None
    reference_key: str | None
    total: Money | None
    issue_date: date | None

    @classmethod
    def from_document(cls, doc_id: str, file_sha256: str, doc: ExtractedDocument) -> "Fingerprint":
        return cls(
            doc_id=doc_id,
            file_sha256=file_sha256,
            doc_type=doc.doc_type,
            vendor_key=normalize_vendor(doc.vendor_name),
            number_key=normalize_document_number(doc.document_number),
            reference_key=normalize_document_number(doc.referenced_document_number),
            total=doc.total,
            issue_date=doc.issue_date,
        )

    @property
    def is_credit(self) -> bool:
        return self.doc_type is DocumentType.CREDIT_NOTE


class DuplicateKind(StrEnum):
    EXACT_FILE = "exact_file"
    SAME_NUMBER = "same_number"
    DUPLICATE_CREDIT_NOTE = "duplicate_credit_note"
    NEAR_DUPLICATE = "near_duplicate"

    @property
    def is_hard(self) -> bool:
        return self is not DuplicateKind.NEAR_DUPLICATE


class DuplicateMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: DuplicateKind
    other_doc_id: str
    detail: str

    @property
    def is_hard(self) -> bool:
        return self.kind.is_hard


def find_duplicates(
    candidate: Fingerprint,
    history: Iterable[Fingerprint],
    *,
    window_days: int = NEAR_DUPLICATE_WINDOW_DAYS,
) -> tuple[DuplicateMatch, ...]:
    """Compare a document with previously seen documents of the same client.

    Reports at most one match per earlier document (the strongest one).
    """
    matches: list[DuplicateMatch] = []
    for other in history:
        if other.doc_id == candidate.doc_id:
            continue
        match = _compare(candidate, other, window_days)
        if match is not None:
            matches.append(match)
    return tuple(matches)


def _compare(  # noqa: PLR0911 - one return per rule reads clearest
    c: Fingerprint, o: Fingerprint, window_days: int
) -> DuplicateMatch | None:
    if c.file_sha256 == o.file_sha256:
        return DuplicateMatch(
            kind=DuplicateKind.EXACT_FILE,
            other_doc_id=o.doc_id,
            detail="identical file was already submitted",
        )
    if c.vendor_key is None or c.vendor_key != o.vendor_key or c.is_credit != o.is_credit:
        return None

    if c.number_key is not None and c.number_key == o.number_key:
        return DuplicateMatch(
            kind=DuplicateKind.SAME_NUMBER,
            other_doc_id=o.doc_id,
            detail=f"same vendor and document number ({c.number_key})",
        )
    if c.is_credit:
        if c.reference_key is not None and c.reference_key == o.reference_key:
            return DuplicateMatch(
                kind=DuplicateKind.DUPLICATE_CREDIT_NOTE,
                other_doc_id=o.doc_id,
                detail=f"another credit note already refers to document {c.reference_key}",
            )
        return None

    if (
        c.total is not None
        and c.total == o.total
        and c.issue_date is not None
        and o.issue_date is not None
        and abs((c.issue_date - o.issue_date).days) <= window_days
    ):
        return DuplicateMatch(
            kind=DuplicateKind.NEAR_DUPLICATE,
            other_doc_id=o.doc_id,
            detail=(
                f"same vendor and total ({c.total}) within {window_days} days, "
                f"different or missing document number"
            ),
        )
    return None
