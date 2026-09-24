"""A stand-in model that reads documents perfectly, from the generator's ground truth.

Used where an experiment needs the pipeline to run without model error: integration tests, and
evaluations that isolate one component (for example the review assistant) from extraction and
coding mistakes.
"""

from typing import Any

from accrueboard.datagen.records import GroundTruth
from accrueboard.domain.documents import ExtractedDocument
from accrueboard.llm.types import FilePart, LLMError, LLMRequest


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


class Oracle:
    """Answers classify and extract from the file's ground truth, and code from the document it
    last read (the processor handles one document at a time). Use as ``FakeLLM(oracle)``."""

    def __init__(self) -> None:
        self.by_sha: dict[str, GroundTruth] = {}
        self.current: GroundTruth | None = None
        self.fail_for: set[str] = set()
        """Document ids whose extraction should fail, to simulate an outage."""

    def register(self, sha256: str, record: GroundTruth) -> None:
        self.by_sha[sha256] = record

    def __call__(self, request: LLMRequest) -> dict[str, Any]:
        if request.purpose == "code":
            if self.current is None:
                raise LLMError("no document in context")
            return {
                "lines": [
                    {"line": i, "account": a, "reason": "history"}
                    for i, a in enumerate(self.current.line_accounts)
                ]
            }
        part = next(p for p in request.parts if isinstance(p, FilePart))
        record = self.by_sha[part.sha256]
        if record.doc_id in self.fail_for:
            raise LLMError("simulated outage")
        self.current = record
        if request.purpose == "classify":
            return {"doc_type": record.document.doc_type.value, "evidence": "title"}
        return truth_output(record.document)
