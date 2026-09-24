from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from accrueboard.domain.documents import DocumentType, ExtractedDocument, PaymentMethod
from accrueboard.domain.journal import (
    JournalEntry,
    JournalLine,
    PostingError,
    build_entry,
    landed_costs,
    reverse_entry,
)
from accrueboard.domain.money import CENT, money, round_money
from accrueboard.domain.validation import taxable_base, validate_document

from .factories import (
    AP,
    BANK,
    CARD,
    INVENTORY,
    OFFICE,
    SHIPPING,
    TODAY,
    chart,
    invoice,
    line,
)


def debits(entry: JournalEntry) -> dict[str, Decimal]:
    return {jl.account_code: jl.debit for jl in entry.lines if jl.debit}


def credits(entry: JournalEntry) -> dict[str, Decimal]:
    return {jl.account_code: jl.credit for jl in entry.lines if jl.credit}


# ------------------------------------------------------------------ journal primitives


def test_journal_line_needs_exactly_one_side() -> None:
    with pytest.raises(ValidationError):
        JournalLine(account_code=OFFICE, debit=money("1"), credit=money("1"))
    with pytest.raises(ValidationError):
        JournalLine(account_code=OFFICE)
    with pytest.raises(ValidationError):
        JournalLine(account_code=OFFICE, debit=money("-1"))


def test_unbalanced_entry_is_rejected() -> None:
    with pytest.raises(ValidationError, match="balance"):
        JournalEntry(
            entry_date=TODAY,
            memo="x",
            lines=(
                JournalLine(account_code=OFFICE, debit=money("10")),
                JournalLine(account_code=AP, credit=money("9.99")),
            ),
        )


# ------------------------------------------------------------------ landed costs


def test_landed_costs_allocate_shipping_and_tax() -> None:
    # Shipping 10.00 split 90:125 -> 4.19 / 5.81; tax 7.43 only on the taxable first line.
    assert landed_costs(invoice()) == [money("101.62"), money("130.81")]


def test_landed_costs_apply_discount() -> None:
    doc = invoice(discount="21.50", tax="6.68", total="210.18")
    costs = landed_costs(doc)
    assert sum(costs) == money("210.18")


def test_landed_costs_refuse_unreconciled_document() -> None:
    with pytest.raises(PostingError, match="reconcile"):
        landed_costs(invoice(total="999.00"))


# ------------------------------------------------------------------ posting templates


def test_invoice_debits_line_accounts_and_credits_payables() -> None:
    entry = build_entry(invoice(), [OFFICE, INVENTORY], chart())
    assert debits(entry) == {OFFICE: money("101.62"), INVENTORY: money("130.81")}
    assert credits(entry) == {AP: money("232.43")}
    assert entry.entry_date == date(2026, 6, 1)
    assert entry.is_balanced


def test_lines_on_the_same_account_are_combined() -> None:
    entry = build_entry(invoice(), [OFFICE, OFFICE], chart())
    assert debits(entry) == {OFFICE: money("232.43")}


@pytest.mark.parametrize(
    ("method", "account"), [(PaymentMethod.CARD, CARD), (PaymentMethod.BANK, BANK)]
)
def test_receipt_credits_the_payment_account(method: PaymentMethod, account: str) -> None:
    receipt = invoice(doc_type=DocumentType.RECEIPT, payment_method=method)
    entry = build_entry(receipt, [OFFICE, INVENTORY], chart())
    assert credits(entry) == {account: money("232.43")}


@pytest.mark.parametrize(
    ("method", "account"), [(PaymentMethod.CARD, CARD), (PaymentMethod.BANK, BANK)]
)
def test_invoice_already_charged_credits_the_payment_account(
    method: PaymentMethod, account: str
) -> None:
    # "Charged to the payment method on file": the bill is already paid, so crediting
    # payables would leave a liability that nothing ever settles.
    paid = invoice(payment_method=method)
    entry = build_entry(paid, [OFFICE, INVENTORY], chart())
    assert credits(entry) == {account: money("232.43")}
    as_receipt = invoice(doc_type=DocumentType.RECEIPT, payment_method=method)
    assert build_entry(as_receipt, [OFFICE, INVENTORY], chart()).lines == entry.lines


def test_receipt_without_payment_method_cannot_post() -> None:
    receipt = invoice(doc_type=DocumentType.RECEIPT)
    with pytest.raises(PostingError, match="payment method"):
        build_entry(receipt, [OFFICE, INVENTORY], chart())


def test_credit_note_reverses_direction() -> None:
    credit = invoice(doc_type=DocumentType.CREDIT_NOTE, referenced_document_number="INV-00123")
    entry = build_entry(credit, [OFFICE, INVENTORY], chart())
    assert debits(entry) == {AP: money("232.43")}
    assert credits(entry) == {OFFICE: money("101.62"), INVENTORY: money("130.81")}


def test_other_documents_cannot_post() -> None:
    with pytest.raises(PostingError, match="cannot post"):
        build_entry(ExtractedDocument(doc_type=DocumentType.OTHER), [], chart())


def test_account_count_must_match_lines() -> None:
    with pytest.raises(PostingError, match="account per line"):
        build_entry(invoice(), [OFFICE], chart())


def test_unknown_account_is_rejected() -> None:
    with pytest.raises(PostingError, match="unknown account"):
        build_entry(invoice(), [OFFICE, "9999"], chart())


def test_lines_cannot_be_coded_to_liability_accounts() -> None:
    with pytest.raises(PostingError, match="cannot be coded"):
        build_entry(invoice(), [OFFICE, AP], chart())


def test_zero_value_lines_are_dropped() -> None:
    doc = invoice(
        lines=(line(), line("Free sample", "1", "0.00", "0.00", taxable=False)),
        subtotal="90.00",
        shipping="0.00",
        tax="7.43",
        total="97.43",
    )
    entry = build_entry(doc, [OFFICE, INVENTORY], chart())
    assert debits(entry) == {OFFICE: money("97.43")}


# ------------------------------------------------------------------ reversal


def test_reversal_swaps_sides_and_links_original() -> None:
    original = build_entry(invoice(), [OFFICE, INVENTORY], chart())
    reversal = reverse_entry(original, entry_date=TODAY, original_id="je-1")
    assert reversal.reverses == "je-1"
    assert debits(reversal) == credits(original)
    assert credits(reversal) == debits(original)
    assert reversal.entry_date == TODAY


# ------------------------------------------------------------------ property: always balanced

amounts = st.integers(min_value=1, max_value=500_000).map(lambda c: Decimal(c) * CENT)
rates = st.sampled_from([None, Decimal("0.0625"), Decimal("0.0825"), Decimal("0.1025")])
accounts = st.sampled_from([OFFICE, INVENTORY, SHIPPING])


@st.composite
def valid_documents(draw: st.DrawFn) -> tuple[ExtractedDocument, list[str]]:
    n = draw(st.integers(min_value=1, max_value=8))
    items = []
    for i in range(n):
        qty = draw(st.integers(min_value=1, max_value=20))
        price = draw(amounts)
        items.append(
            line(f"item {i}", str(qty), str(price), str(qty * price), taxable=draw(st.booleans()))
        )
    subtotal = sum((it.amount for it in items), Decimal(0))
    discount = round_money(subtotal * draw(st.sampled_from([0, 5, 10, 25])) / 100)
    shipping = draw(st.sampled_from([Decimal(0), money("7.99"), money("25.00")]))
    rate = draw(rates)
    base_doc = invoice(
        lines=tuple(items), subtotal=str(subtotal), discount=str(discount), tax_rate=rate
    )
    base = taxable_base(base_doc, subtotal)
    tax = round_money(base * (rate or Decimal("0.07")))
    total = subtotal - discount + shipping + tax
    doc = base_doc.model_copy(update={"shipping": shipping, "tax": tax, "total": total})
    doc_type = draw(
        st.sampled_from([DocumentType.INVOICE, DocumentType.RECEIPT, DocumentType.CREDIT_NOTE])
    )
    doc = doc.model_copy(
        update={
            "doc_type": doc_type,
            "payment_method": PaymentMethod.CARD,
            "referenced_document_number": "INV-1",
        }
    )
    return doc, [draw(accounts) for _ in items]


@settings(max_examples=300)
@given(case=valid_documents())
def test_every_valid_document_posts_a_balanced_entry(
    case: tuple[ExtractedDocument, list[str]],
) -> None:
    doc, codes = case
    assert validate_document(doc, today=TODAY) == ()
    entry = build_entry(doc, codes, chart())
    assert entry.is_balanced
    assert entry.total == doc.total
    assert all(jl.debit >= 0 and jl.credit >= 0 for jl in entry.lines)
    assert sum(landed_costs(doc)) == doc.total
