import hashlib
from decimal import Decimal
from functools import cache
from pathlib import Path

import pytest

from accrueboard.datagen.generate import generate
from accrueboard.datagen.records import AnomalyLabel, GroundTruth
from accrueboard.datagen.spec import EVAL_SPLITS, load_anomaly_catalog, load_client
from accrueboard.db.models import Task
from accrueboard.domain.documents import DocumentType, PaymentMethod
from accrueboard.domain.routing import Rule
from accrueboard.eval.archive import pack, unpack
from accrueboard.eval.end_to_end import (
    AccountError,
    _posting_differs,
    _should_not_post,
    _stop_if_unbillable,
)

SPEC = load_client("fernhill")


@cache
def records() -> tuple[GroundTruth, ...]:
    return tuple(r for r in generate(SPEC, load_anomaly_catalog(), 7) if r.split in EVAL_SPLITS)


def bill(kind: DocumentType) -> GroundTruth:
    return next(
        r
        for r in records()
        if r.document.doc_type is kind and not r.anomalies and len(r.document.lines) >= 2
    )


def label(kind: str, *, injected: bool = True) -> AnomalyLabel:
    return AnomalyLabel(type=kind, expected_rule=Rule.AMOUNT_OUTLIER, injected=injected)


def test_documents_that_must_not_be_posted() -> None:
    record = bill(DocumentType.INVOICE)
    assert not _should_not_post(record)
    for kind in ("renumbered_duplicate", "arithmetic_error", "unsupported_document"):
        assert _should_not_post(record.model_copy(update={"anomalies": (label(kind),)}))
    assert _should_not_post(record.model_copy(update={"anomalies": (label("amount_outlier"),)}))
    natural = record.model_copy(update={"anomalies": (label("amount_outlier", injected=False),)})
    assert not _should_not_post(natural)
    policy = record.model_copy(update={"anomalies": (label("over_materiality"),)})
    assert not _should_not_post(policy)


def test_posting_differs_on_anything_that_changes_the_entry() -> None:
    record = bill(DocumentType.INVOICE)
    doc = record.document
    accounts = list(record.line_accounts)
    assert not _posting_differs(record, doc, accounts, SPEC)
    assert _posting_differs(record, None, accounts, SPEC)
    assert _posting_differs(
        record, doc.model_copy(update={"total": Decimal("1.00")}), accounts, SPEC
    )
    assert _posting_differs(record, doc.model_copy(update={"lines": doc.lines[:1]}), accounts, SPEC)
    other = "6100" if accounts[0] != "6100" else "6200"
    assert _posting_differs(record, doc, [other, *accounts[1:]], SPEC)
    # Reading an unpaid bill as already charged credits the card instead of payables.
    assert _posting_differs(
        record, doc.model_copy(update={"payment_method": PaymentMethod.CARD}), accounts, SPEC
    )
    # An invoice already charged to the card, read as a card receipt, posts the same entry.
    paid = record.model_copy(
        update={"document": doc.model_copy(update={"payment_method": PaymentMethod.CARD})}
    )
    as_receipt = paid.document.model_copy(update={"doc_type": DocumentType.RECEIPT})
    assert not _posting_differs(paid, as_receipt, accounts, SPEC)
    # A different vendor is wrong even if the entry matches.
    assert _posting_differs(
        record, doc.model_copy(update={"vendor_name": "Someone Else"}), accounts, SPEC
    )
    # On a receipt, the payment method picks the account credited.
    receipt = bill(DocumentType.RECEIPT)
    flipped = (
        PaymentMethod.BANK
        if receipt.document.payment_method is PaymentMethod.CARD
        else PaymentMethod.CARD
    )
    assert _posting_differs(
        receipt,
        receipt.document.model_copy(update={"payment_method": flipped}),
        list(receipt.line_accounts),
        SPEC,
    )


def test_archive_round_trip_is_reproducible(tmp_path: Path) -> None:
    root = tmp_path / "recordings"
    (root / "code").mkdir(parents=True)
    (root / "extract").mkdir()
    files = [root / "code" / "b.json", root / "extract" / "a.json"]
    files[0].write_text('{"x": 1}\n')
    files[1].write_text('{"y": 2}\n')
    (root / "code" / "stale.json").write_text("not part of this run")

    first, second = tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"
    assert pack(root, files, first) == 2
    assert pack(root, reversed(files), second) == 2
    digest = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (first, second)]
    assert digest[0] == digest[1]

    out = tmp_path / "out"
    assert unpack(first, out) == 2
    assert (out / "code" / "b.json").read_text() == '{"x": 1}\n'
    assert not (out / "code" / "stale.json").exists()


def test_account_errors_stop_the_run_instead_of_counting_as_failures() -> None:
    def task(state: str, error: str | None) -> Task:
        return Task(id="task_x", state=state, last_error=error)

    with pytest.raises(AccountError, match="credit balance"):
        _stop_if_unbillable(task("failed", "BadRequestError: Your credit balance is too low"))
    _stop_if_unbillable(task("failed", "LLMError: extract: output truncated at max_tokens"))
    _stop_if_unbillable(task("posted", None))
