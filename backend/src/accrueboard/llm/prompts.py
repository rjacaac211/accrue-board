"""Versioned prompts and output schemas for the pipeline's model calls.

Bump a prompt's version whenever its text or schema changes: the version is part of the
record/replay key and of the audit trail, so every decision can be traced to the exact prompt.

Amounts are requested as plain decimal *strings* (never JSON numbers), so money never passes
through a binary float between the model and the domain layer.
"""

from typing import Any

_NULLABLE_STRING: dict[str, Any] = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NULLABLE_DATE: dict[str, Any] = {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]}

# ---------------------------------------------------------------------------- classification

CLASSIFY_VERSION = "classify-v1"

CLASSIFY_SYSTEM = """\
You sort documents received by the bookkeeping team of a small business. Decide which one \
of these the document is:

- invoice: a supplier's bill asking the business to pay for goods or services.
- receipt: proof that a purchase was already paid (e.g. a card or till receipt).
- credit_note: a credit memo from a supplier that reduces an amount the business owes.
- other: anything else, such as a statement of account listing several invoices, a quotation \
or estimate, a payment reminder, or a document you cannot read.

Judge by what the document is, not by its title alone. Give a one-sentence reason quoting \
the words on the page that decided it."""

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": ["invoice", "receipt", "credit_note", "other"]},
        "evidence": {"type": "string"},
    },
    "required": ["doc_type", "evidence"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------- extraction

EXTRACT_VERSION = "extract-v2"
VERIFY_VERSION = "verify-v2"

_EXTRACT_RULES = """\
Rules:
- Copy values exactly as printed. Never compute, infer or correct a value; if the document's \
own arithmetic looks wrong, still report what is printed.
- Amounts, quantities and unit prices: plain decimal strings without currency symbols or \
thousands separators, e.g. "1234.50". Report a discount as a positive amount.
- Dates: YYYY-MM-DD.
- tax_rate: only if a percentage is printed, as a decimal fraction string ("8.25%" -> "0.0825").
- A line is taxable only if the document marks it as taxable (for example a "T" in a tax \
column, or an asterisk explained as "taxable item").
- vendor_name is the business that issued the document, not the customer it is addressed to. \
vendor_state is the two-letter US state from the vendor's address.
- document_number is the invoice, receipt or credit memo number. referenced_document_number \
is the invoice a credit memo applies to.
- payment_method: how the document says it was already paid: "card" if paid or charged by \
card, including a note that it was charged to the card or payment method on file (autopay); \
"bank" if paid by bank transfer; null if it is still to be paid or does not say.
- Use null for anything not printed on the document."""

EXTRACT_SYSTEM = f"""\
You extract structured data from a supplier document for bookkeeping.

{_EXTRACT_RULES}"""

VERIFY_SYSTEM = f"""\
You are the second, independent reader of a supplier document. Another reader has already \
extracted it; your reading is compared with theirs field by field, so read every value from \
the page yourself, slowly, character by character. Pay particular attention to digits that \
are easy to confuse, to which column a number sits in, and to dates.

{_EXTRACT_RULES}"""

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vendor_name": _NULLABLE_STRING,
        "vendor_state": _NULLABLE_STRING,
        "document_number": _NULLABLE_STRING,
        "issue_date": _NULLABLE_DATE,
        "due_date": _NULLABLE_DATE,
        "po_number": _NULLABLE_STRING,
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": "string"},
                    "unit_price": {"type": "string"},
                    "amount": {"type": "string"},
                    "taxable": {"type": "boolean"},
                },
                "required": ["description", "quantity", "unit_price", "amount", "taxable"],
                "additionalProperties": False,
            },
        },
        "subtotal": _NULLABLE_STRING,
        "discount": _NULLABLE_STRING,
        "shipping": _NULLABLE_STRING,
        "tax_rate": _NULLABLE_STRING,
        "tax": _NULLABLE_STRING,
        "total": _NULLABLE_STRING,
        "payment_method": {
            "anyOf": [{"type": "string", "enum": ["card", "bank"]}, {"type": "null"}]
        },
        "referenced_document_number": _NULLABLE_STRING,
    },
    "required": [
        "vendor_name",
        "vendor_state",
        "document_number",
        "issue_date",
        "due_date",
        "po_number",
        "lines",
        "subtotal",
        "discount",
        "shipping",
        "tax_rate",
        "tax",
        "total",
        "payment_method",
        "referenced_document_number",
    ],
    "additionalProperties": False,
}


def extraction_instruction(doc_type: str) -> str:
    kind = doc_type.replace("_", " ")
    return f"This document has been classified as: {kind}. Extract its fields."


# ---------------------------------------------------------------------------- account coding

CODE_VERSION = "code-v1"

CODE_SYSTEM_TEMPLATE = """\
You are the bookkeeper for {client_name}, {business}. Assign every line item on a supplier \
document to one account from the client's chart of accounts below.

How to decide:
- This client codes consistently. Follow how the same vendor and similar items were coded \
before (history is given with each document) unless the item is clearly different.
- Stock bought for resale goes to Inventory, not Cost of Goods Sold.
- Computers, printers, scanners, shelving and similar equipment go to Small Equipment \
whatever their price; a separate rule capitalizes expensive items.
- Advance payments for future periods (such as an annual plan) go to Prepaid Expenses.

Give a short reason for each line, naming the history or rule you relied on.

Chart of accounts:
{chart}"""


def code_schema(account_codes: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line": {"type": "integer"},
                        "account": {"type": "string", "enum": account_codes},
                        "reason": {"type": "string"},
                    },
                    "required": ["line", "account", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["lines"],
        "additionalProperties": False,
    }
