"""Deterministic synthetic-data generator.

Ground truth comes first: every document is built as structured data (what is printed on it,
how each line should be coded, which anomaly it carries), and files are rendered from that
data afterwards. The same seed always yields identical records.

Flow:
1. Schedule recurring vendor documents across history, validation and test periods.
2. Build each bill (lines, shipping, discount, sales tax, totals) and occasional credit notes.
3. Inject the anomalies and hard negatives from ``specs/anomalies.yaml`` into the evaluation
   splits (history stays clean; it seeds the knowledge store).
4. Label properties that occur naturally (e.g. an unusually large one-off purchase).
5. Order each split by arrival time and assign stable document ids.
"""

import calendar
import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from accrueboard.datagen.records import AnomalyLabel, FileFormat, GroundTruth, HardNegativeLabel
from accrueboard.datagen.spec import (
    EVAL_SPLITS,
    AnomalyCatalog,
    CatalogItem,
    ClientSpec,
    Split,
    VendorSpec,
)
from accrueboard.domain.documents import DocumentType, ExtractedDocument, LineItem, PaymentMethod
from accrueboard.domain.duplicates import normalize_document_number
from accrueboard.domain.money import CENT, ZERO, round_money
from accrueboard.domain.routing import Rule
from accrueboard.domain.validation import taxable_base

GENERATOR_VERSION = "2"
INVOICE_LAYOUTS = ("classic", "modern", "compact", "boxed", "ledger", "minimal")
PNG_RECEIPT_SHARE = 0.6
PRINTED_RATE_SHARE = 0.7
ALIAS_SHARE = 0.5
"""Share of lines printed with one of the item's alternative wordings."""
SEASONAL_MONTHS = frozenset({10, 11, 12})
SEASONAL_FACTOR = Decimal("1.35")


# ---------------------------------------------------------------------------- drafts


@dataclass
class Draft:
    """A document under construction. Converted to GroundTruth once ids are final."""

    key: int
    vendor: VendorSpec
    doc: ExtractedDocument
    accounts: tuple[str, ...]
    received_at: datetime
    split: Split
    layout: str
    file_format: FileFormat
    anomalies: list[tuple[str, Rule, int | None, bool]] = field(default_factory=list)
    negatives: list[tuple[str, tuple[Rule, ...], int | None]] = field(default_factory=list)
    copy_of: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_bill(self) -> bool:
        return self.doc.doc_type in (DocumentType.INVOICE, DocumentType.RECEIPT)

    @property
    def is_clean(self) -> bool:
        return not self.anomalies and not self.negatives and self.copy_of is None

    @property
    def total(self) -> Decimal:
        assert self.doc.total is not None
        return self.doc.total


@dataclass(frozen=True)
class BillLine:
    item: CatalogItem
    description: str
    quantity: Decimal
    unit_price: Decimal


def _payment_method(vendor: VendorSpec, kind: DocumentType) -> PaymentMethod | None:
    """How a document says it was paid. A receipt is paid by the vendor's usual method; an
    invoice from a vendor without payment terms says it was "charged to the payment method on
    file" (see render.py), so it is already paid by card. Other invoices are unpaid bills."""
    if kind is DocumentType.RECEIPT:
        return vendor.payment_method
    if kind is DocumentType.INVOICE and vendor.terms_days == 0:
        return vendor.payment_method or PaymentMethod.CARD
    return None


class Generator:
    def __init__(self, spec: ClientSpec, catalog: AnomalyCatalog, seed: int) -> None:
        self.spec = spec
        self.catalog = catalog
        self.seed = seed
        self.rng = random.Random(seed)
        self._keys = 0
        self._numbers: dict[str, int] = {}
        self._credit_numbers: dict[str, int] = {}
        self._po = 4100
        self.drafts: list[Draft] = []

    # ------------------------------------------------------------------ public API

    def run(self) -> list[GroundTruth]:
        for vendor in self.spec.vendors:
            for issue in self._schedule(vendor):
                self._add(self.make_bill(vendor, issue))
        self._make_credit_notes()
        for split in EVAL_SPLITS:
            _Injector(self, split).run()
        self._label_natural_properties()
        return self._finalize()

    # ------------------------------------------------------------------ helpers

    def next_key(self) -> int:
        self._keys += 1
        return self._keys

    def _add(self, draft: Draft) -> Draft:
        self.drafts.append(draft)
        return draft

    def split_for(self, day: date) -> Split | None:
        for split, period in self.spec.periods.items():
            if period.start <= day <= period.end:
                return split
        return None

    def received_for(self, issue: date, *, min_days: int = 0, max_days: int = 4) -> datetime:
        day = issue + timedelta(days=self.rng.randint(min_days, max_days))
        # 08:00-18:00 US Central is roughly 13:00-23:00 UTC.
        moment = time(hour=self.rng.randint(13, 22), minute=self.rng.randint(0, 59))
        return datetime.combine(day, moment, tzinfo=UTC)

    def next_number(self, vendor: VendorSpec) -> str:
        if vendor.id not in self._numbers:
            self._numbers[vendor.id] = self.rng.randint(120, 4800)
        self._numbers[vendor.id] += self.rng.randint(1, 9)
        return vendor.number_format.format(n=self._numbers[vendor.id])

    def bump_po(self) -> str:
        self._po += self.rng.randint(1, 4)
        return self.current_po

    @property
    def current_po(self) -> str:
        return f"PO-{self._po}"

    def next_credit_number(self, vendor: VendorSpec) -> str:
        self._credit_numbers[vendor.id] = self._credit_numbers.get(vendor.id, 0) + 1
        return f"CM-{self._credit_numbers[vendor.id] + 100:04d}"

    def _schedule(self, vendor: VendorSpec) -> list[date]:
        assert vendor.schedule is not None
        start = min(p.start for p in self.spec.periods.values())
        end = max(p.end for p in self.spec.periods.values())
        dates: list[date] = []
        if vendor.schedule.monthly_day is not None:
            year, month = start.year, start.month
            while True:
                last = calendar.monthrange(year, month)[1]
                day = date(year, month, min(vendor.schedule.monthly_day, last))
                if day > end:
                    break
                if day >= start:
                    dates.append(day)
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            return dates
        every = vendor.schedule.every_days
        assert every is not None
        jitter = vendor.schedule.jitter_days
        day = start + timedelta(days=self.rng.randint(0, every - 1))
        while day <= end:
            dates.append(day)
            day += timedelta(days=max(1, every + self.rng.randint(-jitter, jitter)))
        return dates

    # ------------------------------------------------------------------ bills

    def pick_lines(self, vendor: VendorSpec, issue: date) -> list[BillLine]:
        rng = self.rng
        always = [i for i in vendor.catalog if i.always]
        common = [i for i in vendor.catalog if not i.always and not i.rare]
        rare = [i for i in vendor.catalog if i.rare]
        count = rng.randint(*vendor.lines)
        chosen = list(always)
        pool = list(common)
        rng.shuffle(pool)
        while len(chosen) < count and pool:
            chosen.append(pool.pop())
        if rare and rng.random() < 0.08:
            chosen[-1] = rng.choice(rare)
        factor = SEASONAL_FACTOR if vendor.seasonal and issue.month in SEASONAL_MONTHS else 1
        lines: list[BillLine] = []
        for item in chosen:
            qty = Decimal(rng.randint(*item.qty))
            if factor != 1 and item.qty[1] > 1:
                qty = (qty * factor).to_integral_value()
            lo, hi = (int(p / CENT) for p in item.price)
            price = item.price[0] if item.fixed else Decimal(rng.randint(lo, hi)) * CENT
            wording = item.description
            if item.aliases and rng.random() < ALIAS_SHARE:
                wording = rng.choice(item.aliases)
            lines.append(BillLine(item=item, description=wording, quantity=qty, unit_price=price))
        return lines

    def build_doc(
        self,
        vendor: VendorSpec,
        issue: date,
        lines: list[BillLine],
        *,
        doc_type: DocumentType | None = None,
        number: str | None = None,
        discount_pct: int | None = None,
        shipping: Decimal | None = None,
        force_taxable: bool = False,
        print_rate: bool | None = None,
        po_number: str | None = None,
        referenced: str | None = None,
    ) -> tuple[ExtractedDocument, tuple[str, ...]]:
        """Assemble a consistent document (every total reconciles) from chosen lines."""
        rng = self.rng
        kind = doc_type or DocumentType(vendor.doc_type)
        items = tuple(
            LineItem(
                description=bl.description,
                quantity=bl.quantity,
                unit_price=bl.unit_price,
                amount=round_money(bl.quantity * bl.unit_price),
                taxable=bl.item.taxable or force_taxable,
            )
            for bl in lines
        )
        subtotal = sum((i.amount for i in items), ZERO)
        if discount_pct is None and vendor.discount and rng.random() < vendor.discount.prob:
            discount_pct = rng.randint(vendor.discount.pct_min, vendor.discount.pct_max)
        discount = round_money(subtotal * discount_pct / 100) if discount_pct else ZERO
        if shipping is None:
            shipping = ZERO
            if vendor.shipping and rng.random() < vendor.shipping.prob:
                lo, hi = int(vendor.shipping.min / CENT), int(vendor.shipping.max / CENT)
                shipping = Decimal(rng.randint(lo, hi)) * CENT
        rate = self.spec.tax_rates[vendor.state]
        draft = ExtractedDocument(doc_type=kind, lines=items, subtotal=subtotal, discount=discount)
        base = taxable_base(draft, subtotal)
        tax = round_money(base * rate) if base > 0 and rate > 0 else ZERO
        if print_rate is None:
            print_rate = rng.random() < PRINTED_RATE_SHARE
        terms = vendor.terms_days if kind is DocumentType.INVOICE else 0
        doc = ExtractedDocument(
            doc_type=kind,
            vendor_name=vendor.name,
            vendor_state=vendor.state,
            document_number=number or self.next_number(vendor),
            issue_date=issue,
            due_date=issue + timedelta(days=terms) if terms > 0 else None,
            po_number=po_number,
            lines=items,
            subtotal=subtotal,
            discount=discount,
            shipping=shipping,
            tax_rate=rate if tax > 0 and print_rate else None,
            tax=tax,
            total=subtotal - discount + shipping + tax,
            payment_method=_payment_method(vendor, kind),
            referenced_document_number=referenced,
        )
        return doc, tuple(bl.item.account for bl in lines)

    def make_bill(self, vendor: VendorSpec, issue: date, split: Split | None = None) -> Draft:
        lines = self.pick_lines(vendor, issue)
        po = None
        if vendor.is_inventory_supplier and self.rng.random() < 0.6:
            po = self.bump_po()
        doc, accounts = self.build_doc(vendor, issue, lines, po_number=po)
        return self.draft_for(vendor, doc, accounts, issue, split=split)

    def draft_for(
        self,
        vendor: VendorSpec,
        doc: ExtractedDocument,
        accounts: tuple[str, ...],
        issue: date,
        *,
        split: Split | None = None,
        received_at: datetime | None = None,
        layout: str | None = None,
    ) -> Draft:
        resolved = split or self.split_for(issue)
        assert resolved is not None, f"{issue} is outside every period"
        is_receipt = doc.doc_type is DocumentType.RECEIPT
        file_format: FileFormat = (
            "png" if is_receipt and self.rng.random() < PNG_RECEIPT_SHARE else "pdf"
        )
        return Draft(
            key=self.next_key(),
            vendor=vendor,
            doc=doc,
            accounts=accounts,
            received_at=received_at or self.received_for(issue),
            split=resolved,
            layout=layout or vendor.layout,
            file_format=file_format,
        )

    def credit_note_for(
        self, invoice: Draft, *, days_later: int | None = None, split: Split | None = None
    ) -> Draft:
        """A credit note returning part of one line of an earlier invoice."""
        rng = self.rng
        vendor = invoice.vendor
        index = rng.randrange(len(invoice.doc.lines))
        original = invoice.doc.lines[index]
        qty = Decimal(rng.randint(1, max(1, min(int(original.quantity), 6))))
        catalog_item = next(
            i for i in vendor.catalog if original.description in (i.description, *i.aliases)
        )
        assert invoice.doc.issue_date is not None
        issue = invoice.doc.issue_date + timedelta(days=days_later or rng.randint(3, 20))
        doc, accounts = self.build_doc(
            vendor,
            issue,
            [
                BillLine(
                    item=catalog_item,
                    description=original.description,
                    quantity=qty,
                    unit_price=original.unit_price,
                )
            ],
            doc_type=DocumentType.CREDIT_NOTE,
            number=self.next_credit_number(vendor),
            discount_pct=0,
            shipping=ZERO,
            referenced=invoice.doc.document_number,
        )
        doc = doc.model_copy(
            update={"lines": (doc.lines[0].model_copy(update={"taxable": original.taxable}),)}
        )
        doc = _recompute(doc, self.spec.tax_rates[vendor.state], print_rate=False)
        return self.draft_for(vendor, doc, accounts, issue, split=split)

    def _make_credit_notes(self) -> None:
        for draft in list(self.drafts):
            vendor = draft.vendor
            if (
                draft.doc.doc_type is DocumentType.INVOICE
                and vendor.credit_note_prob > 0
                and self.rng.random() < vendor.credit_note_prob
            ):
                assert draft.doc.issue_date is not None
                if self.split_for(draft.doc.issue_date + timedelta(days=20)) is None:
                    continue
                self._add(self.credit_note_for(draft))

    # ------------------------------------------------------------------ labelling

    def history_totals(self, vendor_id: str) -> list[Decimal]:
        return [
            d.total
            for d in self.drafts
            if d.split is Split.HISTORY and d.vendor.id == vendor_id and d.is_bill
        ]

    def history_median(self, vendor_id: str) -> Decimal | None:
        totals = self.history_totals(vendor_id)
        if len(totals) < 5:
            return None
        return Decimal(str(statistics.median(totals)))

    def _label_natural_properties(self) -> None:
        cap = self.spec.materiality_cap
        credited: set[tuple[str, str | None]] = set()
        for draft in sorted(self.drafts, key=lambda d: (d.received_at, d.key)):
            if draft.doc.doc_type is not DocumentType.CREDIT_NOTE:
                continue
            ref = (draft.vendor.id, normalize_document_number(draft.doc.referenced_document_number))
            first = ref not in credited
            credited.add(ref)
            if first and draft.split is not Split.HISTORY and not draft.negatives:
                spec = self.catalog.hard_negative("credit_note_for_invoice")
                invoice = next(
                    (
                        d
                        for d in self.drafts
                        if d.vendor.id == draft.vendor.id
                        and d.doc.doc_type is DocumentType.INVOICE
                        and d.doc.document_number == draft.doc.referenced_document_number
                    ),
                    None,
                )
                draft.negatives.append(
                    (spec.id, spec.must_not_fire, invoice.key if invoice else None)
                )
        for draft in self.drafts:
            if draft.split is Split.HISTORY or not draft.is_bill:
                continue
            labelled = {a[0] for a in draft.anomalies}
            median = self.history_median(draft.vendor.id)
            if (
                "amount_outlier" not in labelled
                and median is not None
                and draft.total >= 8 * median
            ):
                draft.anomalies.append(("amount_outlier", Rule.AMOUNT_OUTLIER, None, False))
            if "over_materiality" not in labelled and draft.total > cap:
                draft.anomalies.append(("over_materiality", Rule.OVER_MATERIALITY, None, False))
            fixed_only = all(item.fixed for item in draft.vendor.catalog)
            if fixed_only and draft.copy_of is None and not draft.anomalies:
                spec = self.catalog.hard_negative("recurring_charge")
                draft.negatives.append((spec.id, spec.must_not_fire, None))

    # ------------------------------------------------------------------ finalize

    def _number_credit_notes(self) -> None:
        """Give each vendor's credit memos sequential numbers in issue order."""
        notes = sorted(
            (d for d in self.drafts if d.doc.doc_type is DocumentType.CREDIT_NOTE),
            key=lambda d: (d.doc.issue_date, d.received_at, d.key),
        )
        seq: dict[str, int] = {}
        for draft in notes:
            seq[draft.vendor.id] = seq.get(draft.vendor.id, 100) + 1
            number = f"CM-{seq[draft.vendor.id]:04d}"
            draft.doc = draft.doc.model_copy(update={"document_number": number})

    def _finalize(self) -> list[GroundTruth]:
        self._number_credit_notes()
        ids: dict[int, str] = {}
        ordered: list[Draft] = []
        for split in Split:
            in_split = sorted(
                (d for d in self.drafts if d.split is split), key=lambda d: (d.received_at, d.key)
            )
            for i, draft in enumerate(in_split, start=1):
                ids[draft.key] = f"{self.spec.id}-{split.value}-{i:04d}"
            ordered += in_split

        records: list[GroundTruth] = []
        for draft in ordered:
            doc_id = ids[draft.key]
            rendered = draft.split is not Split.HISTORY
            file = (
                f"documents/{draft.split.value}/{doc_id}.{draft.file_format}" if rendered else None
            )
            records.append(
                GroundTruth(
                    doc_id=doc_id,
                    client_id=self.spec.id,
                    split=draft.split,
                    vendor_id=draft.vendor.id,
                    received_at=draft.received_at,
                    layout=draft.layout,
                    file_format=draft.file_format,
                    file=file,
                    copy_of=ids[draft.copy_of] if draft.copy_of is not None else None,
                    document=draft.doc,
                    line_accounts=draft.accounts,
                    anomalies=tuple(
                        AnomalyLabel(
                            type=kind,
                            expected_rule=rule,
                            source_doc_id=ids[src] if src is not None else None,
                            injected=injected,
                        )
                        for kind, rule, src, injected in draft.anomalies
                    ),
                    hard_negatives=tuple(
                        HardNegativeLabel(
                            type=kind,
                            must_not_fire=rules,
                            related_doc_id=ids[rel] if rel is not None else None,
                        )
                        for kind, rules, rel in draft.negatives
                    ),
                    extra=draft.extra,
                )
            )
        return records


def _recompute(doc: ExtractedDocument, rate: Decimal, *, print_rate: bool) -> ExtractedDocument:
    """Recompute subtotal, tax and total after changing lines or taxability."""
    subtotal = sum((i.amount for i in doc.lines), ZERO)
    staged = doc.model_copy(update={"subtotal": subtotal})
    base = taxable_base(staged, subtotal)
    tax = round_money(base * rate) if base > 0 and rate > 0 else ZERO
    return staged.model_copy(
        update={
            "tax": tax,
            "tax_rate": rate if tax > 0 and print_rate else None,
            "total": subtotal - doc.discount + doc.shipping + tax,
        }
    )


def _scale_lines(doc: ExtractedDocument, factor: Decimal) -> ExtractedDocument:
    """Multiply quantities (or, for single-unit lines, unit prices) by ``factor``."""
    lines = []
    for item in doc.lines:
        if item.quantity > 1:
            qty = max(Decimal(1), (item.quantity * factor).to_integral_value())
            lines.append(
                item.model_copy(
                    update={"quantity": qty, "amount": round_money(qty * item.unit_price)}
                )
            )
        else:
            price = round_money(item.unit_price * factor)
            lines.append(
                item.model_copy(
                    update={"unit_price": price, "amount": round_money(item.quantity * price)}
                )
            )
    subtotal = sum((i.amount for i in lines), ZERO)
    pct = doc.discount / doc.subtotal if doc.subtotal else ZERO
    return doc.model_copy(update={"lines": tuple(lines), "discount": round_money(subtotal * pct)})


def renumber(number: str, rng: random.Random) -> str:
    """A differently formatted spelling of the same document number."""
    key = normalize_document_number(number)
    candidates = [
        key or "",
        f"#{number}",
        number.lower().replace("-", " "),
        f"No. {key}",
        f"INV-{key}",
    ]
    valid = [c for c in candidates if c and c != number and normalize_document_number(c) == key]
    return rng.choice(valid)


class _Injector:
    """Adds the anomalies and hard negatives of one evaluation split."""

    def __init__(self, gen: Generator, split: Split) -> None:
        self.gen = gen
        self.rng = gen.rng
        self.split = split
        period = gen.spec.periods[split]
        self.start, self.end = period.start, period.end
        self.used: set[int] = set()

    def run(self) -> None:
        catalog = self.gen.catalog
        handlers = {
            "exact_file_duplicate": self.exact_file_duplicate,
            "renumbered_duplicate": self.renumbered_duplicate,
            "near_duplicate": self.near_duplicate,
            "amount_outlier": self.amount_outlier,
            "arithmetic_error": self.arithmetic_error,
            "tax_on_resale_inventory": self.tax_on_resale_inventory,
            "over_materiality": self.over_materiality,
            "duplicate_credit_note": self.duplicate_credit_note,
            "first_time_vendor": self.first_time_vendor,
            "unsupported_document": self.unsupported_document,
            "split_shipment": self.split_shipment,
            "same_number_other_vendor": self.same_number_other_vendor,
            "seasonal_restock": self.seasonal_restock,
        }
        for spec in catalog.anomalies:
            for _ in range(spec.counts.get(self.split, 0)):
                handlers[spec.id]()
        for neg in catalog.hard_negatives:
            if neg.counts != "natural":
                for _ in range(neg.counts.get(self.split, 0)):
                    handlers[neg.id]()

    # ------------------------------------------------------------------ selection

    def clean_bills(self, *, pdf_only: bool = False, **filters: Any) -> list[Draft]:
        out = [
            d
            for d in self.gen.drafts
            if d.split is self.split
            and d.is_bill
            and d.is_clean
            and d.key not in self.used
            and (not pdf_only or d.file_format == "pdf")
        ]
        if filters.get("inventory"):
            out = [d for d in out if d.vendor.is_inventory_supplier]
        if filters.get("invoice"):
            out = [d for d in out if d.doc.doc_type is DocumentType.INVOICE]
        return out

    def take(self, pool: list[Draft]) -> Draft:
        if not pool:
            raise RuntimeError(f"not enough candidate documents in {self.split.value}")
        draft = self.rng.choice(pool)
        self.used.add(draft.key)
        return draft

    def label(self, draft: Draft, anomaly_id: str, source: Draft | None = None) -> None:
        rule = self.gen.catalog.anomaly(anomaly_id).expected_rule
        draft.anomalies.append((anomaly_id, rule, source.key if source else None, True))

    def negative(self, draft: Draft, negative_id: str, related: Draft | None = None) -> None:
        rules = self.gen.catalog.hard_negative(negative_id).must_not_fire
        draft.negatives.append((negative_id, rules, related.key if related else None))

    def later(self, moment: datetime, lo: int, hi: int) -> datetime:
        limit = datetime.combine(self.end, time(23, 0), tzinfo=UTC)
        return min(moment + timedelta(days=self.rng.randint(lo, hi), hours=1), limit)

    def replace_doc(self, draft: Draft, doc: ExtractedDocument) -> None:
        draft.doc = doc

    # ------------------------------------------------------------------ anomalies

    def exact_file_duplicate(self) -> None:
        source = self.take(self.clean_bills(pdf_only=True))
        copy = replace(
            source,
            key=self.gen.next_key(),
            received_at=self.later(source.received_at, 1, 12),
            anomalies=[],
            negatives=[],
            copy_of=source.key,
        )
        self.label(copy, "exact_file_duplicate", source)
        self.gen.drafts.append(copy)

    def renumbered_duplicate(self) -> None:
        pool = [d for d in self.clean_bills(invoice=True) if d.doc.document_number]
        history = [
            d
            for d in self.gen.drafts
            if d.split is Split.HISTORY
            and d.doc.doc_type is DocumentType.INVOICE
            and d.vendor.schedule is not None
            and d.doc.issue_date is not None
            and (self.start - d.doc.issue_date).days < 120
        ]
        from_history = history and self.rng.random() < 0.5
        source = self.rng.choice(history) if from_history else self.take(pool)
        assert source.doc.document_number is not None
        number = renumber(source.doc.document_number, self.rng)
        layouts = [lay for lay in INVOICE_LAYOUTS if lay != source.layout]
        if from_history:
            day = self.start + timedelta(days=self.rng.randint(0, 20))
            received = self.gen.received_for(day)
        else:
            received = self.later(source.received_at, 5, 30)
        copy = Draft(
            key=self.gen.next_key(),
            vendor=source.vendor,
            doc=source.doc.model_copy(update={"document_number": number}),
            accounts=source.accounts,
            received_at=received,
            split=self.split,
            layout=self.rng.choice(layouts),
            file_format="pdf",
        )
        self.label(copy, "renumbered_duplicate", source)
        self.gen.drafts.append(copy)

    def near_duplicate(self) -> None:
        # Only sources with room for the full 3-9 day gap inside the period.
        source = self.take(
            [
                d
                for d in self.clean_bills(invoice=True)
                if d.doc.issue_date is not None and (self.end - d.doc.issue_date).days >= 9
            ]
        )
        assert source.doc.issue_date is not None
        issue = source.doc.issue_date + timedelta(days=self.rng.randint(3, 9))
        vendor = source.vendor
        doc = source.doc.model_copy(
            update={
                "document_number": self.gen.next_number(vendor),
                "issue_date": issue,
                "due_date": issue + timedelta(days=vendor.terms_days)
                if source.doc.due_date
                else None,
            }
        )
        copy = self.gen.draft_for(vendor, doc, source.accounts, issue, split=self.split)
        copy.received_at = max(copy.received_at, source.received_at + timedelta(hours=2))
        self.label(copy, "near_duplicate", source)
        self.gen.drafts.append(copy)

    def amount_outlier(self) -> None:
        cap = self.gen.spec.materiality_cap
        pool = []
        for d in self.clean_bills():
            median = self.gen.history_median(d.vendor.id)
            if median is not None and median * 15 < cap * Decimal("0.9"):
                pool.append(d)
        draft = self.take(pool)
        median = self.gen.history_median(draft.vendor.id)
        assert median is not None
        target = median * Decimal(self.rng.randint(80, 150)) / 10
        rate = self.gen.spec.tax_rates[draft.vendor.state]
        doc = draft.doc
        for _ in range(20):
            assert draft.doc.subtotal is not None
            factor = max(Decimal(2), (target / draft.doc.subtotal).quantize(Decimal("0.01")))
            doc = _recompute(
                _scale_lines(draft.doc, factor), rate, print_rate=draft.doc.tax_rate is not None
            )
            assert doc.total is not None
            if 8 * median <= doc.total < cap:
                break
            target = target * Decimal("1.1") if doc.total < 8 * median else target * Decimal("0.9")
        assert doc.total is not None and 8 * median <= doc.total < cap
        self.replace_doc(draft, doc)
        self.label(draft, "amount_outlier")

    def arithmetic_error(self) -> None:
        draft = self.take(self.clean_bills())
        doc = draft.doc
        assert doc.total is not None
        delta = Decimal(self.rng.randint(5, 95)) + Decimal(self.rng.choice([0, 50])) * CENT
        if self.rng.random() < 0.5:
            i = self.rng.randrange(len(doc.lines))
            bad = doc.lines[i].model_copy(update={"amount": doc.lines[i].amount + delta})
            lines = (*doc.lines[:i], bad, *doc.lines[i + 1 :])
            subtotal = sum((line.amount for line in lines), ZERO)
            doc = doc.model_copy(
                update={
                    "lines": lines,
                    "subtotal": subtotal,
                    "total": subtotal - doc.discount + doc.shipping + doc.tax,
                }
            )
        else:
            sign = 1 if self.rng.random() < 0.5 else -1
            doc = doc.model_copy(update={"total": doc.total + sign * delta})
        self.replace_doc(draft, doc)
        self.label(draft, "arithmetic_error")

    def tax_on_resale_inventory(self) -> None:
        pool = [
            d
            for d in self.clean_bills(inventory=True, invoice=True)
            if self.gen.spec.tax_rates[d.vendor.state] > 0 and all(a == "1300" for a in d.accounts)
        ]
        draft = self.take(pool)
        rate = self.gen.spec.tax_rates[draft.vendor.state]
        lines = tuple(i.model_copy(update={"taxable": True}) for i in draft.doc.lines)
        doc = _recompute(
            draft.doc.model_copy(update={"lines": lines}),
            rate,
            print_rate=self.rng.random() < PRINTED_RATE_SHARE,
        )
        self.replace_doc(draft, doc)
        self.label(draft, "tax_on_resale_inventory")

    def over_materiality(self) -> None:
        draft = self.take(self.clean_bills(inventory=True, invoice=True))
        cap = self.gen.spec.materiality_cap
        target = Decimal(self.rng.randint(10_500, 18_000))
        rate = self.gen.spec.tax_rates[draft.vendor.state]
        doc = draft.doc
        for _ in range(20):
            assert doc.subtotal is not None and draft.doc.subtotal is not None
            factor = (target / draft.doc.subtotal).quantize(Decimal("0.01"))
            doc = _recompute(
                _scale_lines(draft.doc, factor), rate, print_rate=draft.doc.tax_rate is not None
            )
            assert doc.total is not None
            if doc.total > cap:
                break
            target *= Decimal("1.1")
        assert doc.total is not None and doc.total > cap
        self.replace_doc(draft, doc)
        self.label(draft, "over_materiality")

    def duplicate_credit_note(self) -> None:
        credited = {
            (d.vendor.id, d.doc.referenced_document_number)
            for d in self.gen.drafts
            if d.doc.doc_type is DocumentType.CREDIT_NOTE
        }
        invoice = self.take(
            [
                d
                for d in self.clean_bills(inventory=True, invoice=True)
                if d.doc.issue_date
                and (self.end - d.doc.issue_date).days > 30
                and (d.vendor.id, d.doc.document_number) not in credited
            ]
        )
        first = self.gen.credit_note_for(
            invoice, days_later=self.rng.randint(3, 10), split=self.split
        )
        self.negative(first, "credit_note_for_invoice", invoice)
        self.gen.drafts.append(first)
        second = self.gen.credit_note_for(
            invoice, days_later=self.rng.randint(12, 25), split=self.split
        )
        second.received_at = max(second.received_at, first.received_at + timedelta(hours=3))
        self.label(second, "duplicate_credit_note", first)
        self.gen.drafts.append(second)

    def first_time_vendor(self) -> None:
        vendor = self.rng.choice(self.gen.spec.new_vendors)
        issue = self.start + timedelta(days=self.rng.randint(0, (self.end - self.start).days - 5))
        draft = self.gen.make_bill(vendor, issue, split=self.split)
        self.label(draft, "first_time_vendor")
        self.gen.drafts.append(draft)

    def unsupported_document(self) -> None:
        vendor = self.rng.choice([v for v in self.gen.spec.vendors if v.doc_type == "invoice"])
        issue = self.start + timedelta(days=self.rng.randint(0, (self.end - self.start).days - 5))
        kind = self.rng.choice(["statement", "quote"])
        extra: dict[str, Any]
        if kind == "statement":
            open_bills = [
                d
                for d in self.gen.drafts
                if d.vendor.id == vendor.id
                and d.doc.doc_type is DocumentType.INVOICE
                and d.doc.issue_date is not None
                and d.doc.issue_date < issue
            ][-4:]
            rows = [
                {
                    "date": d.doc.issue_date.isoformat() if d.doc.issue_date else "",
                    "number": d.doc.document_number,
                    "amount": str(d.total),
                }
                for d in open_bills
            ]
            balance = sum((d.total for d in open_bills), ZERO)
            extra = {"kind": kind, "rows": rows, "balance": str(balance)}
            number = None
        else:
            lines = self.gen.pick_lines(vendor, issue)
            extra = {
                "kind": kind,
                "rows": [
                    {
                        "description": bl.description,
                        "quantity": str(bl.quantity),
                        "unit_price": str(bl.unit_price),
                        "amount": str(round_money(bl.quantity * bl.unit_price)),
                    }
                    for bl in lines
                ],
                "valid_days": 30,
            }
            number = f"Q-{self.rng.randint(1000, 9999)}"
        doc = ExtractedDocument(
            doc_type=DocumentType.OTHER,
            vendor_name=vendor.name,
            vendor_state=vendor.state,
            document_number=number,
            issue_date=issue,
        )
        draft = self.gen.draft_for(vendor, doc, (), issue, split=self.split, layout=kind)
        draft.file_format = "pdf"
        draft.extra = extra
        self.label(draft, "unsupported_document")
        self.gen.drafts.append(draft)

    # ------------------------------------------------------------------ hard negatives

    def split_shipment(self) -> None:
        source = self.take(
            [d for d in self.clean_bills(inventory=True, invoice=True) if len(d.vendor.catalog) > 1]
        )
        vendor = source.vendor
        base_day = source.doc.issue_date
        assert base_day is not None
        if source.doc.po_number is None:
            self.gen.bump_po()
            source.doc = source.doc.model_copy(update={"po_number": self.gen.current_po})
        issue = min(base_day + timedelta(days=self.rng.randint(0, 3)), self.end)
        po = source.doc.po_number
        doc, accounts = self.gen.build_doc(
            vendor, issue, self.gen.pick_lines(vendor, issue), po_number=po
        )
        for _ in range(10):
            if doc.total != source.doc.total:
                break
            lines = self.gen.pick_lines(vendor, issue)
            doc, accounts = self.gen.build_doc(vendor, issue, lines, po_number=po)
        second = self.gen.draft_for(vendor, doc, accounts, issue, split=self.split)
        self.negative(second, "split_shipment", source)
        self.gen.drafts.append(second)

    def same_number_other_vendor(self) -> None:
        draft = self.take(self.clean_bills(invoice=True))
        others = [
            d
            for d in self.gen.drafts
            if d.vendor.id != draft.vendor.id
            and d.is_bill
            and d.doc.document_number
            and d.received_at < draft.received_at
            and normalize_document_number(d.doc.document_number)
            not in self._numbers_of(draft.vendor.id)
        ]
        other = self.rng.choice(others)
        draft.doc = draft.doc.model_copy(update={"document_number": other.doc.document_number})
        self.negative(draft, "same_number_other_vendor", other)

    def _numbers_of(self, vendor_id: str) -> set[str | None]:
        return {
            normalize_document_number(d.doc.document_number)
            for d in self.gen.drafts
            if d.vendor.id == vendor_id
        }

    def seasonal_restock(self) -> None:
        pool = [
            d
            for d in self.clean_bills(inventory=True, invoice=True)
            if self.gen.history_median(d.vendor.id) is not None
        ]
        draft = self.take(pool)
        median = self.gen.history_median(draft.vendor.id)
        assert median is not None and draft.doc.subtotal is not None
        rate = self.gen.spec.tax_rates[draft.vendor.state]
        target = median * Decimal(self.rng.randint(16, 24)) / 10
        doc = draft.doc
        for _ in range(20):
            factor = max(Decimal("1.05"), (target / draft.doc.subtotal).quantize(Decimal("0.01")))
            doc = _recompute(
                _scale_lines(draft.doc, factor), rate, print_rate=draft.doc.tax_rate is not None
            )
            assert doc.total is not None
            ratio = doc.total / median
            if Decimal("1.5") <= ratio <= Decimal("2.5"):
                break
            target = (
                target * Decimal("1.05") if ratio < Decimal("1.5") else target * Decimal("0.95")
            )
        assert doc.total is not None
        ratio = doc.total / median
        if not Decimal("1.5") <= ratio <= Decimal("2.5"):
            self.used.discard(draft.key)
            return self.seasonal_restock()
        self.replace_doc(draft, doc)
        self.negative(draft, "seasonal_restock")
        return None


def generate(spec: ClientSpec, catalog: AnomalyCatalog, seed: int) -> list[GroundTruth]:
    return Generator(spec, catalog, seed).run()


def log_ratio(a: Decimal, b: Decimal) -> float:
    return math.log(float(a) / float(b))


def group_by_split(records: list[GroundTruth]) -> dict[Split, list[GroundTruth]]:
    out: dict[Split, list[GroundTruth]] = defaultdict(list)
    for record in records:
        out[record.split].append(record)
    return dict(out)
