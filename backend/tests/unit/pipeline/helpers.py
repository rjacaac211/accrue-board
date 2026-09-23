"""Shared helpers: generated documents and fake models that answer from ground truth."""

from collections.abc import Callable
from functools import cache
from typing import Any

from accrueboard.datagen.generate import generate
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.render import render
from accrueboard.datagen.spec import EVAL_SPLITS, load_anomaly_catalog, load_client
from accrueboard.domain.documents import ExtractedDocument
from accrueboard.llm.client import FakeLLM
from accrueboard.llm.types import LLMRequest
from accrueboard.pipeline.files import SourceFile


@cache
def eval_records() -> tuple[GroundTruth, ...]:
    records = generate(load_client("fernhill"), load_anomaly_catalog(), 7)
    return tuple(r for r in records if r.split in EVAL_SPLITS)


@cache
def source_for(doc_id: str) -> SourceFile:
    record = next(r for r in eval_records() if r.doc_id == doc_id)
    return SourceFile.from_bytes(
        f"{doc_id}.{record.file_format}", render(record, load_client("fernhill"))
    )


def truth_output(doc: ExtractedDocument) -> dict[str, Any]:
    """Ground truth in the exact shape the extraction schema asks the model for."""

    def text(value: object) -> str | None:
        return None if value is None else str(value)

    return {
        "vendor_name": doc.vendor_name,
        "vendor_state": doc.vendor_state,
        "document_number": doc.document_number,
        "issue_date": text(doc.issue_date),
        "due_date": text(doc.due_date),
        "po_number": doc.po_number,
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
        "subtotal": text(doc.subtotal),
        "discount": str(doc.discount) if doc.discount else None,
        "shipping": str(doc.shipping) if doc.shipping else None,
        "tax_rate": text(doc.tax_rate),
        "tax": str(doc.tax) if doc.tax else None,
        "total": text(doc.total),
        "payment_method": doc.payment_method.value if doc.payment_method else None,
        "referenced_document_number": doc.referenced_document_number,
    }


Tamper = Callable[[str, dict[str, Any]], dict[str, Any]]


def oracle(record: GroundTruth, tamper: Tamper | None = None) -> FakeLLM:
    """A fake model that reads the document perfectly, optionally tampered per purpose."""

    def answer(request: LLMRequest) -> dict[str, Any]:
        if request.purpose == "classify":
            output: dict[str, Any] = {
                "doc_type": record.document.doc_type.value,
                "evidence": "title on the page",
            }
        else:
            output = truth_output(record.document)
        return tamper(request.purpose, output) if tamper else output

    return FakeLLM(answer)
