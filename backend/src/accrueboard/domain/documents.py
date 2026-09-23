"""Structured document data as produced by extraction.

Fields that extraction may fail to find are optional here; :mod:`accrueboard.domain.validation`
reports them as missing instead of rejecting the whole document, so a partially extracted
document can still reach a human reviewer with a precise explanation.

Sign convention: every amount is recorded as printed (non-negative). The document type decides
the direction of the journal entry, so a credit note for 50.00 has ``total == 50.00``.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer

from accrueboard.domain.money import ZERO, Money, Quantity, UnitPrice


class DocumentType(StrEnum):
    INVOICE = "invoice"
    RECEIPT = "receipt"
    CREDIT_NOTE = "credit_note"
    OTHER = "other"


SUPPORTED_TYPES: frozenset[DocumentType] = frozenset(
    {DocumentType.INVOICE, DocumentType.RECEIPT, DocumentType.CREDIT_NOTE}
)


class PaymentMethod(StrEnum):
    CARD = "card"
    BANK = "bank"


def _tax_rate(value: object) -> Decimal:
    if isinstance(value, float):
        raise ValueError("float is not allowed for tax rates; pass a str or Decimal")
    if not isinstance(value, Decimal | int | str):
        raise ValueError("tax rate must be a decimal fraction, e.g. '0.0825'")
    rate = Decimal(value)
    if not rate.is_finite() or rate < 0 or rate >= 1:
        raise ValueError("tax rate must be a fraction in [0, 1), e.g. '0.0825' for 8.25%")
    return rate


TaxRate = Annotated[
    Decimal,
    BeforeValidator(_tax_rate),
    PlainSerializer(str, return_type=str, when_used="json"),
]
"""A tax rate as a decimal fraction (``0.0825`` means 8.25%)."""


class LineItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    description: str
    quantity: Quantity
    unit_price: UnitPrice
    amount: Money
    taxable: bool = False


class ExtractedDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_type: DocumentType
    vendor_name: str | None = None
    vendor_state: str | None = None
    document_number: str | None = None
    issue_date: date | None = None
    due_date: date | None = None
    po_number: str | None = None
    lines: tuple[LineItem, ...] = ()
    subtotal: Money | None = None
    discount: Money = ZERO
    shipping: Money = ZERO
    tax_rate: TaxRate | None = None
    tax: Money = ZERO
    total: Money | None = None
    payment_method: PaymentMethod | None = None
    referenced_document_number: str | None = None
