"""Classification and extraction, with confidence built from checks we can verify.

Model self-reported confidence is poorly calibrated, so it is not used. Each extracted field
gets a confidence from evidence instead:

- PDF with a text layer: a field found in the text scores 1.0; one not found scores 0.5.
- Second pass: run when a PDF has an ungrounded field, a value that could not be parsed, or a
  validation issue, and always for images (which have no text layer). An independent second
  reading that disagrees drops the field to 0.3; one that agrees lifts an ungrounded PDF field
  to 0.7 and scores an image field 0.9.
- A value the model returned in a form that cannot be parsed scores 0.0.

The document's extraction confidence is its weakest field.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from accrueboard.domain.documents import DocumentType, ExtractedDocument, LineItem
from accrueboard.domain.validation import validate_document
from accrueboard.llm.client import LLMClient
from accrueboard.llm.prompts import (
    CLASSIFY_SCHEMA,
    CLASSIFY_SYSTEM,
    CLASSIFY_VERSION,
    EXTRACT_SCHEMA,
    EXTRACT_SYSTEM,
    EXTRACT_VERSION,
    VERIFY_SYSTEM,
    VERIFY_VERSION,
    extraction_instruction,
)
from accrueboard.llm.types import FilePart, LLMRequest, LLMResponse, TextPart
from accrueboard.pipeline.files import SourceFile, pdf_text
from accrueboard.pipeline.grounding import ground

GROUNDED = 1.0
UNGROUNDED = 0.5
AGREED_UNGROUNDED = 0.7
AGREED_IMAGE = 0.9
DISAGREED = 0.3
UNPARSABLE = 0.0


class CallRecord(BaseModel):
    """What the audit trail keeps about one model call."""

    model_config = ConfigDict(frozen=True)

    purpose: str
    prompt_version: str
    model: str
    request_key: str
    cost_usd: Decimal
    latency_ms: int
    input_tokens: int
    output_tokens: int
    replayed: bool

    @classmethod
    def of(cls, request: LLMRequest, response: LLMResponse) -> "CallRecord":
        return cls(
            purpose=request.purpose,
            prompt_version=request.prompt_version,
            model=request.model,
            request_key=request.key,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            replayed=response.replayed,
        )


@dataclass(frozen=True)
class Models:
    classify: str
    extract: str
    verify: str


# ---------------------------------------------------------------------------- classification


class Classification(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_type: DocumentType
    evidence: str
    call: CallRecord


def classify(llm: LLMClient, source: SourceFile, model: str) -> Classification:
    request = LLMRequest(
        purpose="classify",
        prompt_version=CLASSIFY_VERSION,
        model=model,
        system=CLASSIFY_SYSTEM,
        parts=(
            FilePart(media_type=source.media_type, data=source.data),
            TextPart(text="Classify this document."),
        ),
        output_schema=CLASSIFY_SCHEMA,
        max_tokens=512,
    )
    response = llm.complete(request)
    return Classification(
        doc_type=DocumentType(response.output["doc_type"]),
        evidence=str(response.output.get("evidence", "")),
        call=CallRecord.of(request, response),
    )


# ---------------------------------------------------------------------------- parsing model output


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("$", "").replace("USD", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return number


def _money(value: object) -> Decimal | None:
    number = _decimal(value)
    return None if number is None else abs(number)


def _rate(value: object) -> Decimal | None:
    number = _decimal(value)
    if number is None:
        return None
    return number / 100 if number >= 1 else number


def _date(value: object) -> date | None:
    if value in (None, ""):
        return None
    return date.fromisoformat(str(value))


@dataclass(frozen=True)
class Parsed:
    document: ExtractedDocument
    errors: frozenset[str]
    """Fields whose model output could not be parsed (they are left empty)."""


def to_document(output: dict[str, Any], doc_type: DocumentType) -> Parsed:
    """Convert a model's extraction output into a domain document, field by field."""
    errors: set[str] = set()
    fields: dict[str, Any] = {"doc_type": doc_type}

    def take(name: str, convert: Any) -> None:
        try:
            value = convert(output.get(name))
            if value is not None:
                fields[name] = value
        except (ValueError, TypeError):
            errors.add(name)

    for name in (
        "vendor_name",
        "vendor_state",
        "document_number",
        "po_number",
        "referenced_document_number",
    ):
        take(name, lambda v: (str(v).strip() or None) if v is not None else None)
    take("issue_date", _date)
    take("due_date", _date)
    for name in ("subtotal", "discount", "shipping", "tax", "total"):
        take(name, _money)
    take("tax_rate", _rate)
    take("payment_method", lambda v: v if v in ("card", "bank") else None)

    lines: list[LineItem] = []
    for i, raw in enumerate(output.get("lines") or []):
        try:
            lines.append(
                LineItem.model_validate(
                    {
                        "description": str(raw["description"]).strip(),
                        "quantity": _decimal(raw["quantity"]),
                        "unit_price": _money(raw["unit_price"]),
                        "amount": _money(raw["amount"]),
                        "taxable": bool(raw["taxable"]),
                    }
                )
            )
        except (ValueError, TypeError, KeyError, ValidationError):
            errors.add(f"lines[{i}]")
    fields["lines"] = tuple(lines)

    # Validate field by field so one bad value does not discard the whole document.
    document = ExtractedDocument(doc_type=doc_type)
    for name, value in fields.items():
        try:
            document = ExtractedDocument.model_validate({**document.model_dump(), name: value})
        except ValidationError:
            errors.add(name)
    return Parsed(document=document, errors=frozenset(errors))


# ---------------------------------------------------------------------------- comparison


def field_values(doc: ExtractedDocument) -> dict[str, object]:
    """Comparable, normalized values of every printed field."""

    def norm(value: str | None) -> str | None:
        return " ".join(value.casefold().split()) if value else None

    values: dict[str, object] = {
        "vendor_name": norm(doc.vendor_name),
        "document_number": norm(doc.document_number),
        "po_number": norm(doc.po_number),
        "referenced_document_number": norm(doc.referenced_document_number),
        "issue_date": doc.issue_date,
        "due_date": doc.due_date,
        "subtotal": doc.subtotal,
        "discount": doc.discount or None,
        "shipping": doc.shipping or None,
        "tax": doc.tax or None,
        "tax_rate": doc.tax_rate,
        "total": doc.total,
        "payment_method": doc.payment_method,
        "line_count": len(doc.lines),
    }
    for i, item in enumerate(doc.lines):
        values[f"lines[{i}].description"] = norm(item.description)
        values[f"lines[{i}].quantity"] = item.quantity
        values[f"lines[{i}].unit_price"] = item.unit_price
        values[f"lines[{i}].amount"] = item.amount
        values[f"lines[{i}].taxable"] = item.taxable
    return {k: v for k, v in values.items() if v is not None}


# ---------------------------------------------------------------------------- extraction


class FieldCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    grounded: bool | None
    """None when the file has no text layer to check against."""
    agreed: bool | None
    """None when no second pass ran."""
    confidence: float


class ExtractionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    document: ExtractedDocument
    fields: tuple[FieldCheck, ...]
    confidence: float
    ungrounded: frozenset[str]
    unparsable: frozenset[str]
    second_pass: bool
    second_pass_reason: str | None
    calls: tuple[CallRecord, ...]

    @property
    def disagreements(self) -> tuple[str, ...]:
        return tuple(f.field for f in self.fields if f.agreed is False)


def _request(
    *,
    purpose: str,
    version: str,
    system: str,
    model: str,
    source: SourceFile,
    doc_type: DocumentType,
) -> LLMRequest:
    return LLMRequest(
        purpose=purpose,
        prompt_version=version,
        model=model,
        system=system,
        parts=(
            FilePart(media_type=source.media_type, data=source.data),
            TextPart(text=extraction_instruction(doc_type.value)),
        ),
        output_schema=EXTRACT_SCHEMA,
        max_tokens=4096,
    )


def field_confidence(*, has_text_layer: bool, grounded: bool | None, agreed: bool | None) -> float:
    """Confidence of one field from the evidence available (see the module docstring)."""
    if not has_text_layer:
        return AGREED_IMAGE if agreed else DISAGREED
    if agreed is False:
        return DISAGREED
    if grounded is False:
        return AGREED_UNGROUNDED if agreed else UNGROUNDED
    return GROUNDED


def _second_pass_reason(
    doc: ExtractedDocument, grounding: dict[str, bool] | None, parsed: Parsed, today: date
) -> str | None:
    if grounding is None:
        return "image has no text layer"
    ungrounded = sorted(k for k, ok in grounding.items() if not ok)
    if ungrounded:
        return "values not found in the text layer: " + ", ".join(ungrounded)
    if parsed.errors:
        return "unparsable values: " + ", ".join(sorted(parsed.errors))
    if validate_document(doc, today=today):
        return "document failed validation"
    return None


def extract(
    llm: LLMClient,
    source: SourceFile,
    doc_type: DocumentType,
    models: Models,
    *,
    today: date,
) -> ExtractionResult:
    first_request = _request(
        purpose="extract",
        version=EXTRACT_VERSION,
        system=EXTRACT_SYSTEM,
        model=models.extract,
        source=source,
        doc_type=doc_type,
    )
    first_response = llm.complete(first_request)
    calls = [CallRecord.of(first_request, first_response)]
    first = to_document(first_response.output, doc_type)
    doc = first.document

    grounding = ground(doc, pdf_text(source.data)) if source.is_pdf else None
    reason = _second_pass_reason(doc, grounding, first, today)

    second_values: dict[str, object] | None = None
    if reason is not None:
        verify_request = _request(
            purpose="verify",
            version=VERIFY_VERSION,
            system=VERIFY_SYSTEM,
            model=models.verify,
            source=source,
            doc_type=doc_type,
        )
        verify_response = llm.complete(verify_request)
        calls.append(CallRecord.of(verify_request, verify_response))
        second_values = field_values(to_document(verify_response.output, doc_type).document)

    checks: list[FieldCheck] = []
    for name, value in field_values(doc).items():
        grounded = None if grounding is None else grounding.get(name)
        agreed = None if second_values is None else second_values.get(name) == value
        confidence = field_confidence(
            has_text_layer=grounding is not None, grounded=grounded, agreed=agreed
        )
        checks.append(
            FieldCheck(field=name, grounded=grounded, agreed=agreed, confidence=confidence)
        )
    checks += [
        FieldCheck(field=name, grounded=None, agreed=None, confidence=UNPARSABLE)
        for name in sorted(first.errors)
    ]

    return ExtractionResult(
        document=doc,
        fields=tuple(checks),
        confidence=min((c.confidence for c in checks), default=0.0),
        ungrounded=frozenset(k for k, ok in (grounding or {}).items() if not ok),
        unparsable=first.errors,
        second_pass=second_values is not None,
        second_pass_reason=reason,
        calls=tuple(calls),
    )
