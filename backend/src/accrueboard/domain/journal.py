"""Double-entry journal and posting templates (accrual basis, US sales tax).

Posting templates:
- Invoice:      Dr each line's account (landed cost)     Cr Accounts Payable (total)
- Receipt:      Dr each line's account (landed cost)     Cr Card Clearing or Bank (total)
- Credit note:  Dr Accounts Payable (total)              Cr each line's account (landed cost)

Purchase sales tax is not recoverable in the US, so it is part of the cost of what was bought.
Each line's *landed cost* is its amount, less its share of any discount, plus its share of
shipping, plus its share of tax (taxable lines only). Shares are allocated pro rata with
:func:`accrueboard.domain.money.allocate`, so the landed costs always sum to the document total.

Posted entries are never edited. A correction after posting is a reversing entry
(:func:`reverse_entry`) followed by a new entry.
"""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, computed_field, model_validator

from accrueboard.domain.accounts import AccountRole, AccountType, ChartOfAccounts
from accrueboard.domain.documents import DocumentType, ExtractedDocument, PaymentMethod
from accrueboard.domain.money import ZERO, Money, allocate


class PostingError(ValueError):
    """The document cannot be turned into a journal entry as given."""


class JournalLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_code: str
    debit: Money = ZERO
    credit: Money = ZERO
    memo: str = ""

    @model_validator(mode="after")
    def _one_side(self) -> "JournalLine":
        if self.debit < 0 or self.credit < 0:
            raise ValueError("debit and credit must be non-negative")
        if (self.debit > 0) == (self.credit > 0):
            raise ValueError("a journal line must have exactly one of debit or credit")
        return self


class JournalEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_date: date
    memo: str
    lines: tuple[JournalLine, ...]
    reverses: str | None = None
    """Identifier of the entry this one reverses, if it is a reversal."""

    @model_validator(mode="after")
    def _balanced(self) -> "JournalEntry":
        if len(self.lines) < 2:
            raise ValueError("a journal entry needs at least two lines")
        if not self.is_balanced:
            raise ValueError(
                f"entry does not balance: debits {self.total_debits} "
                f"!= credits {self.total_credits}"
            )
        return self

    @property
    def total_debits(self) -> Decimal:
        return sum((jl.debit for jl in self.lines), ZERO)

    @property
    def total_credits(self) -> Decimal:
        return sum((jl.credit for jl in self.lines), ZERO)

    @property
    def is_balanced(self) -> bool:
        return self.total_debits == self.total_credits

    @computed_field
    @property
    def total(self) -> Decimal:
        return self.total_debits


_CODABLE_TYPES = frozenset({AccountType.ASSET, AccountType.EXPENSE})
_PAYMENT_ROLE = {
    PaymentMethod.CARD: AccountRole.CARD_CLEARING,
    PaymentMethod.BANK: AccountRole.BANK,
}


def landed_costs(doc: ExtractedDocument) -> list[Decimal]:
    """Per-line cost including allocated discount, shipping and tax. Sums to the total exactly."""
    if doc.subtotal is None or doc.total is None or not doc.lines:
        raise PostingError("document needs line items, a subtotal and a total to post")
    reconciled = doc.subtotal - doc.discount + doc.shipping + doc.tax
    line_sum = sum((item.amount for item in doc.lines), ZERO)
    if line_sum != doc.subtotal or reconciled != doc.total:
        raise PostingError(
            "document does not reconcile (lines, subtotal and total disagree); validate first"
        )

    amounts = [item.amount for item in doc.lines]
    if line_sum == 0:
        raise PostingError("document has no value to post")
    discount_shares = allocate(doc.discount, amounts)
    shipping_shares = allocate(doc.shipping, amounts)
    taxable_amounts = [item.amount if item.taxable else ZERO for item in doc.lines]
    if doc.tax > 0:
        if sum(taxable_amounts, ZERO) == 0:
            raise PostingError("tax is charged but no line is taxable; validate first")
        tax_shares = allocate(doc.tax, taxable_amounts)
    else:
        tax_shares = [ZERO] * len(amounts)

    costs = [
        a - d + s + t
        for a, d, s, t in zip(amounts, discount_shares, shipping_shares, tax_shares, strict=True)
    ]
    if any(c < 0 for c in costs):
        raise PostingError("discount allocation produced a negative line cost")
    return costs


def _check_postable(
    doc: ExtractedDocument, line_accounts: Sequence[str], coa: ChartOfAccounts
) -> date:
    """Structural preconditions for posting; returns the entry date."""
    if doc.doc_type is DocumentType.OTHER:
        raise PostingError("cannot post a document of type 'other'")
    if doc.issue_date is None:
        raise PostingError("document needs an issue date to post")
    if len(line_accounts) != len(doc.lines):
        raise PostingError(
            f"need exactly one account per line ({len(doc.lines)} lines, "
            f"{len(line_accounts)} accounts)"
        )
    for code in line_accounts:
        if not coa.has(code):
            raise PostingError(f"unknown account code {code!r}")
        if coa.get(code).type not in _CODABLE_TYPES:
            raise PostingError(f"account {code} cannot be coded to (only asset/expense accounts)")
    return doc.issue_date


def build_entry(
    doc: ExtractedDocument,
    line_accounts: Sequence[str],
    coa: ChartOfAccounts,
    *,
    memo: str | None = None,
) -> JournalEntry:
    """Build the balanced journal entry for a validated document and its coded lines."""
    issue_date = _check_postable(doc, line_accounts, coa)
    costs = landed_costs(doc)
    by_account: dict[str, Decimal] = {}
    for code, cost in zip(line_accounts, costs, strict=True):
        if cost > 0:
            by_account[code] = by_account.get(code, ZERO) + cost
    total = sum(by_account.values(), ZERO)

    if doc.doc_type is DocumentType.RECEIPT:
        if doc.payment_method is None:
            raise PostingError("receipt has no payment method; cannot choose the credit account")
        counter_account = coa.role(_PAYMENT_ROLE[doc.payment_method])
    else:
        counter_account = coa.role(AccountRole.ACCOUNTS_PAYABLE)

    if doc.doc_type is DocumentType.CREDIT_NOTE:
        lines = (
            JournalLine(account_code=counter_account, debit=total),
            *(JournalLine(account_code=c, credit=v) for c, v in by_account.items()),
        )
    else:
        lines = (
            *(JournalLine(account_code=c, debit=v) for c, v in by_account.items()),
            JournalLine(account_code=counter_account, credit=total),
        )

    label = doc.doc_type.value.replace("_", " ")
    default_memo = f"{label} {doc.document_number or ''} {doc.vendor_name or ''}".strip()
    return JournalEntry(entry_date=issue_date, memo=memo or default_memo, lines=lines)


def reverse_entry(entry: JournalEntry, *, entry_date: date, original_id: str) -> JournalEntry:
    """Mirror an entry (debits become credits and vice versa), linked to the original."""
    return JournalEntry(
        entry_date=entry_date,
        memo=f"Reversal of {original_id}: {entry.memo}",
        reverses=original_id,
        lines=tuple(
            JournalLine(
                account_code=jl.account_code, debit=jl.credit, credit=jl.debit, memo=jl.memo
            )
            for jl in entry.lines
        ),
    )
