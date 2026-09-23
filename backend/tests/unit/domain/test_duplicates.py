from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from accrueboard.domain.documents import DocumentType
from accrueboard.domain.duplicates import (
    DuplicateKind,
    Fingerprint,
    find_duplicates,
    normalize_document_number,
    normalize_vendor,
)

from .factories import invoice

# ------------------------------------------------------------------ normalization


@pytest.mark.parametrize(
    "raw",
    ["INV-00123", "inv 123", "#123", "Invoice No. 0123", "INV#000123", "  123 ", "INVOICE-123"],
)
def test_document_number_variants_normalize_equal(raw: str) -> None:
    assert normalize_document_number(raw) == "123"


def test_document_number_keeps_meaningful_letters() -> None:
    # Only truly leading zeros go; zeros after a letter are part of the number.
    assert normalize_document_number("A-0042") == "A0042"
    assert normalize_document_number("INV-A-0042") == "A0042"
    assert normalize_document_number("NOVA-7") == "NOVA7"  # 'NO' is only stripped before a digit


@pytest.mark.parametrize("raw", [None, "", "  ", "#", "INV-"])
def test_empty_document_numbers_normalize_to_none(raw: str | None) -> None:
    assert normalize_document_number(raw) is None


def test_all_zero_number_is_zero() -> None:
    assert normalize_document_number("000") == "0"


@given(st.text(alphabet="INVOICE0123456789-# ABno.", max_size=20))
def test_document_number_normalization_is_idempotent(raw: str) -> None:
    once = normalize_document_number(raw)
    assert normalize_document_number(once) == once


@pytest.mark.parametrize(
    "raw",
    [
        "Acme Office Supply Inc.",
        "ACME OFFICE SUPPLY, INC",
        "The Acme Office Supply LLC",
        "acme office supply",
    ],
)
def test_vendor_variants_normalize_equal(raw: str) -> None:
    assert normalize_vendor(raw) == "acme office supply"


def test_vendor_ampersand() -> None:
    assert normalize_vendor("Smith & Sons Co.") == normalize_vendor("Smith and Sons")


def test_vendor_blank_is_none() -> None:
    assert normalize_vendor("  ") is None
    assert normalize_vendor(None) is None


# ------------------------------------------------------------------ detection


def fp(doc_id: str, sha: str = "", **overrides: object) -> Fingerprint:
    return Fingerprint.from_document(doc_id, sha or f"sha-{doc_id}", invoice(**overrides))


def kinds(candidate: Fingerprint, *history: Fingerprint) -> list[tuple[DuplicateKind, str]]:
    return [(m.kind, m.other_doc_id) for m in find_duplicates(candidate, history)]


def test_exact_file_duplicate() -> None:
    assert kinds(fp("b", sha="same"), fp("a", sha="same")) == [(DuplicateKind.EXACT_FILE, "a")]


def test_same_vendor_and_number_in_different_format() -> None:
    original = fp("a", document_number="INV-00123")
    resent = fp("b", document_number="123", vendor_name="ACME OFFICE SUPPLY, INC")
    assert kinds(resent, original) == [(DuplicateKind.SAME_NUMBER, "a")]


def test_same_number_from_different_vendor_is_not_a_duplicate() -> None:
    assert kinds(fp("b", vendor_name="Globex Corp"), fp("a")) == []


def test_document_is_not_its_own_duplicate() -> None:
    assert kinds(fp("a"), fp("a")) == []


def test_credit_note_is_not_a_duplicate_of_its_invoice() -> None:
    credit = fp(
        "cn",
        doc_type=DocumentType.CREDIT_NOTE,
        document_number="123",  # same number series as the invoice by coincidence
        referenced_document_number="INV-00123",
    )
    assert kinds(credit, fp("inv")) == []


def test_second_credit_note_against_same_invoice() -> None:
    first = fp(
        "cn1",
        doc_type=DocumentType.CREDIT_NOTE,
        document_number="CN-1",
        referenced_document_number="INV-00123",
    )
    second = fp(
        "cn2",
        doc_type=DocumentType.CREDIT_NOTE,
        document_number="CN-2",
        referenced_document_number="00123",
    )
    assert kinds(second, first) == [(DuplicateKind.DUPLICATE_CREDIT_NOTE, "cn1")]


def test_near_duplicate_same_total_close_dates_different_number() -> None:
    original = fp("a", document_number="INV-00123", issue_date=date(2026, 6, 1))
    rekeyed = fp("b", document_number="INV-00999", issue_date=date(2026, 6, 8))
    matches = find_duplicates(rekeyed, [original])
    assert [(m.kind, m.is_hard) for m in matches] == [(DuplicateKind.NEAR_DUPLICATE, False)]


def test_near_duplicate_when_number_missing() -> None:
    original = fp("a")
    no_number = fp("b", doc_type=DocumentType.RECEIPT, document_number=None)
    assert kinds(no_number, original) == [(DuplicateKind.NEAR_DUPLICATE, "a")]


def test_monthly_recurring_charge_is_not_a_near_duplicate() -> None:
    may = fp("may", document_number="SUB-5", issue_date=date(2026, 5, 1), due_date=None)
    june = fp("jun", document_number="SUB-6", issue_date=date(2026, 6, 1), due_date=None)
    assert kinds(june, may) == []


def test_hard_kinds() -> None:
    hard = {k for k in DuplicateKind if k.is_hard}
    assert hard == {
        DuplicateKind.EXACT_FILE,
        DuplicateKind.SAME_NUMBER,
        DuplicateKind.DUPLICATE_CREDIT_NOTE,
    }
