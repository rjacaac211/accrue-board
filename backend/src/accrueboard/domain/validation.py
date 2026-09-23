"""Deterministic document validation.

Every check here is arithmetic or structural; none needs a model. Any issue found is a hard
routing rule (the document goes to a human), because a document that does not add up must
not be posted, whether the error is in the extraction or on the document itself.

Conventions (US sales tax on purchases):
- Tax applies to lines marked ``taxable``. Shipping is treated as non-taxable.
- A document-level discount reduces the taxable base pro rata
  (``taxable_base = taxable_lines - discount * taxable_lines / subtotal``).
- If a rate is printed, ``round(taxable_base * rate)`` must match the tax within one cent.
  If not, the implied rate must be plausible (0% to 12%, above every US combined rate).
"""

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from accrueboard.domain.documents import SUPPORTED_TYPES, DocumentType, ExtractedDocument
from accrueboard.domain.money import CENT, round_money


class IssueCode(StrEnum):
    MISSING_FIELD = "missing_field"
    NO_LINE_ITEMS = "no_line_items"
    NEGATIVE_AMOUNT = "negative_amount"
    LINE_AMOUNT_MISMATCH = "line_amount_mismatch"
    SUBTOTAL_MISMATCH = "subtotal_mismatch"
    DISCOUNT_EXCEEDS_SUBTOTAL = "discount_exceeds_subtotal"
    TAX_RATE_MISMATCH = "tax_rate_mismatch"
    TAX_RATE_IMPLAUSIBLE = "tax_rate_implausible"
    TAX_WITHOUT_TAXABLE_LINES = "tax_without_taxable_lines"
    TOTAL_MISMATCH = "total_mismatch"
    ISSUE_DATE_IN_FUTURE = "issue_date_in_future"
    ISSUE_DATE_TOO_OLD = "issue_date_too_old"
    DUE_BEFORE_ISSUE = "due_before_issue"


class ValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: IssueCode
    field: str | None = None
    message: str


@dataclass(frozen=True)
class ValidationConfig:
    tolerance: Decimal = CENT
    max_tax_rate: Decimal = Decimal("0.12")
    max_age_months: int = 18


DEFAULT_VALIDATION = ValidationConfig()

_COMMON_REQUIRED = ("vendor_name", "issue_date", "subtotal", "total")
_TYPE_REQUIRED: dict[DocumentType, tuple[str, ...]] = {
    DocumentType.INVOICE: ("document_number",),
    DocumentType.RECEIPT: ("payment_method",),
    DocumentType.CREDIT_NOTE: ("document_number", "referenced_document_number"),
}


def validate_document(
    doc: ExtractedDocument,
    *,
    today: date,
    config: ValidationConfig = DEFAULT_VALIDATION,
) -> tuple[ValidationIssue, ...]:
    """Return every validation issue found, in a stable order. Empty means the document is sound.

    Documents of unsupported type are not validated; routing handles them separately.
    """
    if doc.doc_type not in SUPPORTED_TYPES:
        return ()
    issues: list[ValidationIssue] = []
    issues += _required_fields(doc)
    issues += _negative_amounts(doc)
    issues += _arithmetic(doc, config)
    issues += _dates(doc, today, config)
    return tuple(issues)


def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _required_fields(doc: ExtractedDocument) -> list[ValidationIssue]:
    issues = [
        ValidationIssue(code=IssueCode.MISSING_FIELD, field=name, message=f"{name} is missing")
        for name in (*_COMMON_REQUIRED, *_TYPE_REQUIRED.get(doc.doc_type, ()))
        if _is_missing(getattr(doc, name))
    ]
    if not doc.lines:
        issues.append(
            ValidationIssue(code=IssueCode.NO_LINE_ITEMS, message="document has no line items")
        )
    return issues


def _negative_amounts(doc: ExtractedDocument) -> list[ValidationIssue]:
    fields: list[tuple[str, Decimal | None]] = [
        ("subtotal", doc.subtotal),
        ("discount", doc.discount),
        ("shipping", doc.shipping),
        ("tax", doc.tax),
        ("total", doc.total),
    ]
    for i, item in enumerate(doc.lines):
        fields += [
            (f"lines[{i}].quantity", item.quantity),
            (f"lines[{i}].unit_price", item.unit_price),
            (f"lines[{i}].amount", item.amount),
        ]
    return [
        ValidationIssue(
            code=IssueCode.NEGATIVE_AMOUNT,
            field=name,
            message=f"{name} is negative ({value}); amounts are recorded as printed",
        )
        for name, value in fields
        if value is not None and value < 0
    ]


def _arithmetic(doc: ExtractedDocument, config: ValidationConfig) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    tol = config.tolerance

    for i, item in enumerate(doc.lines):
        expected = round_money(item.quantity * item.unit_price)
        if abs(expected - item.amount) > tol:
            issues.append(
                ValidationIssue(
                    code=IssueCode.LINE_AMOUNT_MISMATCH,
                    field=f"lines[{i}]",
                    message=(
                        f"line {i + 1}: {item.quantity} x {item.unit_price} = {expected}, "
                        f"but the line amount is {item.amount}"
                    ),
                )
            )

    if doc.subtotal is None or not doc.lines:
        return issues

    line_sum = sum((item.amount for item in doc.lines), Decimal(0))
    if line_sum != doc.subtotal:
        issues.append(
            ValidationIssue(
                code=IssueCode.SUBTOTAL_MISMATCH,
                field="subtotal",
                message=f"line items sum to {line_sum}, but the subtotal is {doc.subtotal}",
            )
        )

    if doc.discount > doc.subtotal:
        issues.append(
            ValidationIssue(
                code=IssueCode.DISCOUNT_EXCEEDS_SUBTOTAL,
                field="discount",
                message=f"discount {doc.discount} exceeds the subtotal {doc.subtotal}",
            )
        )
    else:
        issues += _tax(doc, doc.subtotal, config)

    if doc.total is not None:
        expected_total = doc.subtotal - doc.discount + doc.shipping + doc.tax
        if expected_total != doc.total:
            issues.append(
                ValidationIssue(
                    code=IssueCode.TOTAL_MISMATCH,
                    field="total",
                    message=(
                        f"subtotal - discount + shipping + tax = {expected_total}, "
                        f"but the total is {doc.total}"
                    ),
                )
            )
    return issues


def taxable_base(doc: ExtractedDocument, subtotal: Decimal) -> Decimal:
    """Taxable line amounts less their pro-rata share of any document-level discount."""
    taxable = sum((item.amount for item in doc.lines if item.taxable), Decimal(0))
    if subtotal <= 0 or doc.discount == 0:
        return taxable
    return taxable - doc.discount * taxable / subtotal


def _tax(
    doc: ExtractedDocument, subtotal: Decimal, config: ValidationConfig
) -> list[ValidationIssue]:
    base = taxable_base(doc, subtotal)

    if doc.tax_rate is not None:
        issues: list[ValidationIssue] = []
        if doc.tax_rate > config.max_tax_rate:
            issues.append(
                ValidationIssue(
                    code=IssueCode.TAX_RATE_IMPLAUSIBLE,
                    field="tax_rate",
                    message=f"printed tax rate {doc.tax_rate:%} exceeds {config.max_tax_rate:%}",
                )
            )
        expected = round_money(base * doc.tax_rate)
        if abs(expected - doc.tax) > config.tolerance:
            issues.append(
                ValidationIssue(
                    code=IssueCode.TAX_RATE_MISMATCH,
                    field="tax",
                    message=(
                        f"{doc.tax_rate:%} of the taxable base {round_money(base)} is {expected}, "
                        f"but the tax is {doc.tax}"
                    ),
                )
            )
        return issues

    if doc.tax <= 0:
        return []
    if base <= 0:
        return [
            ValidationIssue(
                code=IssueCode.TAX_WITHOUT_TAXABLE_LINES,
                field="tax",
                message=f"tax of {doc.tax} is charged but no line is marked taxable",
            )
        ]
    implied = doc.tax / base
    if implied > config.max_tax_rate:
        return [
            ValidationIssue(
                code=IssueCode.TAX_RATE_IMPLAUSIBLE,
                field="tax",
                message=(
                    f"implied tax rate {implied:.2%} on a taxable base of {round_money(base)} "
                    f"exceeds {config.max_tax_rate:%}"
                ),
            )
        ]
    return []


def subtract_months(day: date, months: int) -> date:
    """Calendar month subtraction, clamping to the last day of the target month."""
    index = day.year * 12 + (day.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _dates(doc: ExtractedDocument, today: date, config: ValidationConfig) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if doc.issue_date is not None:
        if doc.issue_date > today:
            issues.append(
                ValidationIssue(
                    code=IssueCode.ISSUE_DATE_IN_FUTURE,
                    field="issue_date",
                    message=f"issue date {doc.issue_date} is after today ({today})",
                )
            )
        oldest = subtract_months(today, config.max_age_months)
        if doc.issue_date < oldest:
            issues.append(
                ValidationIssue(
                    code=IssueCode.ISSUE_DATE_TOO_OLD,
                    field="issue_date",
                    message=(
                        f"issue date {doc.issue_date} is more than "
                        f"{config.max_age_months} months old"
                    ),
                )
            )
        if doc.due_date is not None and doc.due_date < doc.issue_date:
            issues.append(
                ValidationIssue(
                    code=IssueCode.DUE_BEFORE_ISSUE,
                    field="due_date",
                    message=f"due date {doc.due_date} is before issue date {doc.issue_date}",
                )
            )
    return issues
