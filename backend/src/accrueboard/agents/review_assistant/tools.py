"""Read-only investigation tools and the suggestion they lead to.

The tools see exactly one client's data and never write: they are plain queries over the
documents, tasks, journal and knowledge store. Each returns a JSON-serialisable dict; errors a
model can fix (an unknown document id, say) come back as a result with ``error`` set rather
than an exception, so the investigation can continue.
"""

import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from accrueboard.agents.review_assistant.prompts import SUBMIT
from accrueboard.db.models import Document, Task
from accrueboard.domain.accounts import ChartOfAccounts
from accrueboard.domain.documents import ExtractedDocument
from accrueboard.domain.duplicates import normalize_document_number, normalize_vendor
from accrueboard.pipeline.files import pdf_text
from accrueboard.retrieval.knowledge import KnowledgeStore
from accrueboard.services import ledger

TEXT_LIMIT = 6000
RECENT_DOCUMENTS = 10
SIMILAR_ITEMS = 8
LOOKUP_LIMIT = 10


# ---------------------------------------------------------------------------- suggestion


class SuggestedAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    BLOCK = "block"


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    UNCERTAIN = "uncertain"


class RuleAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: str
    verdict: Verdict
    reason: str


class LineSuggestion(BaseModel):
    model_config = ConfigDict(frozen=True)

    line: int
    account: str
    reason: str


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    detail: str
    document_id: str | None = None


class Suggestion(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: SuggestedAction
    summary: str
    question: str | None = None
    rule_assessments: tuple[RuleAssessment, ...] = ()
    lines: tuple[LineSuggestion, ...] = ()
    evidence: tuple[Evidence, ...] = ()

    @property
    def accounts(self) -> list[str]:
        return [line.account for line in sorted(self.lines, key=lambda x: x.line)]


class InvalidSuggestionError(ValueError):
    pass


def parse_suggestion(raw: dict[str, Any], *, line_count: int, accounts: set[str]) -> Suggestion:
    """Check a submitted suggestion against the document; the message says what to fix."""
    try:
        suggestion = Suggestion.model_validate(raw)
    except ValidationError as exc:
        raise InvalidSuggestionError(f"the suggestion does not match the schema: {exc}") from exc
    numbers = sorted(line.line for line in suggestion.lines)
    if numbers != list(range(line_count)):
        raise InvalidSuggestionError(
            f"give exactly one entry per line, numbered 0 to {line_count - 1} "
            f"(got {numbers or 'none'})"
        )
    unknown = sorted({line.account for line in suggestion.lines} - accounts)
    if unknown:
        raise InvalidSuggestionError(f"unknown account(s) {', '.join(unknown)}")
    if suggestion.action is SuggestedAction.BLOCK and not (suggestion.question or "").strip():
        raise InvalidSuggestionError("a block recommendation needs the question to ask")
    return suggestion


# ---------------------------------------------------------------------------- tools


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def to_json(result: dict[str, Any]) -> str:
    return json.dumps(result, separators=(",", ":"), default=str)


@dataclass
class Toolbox:
    """The investigation tools, bound to one task's client and transaction."""

    session: Session
    task: Task
    chart: ChartOfAccounts
    store: KnowledgeStore

    @property
    def client_id(self) -> str:
        return self.task.client_id

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "get_document_text": lambda _: self.get_document_text(),
            "vendor_history": lambda args: self.vendor_history(str(args["vendor_name"])),
            "similar_transactions": lambda args: self.similar_transactions(str(args["query"])),
            "get_document": lambda args: self.get_document(str(args["document_id"])),
            "ledger_lookup": lambda args: self.ledger_lookup(
                str(args["document_number"]), args.get("vendor_name")
            ),
        }

    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = self.handlers().get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}" + (" here" if name == SUBMIT else "")}
        try:
            return handler(args)
        except KeyError as exc:
            return {"error": f"missing argument {exc}"}

    def _account_name(self, code: str) -> str:
        return self.chart.get(code).name if self.chart.has(code) else "unknown account"

    # ------------------------------------------------------------------ tools

    def get_document_text(self) -> dict[str, Any]:
        document = self.task.document
        if document.content is None or document.media_type != "application/pdf":
            return {
                "media_type": document.media_type,
                "text": None,
                "note": "no text layer (image or missing file); rely on the extracted values",
            }
        text = pdf_text(document.content)
        return {
            "media_type": document.media_type,
            "text": text[:TEXT_LIMIT],
            "truncated": len(text) > TEXT_LIMIT,
        }

    def vendor_history(self, vendor_name: str) -> dict[str, Any]:
        raw = normalize_vendor(vendor_name)
        if raw is None:
            return {"error": "empty vendor name"}
        key = self.store.resolve_vendor(self.client_id, raw) or raw
        counts = self.store.vendor_accounts(self.client_id, key)
        rows = self.session.execute(
            select(Document, Task)
            .join(Task, Task.document_id == Document.id)
            .where(
                Document.client_id == self.client_id,
                Document.vendor_key == key,
                Document.id != self.task.document_id,
            )
            .order_by(Document.issue_date.desc(), Document.id)
        ).all()
        settled = [d.total for d, t in rows if d.total is not None and t.state == "posted"]
        return {
            "vendor_key": key,
            "known": bool(counts) or bool(rows),
            "past_line_items_by_account": [
                {"account": code, "account_name": self._account_name(code), "lines": n}
                for code, n in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            ],
            "posted_documents": len(settled),
            "median_posted_total": _money(statistics.median(settled)) if settled else None,
            "largest_posted_total": _money(max(settled)) if settled else None,
            "recent_documents": [self._brief(d, t) for d, t in rows[:RECENT_DOCUMENTS]],
        }

    def similar_transactions(self, query: str) -> dict[str, Any]:
        hits = self.store.search(self.client_id, query, SIMILAR_ITEMS)
        return {
            "items": [
                {
                    "vendor": h.entry.vendor_name,
                    "description": h.entry.description,
                    "amount": _money(h.entry.amount),
                    "account": h.entry.account,
                    "account_name": self._account_name(h.entry.account),
                    "source": h.entry.source.value,
                }
                for h in hits
            ]
        }

    def get_document(self, document_id: str) -> dict[str, Any]:
        document = self.session.get(Document, document_id)
        if document is None or document.client_id != self.client_id:
            return {"error": f"no document {document_id!r} for this client"}
        task = document.task
        detail = self._brief(document, task)
        detail["received_at"] = document.received_at.date().isoformat()
        detail["same_file_as_document_under_review"] = (
            document.sha256 is not None and document.sha256 == self.task.document.sha256
        )
        if document.extracted is not None:
            doc = ExtractedDocument.model_validate(document.extracted)
            detail |= {
                "po_number": doc.po_number,
                "referenced_document_number": doc.referenced_document_number,
                "lines": [
                    {
                        "description": item.description,
                        "quantity": str(item.quantity),
                        "unit_price": str(item.unit_price),
                        "amount": str(item.amount),
                        "taxable": item.taxable,
                    }
                    for item in doc.lines
                ],
                "subtotal": _money(doc.subtotal),
                "discount": _money(doc.discount),
                "shipping": _money(doc.shipping),
                "tax": _money(doc.tax),
            }
        detail["posted_accounts"] = [
            {"account": code, "account_name": self._account_name(code)}
            for code in (task.line_accounts or [])
        ]
        detail["journal_entries"] = [
            {
                "date": entry.entry_date.isoformat(),
                "is_reversal": entry.reverses is not None,
                "lines": [
                    {
                        "account": line.account_code,
                        "debit": _money(line.debit),
                        "credit": _money(line.credit),
                    }
                    for line in entry.lines
                ],
            }
            for entry in ledger.entries_for_task(self.session, task.id)
        ]
        return detail

    def ledger_lookup(self, document_number: str, vendor_name: str | None) -> dict[str, Any]:
        number = normalize_document_number(document_number)
        if number is None:
            return {"error": "empty document number"}
        query = (
            select(Document, Task)
            .join(Task, Task.document_id == Document.id)
            .where(
                Document.client_id == self.client_id,
                Document.id != self.task.document_id,
                Document.number_key == number,
            )
            .order_by(Document.issue_date, Document.id)
        )
        vendor_key: str | None = None
        if vendor_name:
            raw = normalize_vendor(vendor_name)
            vendor_key = (self.store.resolve_vendor(self.client_id, raw) or raw) if raw else None
            if vendor_key is not None:
                query = query.where(Document.vendor_key == vendor_key)
        rows = self.session.execute(query.limit(LOOKUP_LIMIT)).all()
        return {
            "normalized_number": number,
            "vendor_key": vendor_key,
            "matches": [self._brief(d, t) for d, t in rows],
        }

    # ------------------------------------------------------------------ helpers

    def _brief(self, document: Document, task: Task) -> dict[str, Any]:
        vendor = None
        if document.extracted is not None:
            vendor = document.extracted.get("vendor_name")
        return {
            "document_id": document.id,
            "doc_type": document.doc_type,
            "vendor": vendor,
            "document_number": (document.extracted or {}).get("document_number"),
            "issue_date": document.issue_date.isoformat() if document.issue_date else None,
            "total": _money(document.total),
            "status": task.state,
        }
