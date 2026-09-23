from datetime import date

import pytest
from pydantic import ValidationError

from accrueboard.domain.documents import DocumentType, ExtractedDocument, PaymentMethod
from accrueboard.domain.validation import IssueCode, validate_document

from .factories import TODAY, invoice, line


def codes(doc: ExtractedDocument, today: date = TODAY) -> set[IssueCode]:
    return {issue.code for issue in validate_document(doc, today=today)}


def test_valid_invoice_has_no_issues() -> None:
    assert validate_document(invoice(), today=TODAY) == ()


# ------------------------------------------------------------------ required fields


@pytest.mark.parametrize("field", ["vendor_name", "issue_date", "subtotal", "total"])
def test_common_required_fields(field: str) -> None:
    issues = validate_document(invoice(**{field: None}), today=TODAY)
    assert any(i.code == IssueCode.MISSING_FIELD and i.field == field for i in issues)


def test_invoice_requires_document_number() -> None:
    issues = validate_document(invoice(document_number=None), today=TODAY)
    assert [(i.code, i.field) for i in issues] == [(IssueCode.MISSING_FIELD, "document_number")]


def test_blank_strings_count_as_missing() -> None:
    assert IssueCode.MISSING_FIELD in codes(invoice(vendor_name="   "))


def test_receipt_requires_payment_method_but_not_number() -> None:
    receipt = invoice(doc_type=DocumentType.RECEIPT, document_number=None, due_date=None)
    issues = validate_document(receipt, today=TODAY)
    assert [(i.code, i.field) for i in issues] == [(IssueCode.MISSING_FIELD, "payment_method")]
    assert codes(receipt.model_copy(update={"payment_method": PaymentMethod.CARD})) == set()


def test_credit_note_requires_reference() -> None:
    credit = invoice(doc_type=DocumentType.CREDIT_NOTE, document_number="CN-7")
    issues = validate_document(credit, today=TODAY)
    assert [(i.code, i.field) for i in issues] == [
        (IssueCode.MISSING_FIELD, "referenced_document_number")
    ]


def test_document_needs_line_items() -> None:
    assert IssueCode.NO_LINE_ITEMS in codes(invoice(lines=()))


def test_other_documents_are_not_validated() -> None:
    assert validate_document(ExtractedDocument(doc_type=DocumentType.OTHER), today=TODAY) == ()


# ------------------------------------------------------------------ arithmetic


def test_line_amount_must_match_quantity_times_price() -> None:
    # The exempt line is misprinted (10 x 12.50 != 126.00); subtotal and total follow the lines.
    bad = invoice(
        lines=(line(), line("Widget stock", "10", "12.50", "126.00", taxable=False)),
        subtotal="216.00",
        total="233.43",
    )
    issues = validate_document(bad, today=TODAY)
    assert [(i.code, i.field) for i in issues] == [(IssueCode.LINE_AMOUNT_MISMATCH, "lines[1]")]


def test_line_amount_allows_one_cent_rounding() -> None:
    # 3 x 3.3333 = 9.9999 -> printed as 10.00
    doc = invoice(
        lines=(line("Labels", "3", "3.3333", "10.00", taxable=False),),
        subtotal="10.00",
        shipping="0.00",
        tax_rate=None,
        tax="0.00",
        total="10.00",
    )
    assert codes(doc) == set()


def test_subtotal_must_equal_sum_of_lines() -> None:
    assert codes(invoice(subtotal="214.99", total="232.42")) == {IssueCode.SUBTOTAL_MISMATCH}


def test_total_must_reconcile() -> None:
    assert codes(invoice(total="232.44")) == {IssueCode.TOTAL_MISMATCH}


def test_discount_reduces_total_and_taxable_base() -> None:
    # discount 21.50 = 10% of subtotal; taxable base 90.00 -> 81.00; tax 6.68; total 210.18
    doc = invoice(discount="21.50", tax="6.68", total="210.18")
    assert codes(doc) == set()


def test_discount_cannot_exceed_subtotal() -> None:
    assert IssueCode.DISCOUNT_EXCEEDS_SUBTOTAL in codes(invoice(discount="300.00"))


def test_negative_amounts_are_flagged() -> None:
    assert IssueCode.NEGATIVE_AMOUNT in codes(invoice(shipping="-10.00", total="212.43"))


# ------------------------------------------------------------------ tax


def test_printed_rate_must_match_tax_amount() -> None:
    assert codes(invoice(tax="7.50", total="232.50")) == {IssueCode.TAX_RATE_MISMATCH}


def test_printed_rate_tolerates_one_cent() -> None:
    assert codes(invoice(tax="7.42", total="232.42")) == set()


def test_implied_rate_when_no_rate_printed() -> None:
    assert codes(invoice(tax_rate=None)) == set()  # 7.43 / 90.00 = 8.26%
    # 15.00 / 90.00 = 16.7% is outside the plausible US range
    assert codes(invoice(tax_rate=None, tax="15.00", total="240.00")) == {
        IssueCode.TAX_RATE_IMPLAUSIBLE
    }


def test_printed_rate_above_plausible_range() -> None:
    assert IssueCode.TAX_RATE_IMPLAUSIBLE in codes(
        invoice(tax_rate="0.20", tax="18.00", total="243.00")
    )


def test_tax_without_taxable_lines() -> None:
    doc = invoice(
        lines=(line("Widget stock", "10", "12.50", "125.00", taxable=False),),
        subtotal="125.00",
        shipping="0.00",
        tax_rate=None,
        tax="5.00",
        total="130.00",
    )
    assert codes(doc) == {IssueCode.TAX_WITHOUT_TAXABLE_LINES}


def test_tax_rate_rejects_floats_and_out_of_range() -> None:
    with pytest.raises(ValidationError):
        invoice(tax_rate=0.0825)
    with pytest.raises(ValidationError):
        invoice(tax_rate="1.5")


# ------------------------------------------------------------------ dates


def test_issue_date_in_future() -> None:
    assert codes(invoice(issue_date=date(2026, 6, 16), due_date=None)) == {
        IssueCode.ISSUE_DATE_IN_FUTURE
    }


def test_issue_date_too_old() -> None:
    # 18 months before 2026-06-15 is 2024-12-15
    assert codes(invoice(issue_date=date(2024, 12, 15), due_date=None)) == set()
    assert codes(invoice(issue_date=date(2024, 12, 14), due_date=None)) == {
        IssueCode.ISSUE_DATE_TOO_OLD
    }


def test_due_date_before_issue_date() -> None:
    assert codes(invoice(due_date=date(2026, 5, 31))) == {IssueCode.DUE_BEFORE_ISSUE}


def test_every_issue_has_a_message() -> None:
    issues = validate_document(invoice(total="1.00", due_date=date(2026, 1, 1)), today=TODAY)
    assert issues
    assert all(i.message for i in issues)
