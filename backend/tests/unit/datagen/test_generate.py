"""Generator properties: determinism, consistency with the domain rules, and label truthfulness."""

import statistics
from collections import Counter
from decimal import Decimal

import pytest

from accrueboard.datagen.generate import generate
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import (
    EVAL_SPLITS,
    AnomalyCatalog,
    ClientSpec,
    Split,
    load_anomaly_catalog,
    load_client,
)
from accrueboard.domain.documents import DocumentType
from accrueboard.domain.duplicates import normalize_document_number
from accrueboard.domain.journal import build_entry
from accrueboard.domain.validation import validate_document

SEED = 7


@pytest.fixture(scope="module")
def spec() -> ClientSpec:
    return load_client("fernhill")


@pytest.fixture(scope="module")
def catalog() -> AnomalyCatalog:
    return load_anomaly_catalog()


@pytest.fixture(scope="module", params=[SEED, 42, 2026], ids=lambda s: f"seed{s}")
def records(
    spec: ClientSpec, catalog: AnomalyCatalog, request: pytest.FixtureRequest
) -> list[GroundTruth]:
    return generate(spec, catalog, request.param)


@pytest.fixture(scope="module")
def by_id(records: list[GroundTruth]) -> dict[str, GroundTruth]:
    return {r.doc_id: r for r in records}


def with_anomaly(
    records: list[GroundTruth], kind: str, *, injected_only: bool = False
) -> list[GroundTruth]:
    return [
        r
        for r in records
        if any(a.type == kind and (a.injected or not injected_only) for a in r.anomalies)
    ]


def with_negative(records: list[GroundTruth], kind: str) -> list[GroundTruth]:
    return [r for r in records if any(h.type == kind for h in r.hard_negatives)]


def history_median(records: list[GroundTruth], vendor_id: str) -> Decimal:
    totals = [
        r.document.total
        for r in records
        if r.split is Split.HISTORY
        and r.vendor_id == vendor_id
        and r.document.doc_type in (DocumentType.INVOICE, DocumentType.RECEIPT)
    ]
    assert len(totals) >= 5
    return Decimal(str(statistics.median(t for t in totals if t is not None)))


def source_of(record: GroundTruth, kind: str, by_id: dict[str, GroundTruth]) -> GroundTruth:
    label = next(a for a in record.anomalies if a.type == kind)
    assert label.source_doc_id is not None
    return by_id[label.source_doc_id]


# ------------------------------------------------------------------ determinism and shape


def test_same_seed_gives_identical_records(spec: ClientSpec, catalog: AnomalyCatalog) -> None:
    first = generate(spec, catalog, SEED)
    again = generate(spec, catalog, SEED)
    assert [r.model_dump_json() for r in again] == [r.model_dump_json() for r in first]


def test_different_seed_gives_different_records(spec: ClientSpec, catalog: AnomalyCatalog) -> None:
    one = generate(spec, catalog, SEED)
    other = generate(spec, catalog, SEED + 1)
    assert [r.model_dump_json() for r in other] != [r.model_dump_json() for r in one]


def test_split_sizes_are_in_the_planned_range(records: list[GroundTruth]) -> None:
    sizes = Counter(r.split for r in records)
    assert 400 <= sizes[Split.HISTORY] <= 700
    assert 120 <= sizes[Split.VALIDATION] <= 200
    assert 250 <= sizes[Split.TEST] <= 380


def test_ids_are_unique_and_follow_arrival_order(records: list[GroundTruth]) -> None:
    assert len({r.doc_id for r in records}) == len(records)
    for split in Split:
        in_split = [r for r in records if r.split is split]
        assert in_split == sorted(in_split, key=lambda r: (r.received_at, r.doc_id))
        assert in_split[0].doc_id.endswith("-0001")


def test_history_is_clean_and_unrendered(records: list[GroundTruth], spec: ClientSpec) -> None:
    period = spec.periods[Split.HISTORY]
    for r in (r for r in records if r.split is Split.HISTORY):
        assert not r.anomalies
        assert not r.hard_negatives
        assert r.file is None
        assert r.document.issue_date is not None
        assert period.start <= r.document.issue_date <= period.end


def test_eval_documents_have_files_and_arrive_after_issue(records: list[GroundTruth]) -> None:
    for r in (r for r in records if r.split in EVAL_SPLITS):
        assert r.file == f"documents/{r.split.value}/{r.doc_id}.{r.file_format}"
        assert r.document.issue_date is not None
        assert r.document.issue_date <= r.received_at.date()


def test_png_files_are_receipts_only(records: list[GroundTruth]) -> None:
    pngs = [r for r in records if r.file and r.file_format == "png"]
    assert pngs
    assert all(r.document.doc_type is DocumentType.RECEIPT for r in pngs)


# ------------------------------------------------------------------ consistency with the domain


def test_only_arithmetic_errors_fail_validation(records: list[GroundTruth]) -> None:
    for r in records:
        if r.document.doc_type is DocumentType.OTHER:
            continue
        issues = validate_document(r.document, today=r.received_at.date())
        has_error = any(a.type == "arithmetic_error" for a in r.anomalies)
        assert bool(issues) == has_error, (r.doc_id, issues)


def test_every_valid_document_posts_a_balanced_entry(
    records: list[GroundTruth], spec: ClientSpec
) -> None:
    chart = spec.chart
    posted = 0
    for r in records:
        if r.document.doc_type is DocumentType.OTHER or r.anomalies:
            continue
        entry = build_entry(r.document, r.line_accounts, chart)
        assert entry.is_balanced
        assert entry.total == r.document.total
        posted += 1
    assert posted > 500


def test_line_accounts_align_with_lines(records: list[GroundTruth], spec: ClientSpec) -> None:
    for r in records:
        assert len(r.line_accounts) == len(r.document.lines)
        assert all(spec.chart.has(code) for code in r.line_accounts)


# ------------------------------------------------------------------ label counts


def test_injected_anomaly_counts_match_spec(
    records: list[GroundTruth], catalog: AnomalyCatalog
) -> None:
    for spec in catalog.anomalies:
        for split in EVAL_SPLITS:
            found = [
                r
                for r in records
                if r.split is split and any(a.type == spec.id and a.injected for a in r.anomalies)
            ]
            assert len(found) == spec.counts[split], (spec.id, split)


def test_hard_negative_counts_match_spec(
    records: list[GroundTruth], catalog: AnomalyCatalog
) -> None:
    for spec in catalog.hard_negatives:
        if spec.counts == "natural":
            assert with_negative(records, spec.id), spec.id
            continue
        for split in EVAL_SPLITS:
            found = [r for r in with_negative(records, spec.id) if r.split is split]
            assert len(found) == spec.counts[split], (spec.id, split)


def test_labels_carry_the_rule_from_the_spec(
    records: list[GroundTruth], catalog: AnomalyCatalog
) -> None:
    for r in records:
        for a in r.anomalies:
            assert a.expected_rule == catalog.anomaly(a.type).expected_rule


# ------------------------------------------------------------------ each anomaly has its property


def test_exact_file_duplicates_repeat_their_source(
    records: list[GroundTruth], by_id: dict[str, GroundTruth]
) -> None:
    for r in with_anomaly(records, "exact_file_duplicate"):
        source = source_of(r, "exact_file_duplicate", by_id)
        assert r.copy_of == source.doc_id
        assert r.document == source.document
        assert r.file_format == source.file_format == "pdf"
        assert r.received_at > source.received_at


def test_renumbered_duplicates_match_after_normalization(
    records: list[GroundTruth], by_id: dict[str, GroundTruth]
) -> None:
    for r in with_anomaly(records, "renumbered_duplicate"):
        source = source_of(r, "renumbered_duplicate", by_id)
        assert r.vendor_id == source.vendor_id
        assert r.document.total == source.document.total
        assert r.document.document_number != source.document.document_number
        assert normalize_document_number(r.document.document_number) == (
            normalize_document_number(source.document.document_number)
        )
        assert r.layout != source.layout
        assert r.received_at > source.received_at


def test_near_duplicates_are_reissued_within_the_window(
    records: list[GroundTruth], by_id: dict[str, GroundTruth]
) -> None:
    for r in with_anomaly(records, "near_duplicate"):
        source = source_of(r, "near_duplicate", by_id)
        assert r.vendor_id == source.vendor_id
        assert r.document.total == source.document.total
        assert r.document.issue_date is not None
        assert source.document.issue_date is not None
        assert 0 < (r.document.issue_date - source.document.issue_date).days <= 9
        assert normalize_document_number(r.document.document_number) != (
            normalize_document_number(source.document.document_number)
        )


def test_amount_outliers_are_at_least_eight_times_the_median(records: list[GroundTruth]) -> None:
    cases = with_anomaly(records, "amount_outlier")
    assert cases
    for r in cases:
        assert r.document.total is not None
        assert r.document.total >= 8 * history_median(records, r.vendor_id)


def test_injected_outliers_stay_under_the_materiality_cap(
    records: list[GroundTruth], spec: ClientSpec
) -> None:
    for r in with_anomaly(records, "amount_outlier", injected_only=True):
        assert r.document.total is not None
        assert r.document.total < spec.materiality_cap


def test_tax_on_resale_inventory(records: list[GroundTruth]) -> None:
    for r in with_anomaly(records, "tax_on_resale_inventory"):
        assert r.document.tax > 0
        assert any(
            line.taxable and code == "1300"
            for line, code in zip(r.document.lines, r.line_accounts, strict=True)
        )


def test_over_materiality(records: list[GroundTruth], spec: ClientSpec) -> None:
    for r in with_anomaly(records, "over_materiality"):
        assert r.document.total is not None
        assert r.document.total > spec.materiality_cap


def test_every_bill_over_the_cap_is_labelled(records: list[GroundTruth], spec: ClientSpec) -> None:
    for r in records:
        if r.split in EVAL_SPLITS and r.document.total and r.document.total > spec.materiality_cap:
            assert any(a.type == "over_materiality" for a in r.anomalies), r.doc_id


def test_duplicate_credit_notes(records: list[GroundTruth], by_id: dict[str, GroundTruth]) -> None:
    for r in with_anomaly(records, "duplicate_credit_note"):
        first = source_of(r, "duplicate_credit_note", by_id)
        assert r.document.doc_type is first.document.doc_type is DocumentType.CREDIT_NOTE
        assert r.vendor_id == first.vendor_id
        assert r.document.document_number != first.document.document_number
        assert normalize_document_number(r.document.referenced_document_number) == (
            normalize_document_number(first.document.referenced_document_number)
        )


def test_first_time_vendors_are_absent_from_history(records: list[GroundTruth]) -> None:
    history_vendors = {r.vendor_id for r in records if r.split is Split.HISTORY}
    for r in with_anomaly(records, "first_time_vendor"):
        assert r.vendor_id not in history_vendors


def test_unsupported_documents(records: list[GroundTruth]) -> None:
    cases = with_anomaly(records, "unsupported_document")
    assert cases
    for r in cases:
        assert r.document.doc_type is DocumentType.OTHER
        assert r.extra["kind"] in {"statement", "quote"}
        assert r.layout == r.extra["kind"]
    others = [r for r in records if r.document.doc_type is DocumentType.OTHER]
    assert others == cases


# ------------------------------------------------------------------ hard negatives


def related(record: GroundTruth, kind: str, by_id: dict[str, GroundTruth]) -> GroundTruth:
    label = next(h for h in record.hard_negatives if h.type == kind)
    assert label.related_doc_id is not None
    return by_id[label.related_doc_id]


def test_split_shipments(records: list[GroundTruth], by_id: dict[str, GroundTruth]) -> None:
    for r in with_negative(records, "split_shipment"):
        other = related(r, "split_shipment", by_id)
        assert r.vendor_id == other.vendor_id
        assert r.document.po_number == other.document.po_number is not None
        assert r.document.total != other.document.total
        assert normalize_document_number(r.document.document_number) != (
            normalize_document_number(other.document.document_number)
        )


def test_same_number_other_vendor(
    records: list[GroundTruth], by_id: dict[str, GroundTruth]
) -> None:
    for r in with_negative(records, "same_number_other_vendor"):
        other = related(r, "same_number_other_vendor", by_id)
        assert r.vendor_id != other.vendor_id
        assert r.document.document_number == other.document.document_number
        own = [
            x
            for x in records
            if x.vendor_id == r.vendor_id
            and x.doc_id != r.doc_id
            and normalize_document_number(x.document.document_number)
            == normalize_document_number(r.document.document_number)
        ]
        assert own == []


def test_seasonal_restocks_are_moderately_larger(records: list[GroundTruth]) -> None:
    for r in with_negative(records, "seasonal_restock"):
        assert r.document.total is not None
        ratio = r.document.total / history_median(records, r.vendor_id)
        assert Decimal("1.5") <= ratio <= Decimal("2.5")
        assert not r.anomalies


def test_first_credit_notes_are_labelled(records: list[GroundTruth]) -> None:
    eval_notes = [
        r
        for r in records
        if r.split in EVAL_SPLITS and r.document.doc_type is DocumentType.CREDIT_NOTE
    ]
    for r in eval_notes:
        labelled = {h.type for h in r.hard_negatives} | {a.type for a in r.anomalies}
        assert labelled & {"credit_note_for_invoice", "duplicate_credit_note"}, r.doc_id


def test_recurring_charges_have_identical_totals(records: list[GroundTruth]) -> None:
    by_vendor: dict[str, set[Decimal | None]] = {}
    for r in with_negative(records, "recurring_charge"):
        by_vendor.setdefault(r.vendor_id, set()).add(r.document.total)
    assert by_vendor
    assert all(len(totals) == 1 for totals in by_vendor.values())


# ------------------------------------------------------------------ no unlabelled anomalies


def bills_in_order(records: list[GroundTruth]) -> list[GroundTruth]:
    return sorted(
        (r for r in records if r.document.doc_type in (DocumentType.INVOICE, DocumentType.RECEIPT)),
        key=lambda r: (r.received_at, r.doc_id),
    )


def test_every_repeated_vendor_number_is_a_labelled_duplicate(records: list[GroundTruth]) -> None:
    seen: set[tuple[str, str | None]] = set()
    for r in bills_in_order(records):
        key = (r.vendor_id, normalize_document_number(r.document.document_number))
        if key in seen and r.split in EVAL_SPLITS:
            kinds = {a.type for a in r.anomalies}
            assert kinds & {"exact_file_duplicate", "renumbered_duplicate"}, r.doc_id
        seen.add(key)


def test_every_same_total_reissue_is_labelled(records: list[GroundTruth]) -> None:
    earlier: list[GroundTruth] = []
    for r in bills_in_order(records):
        if r.split in EVAL_SPLITS and r.document.issue_date is not None:
            for o in earlier:
                if (
                    o.vendor_id == r.vendor_id
                    and o.document.total == r.document.total
                    and o.document.issue_date is not None
                    and abs((r.document.issue_date - o.document.issue_date).days) <= 10
                ):
                    labels = {a.type for a in r.anomalies}
                    assert labels & {
                        "near_duplicate",
                        "exact_file_duplicate",
                        "renumbered_duplicate",
                    }, (r.doc_id, o.doc_id)
        earlier.append(r)


def test_later_credit_notes_for_one_invoice_are_labelled(records: list[GroundTruth]) -> None:
    notes = sorted(
        (r for r in records if r.document.doc_type is DocumentType.CREDIT_NOTE),
        key=lambda r: (r.received_at, r.doc_id),
    )
    seen: set[tuple[str, str | None]] = set()
    for r in notes:
        key = (r.vendor_id, normalize_document_number(r.document.referenced_document_number))
        if key in seen:
            assert r.split in EVAL_SPLITS
            assert any(a.type == "duplicate_credit_note" for a in r.anomalies), r.doc_id
        seen.add(key)


def test_credit_memo_numbers_increase_with_issue_date(records: list[GroundTruth]) -> None:
    by_vendor: dict[str, list[GroundTruth]] = {}
    for r in records:
        if r.document.doc_type is DocumentType.CREDIT_NOTE:
            by_vendor.setdefault(r.vendor_id, []).append(r)
    for notes in by_vendor.values():
        notes.sort(key=lambda r: (r.document.issue_date, r.received_at))
        numbers = [r.document.document_number or "" for r in notes]
        assert numbers == sorted(numbers)
        assert len(set(numbers)) == len(numbers)
