from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest

from accrueboard.domain.documents import DocumentType
from accrueboard.pipeline.files import SourceFile, UnsupportedFileError, pdf_text
from accrueboard.pipeline.grounding import TextIndex, ground

from .helpers import eval_records, source_for


def test_numbers_match_across_print_formats() -> None:
    index = TextIndex.build("Total Due $1,234.50  Discount -21.50  USD 99.00  qty 12")
    for value in ("1234.5", "1234.50", "21.50", "99", "12"):
        assert index.has_number(Decimal(value)), value
    assert not index.has_number(Decimal("1234.51"))


def test_percentages() -> None:
    index = TextIndex.build("Sales Tax (8.25%)  Tax 8.875 %")
    assert Decimal("0.0825") in index.percents
    assert Decimal("0.08875") in index.percents


@pytest.mark.parametrize(
    "printed",
    ["12/01/2025", "12/01/25", "2025-12-01", "December 01, 2025", "Dec 01, 2025", "01 Dec 2025"],
)
def test_date_formats(printed: str) -> None:
    assert date(2025, 12, 1) in TextIndex.build(f"Date: {printed}").dates


def test_codes_ignore_punctuation_and_case() -> None:
    index = TextIndex.build("Invoice #: inv-00123")
    assert index.has_code("INV-00123")
    assert index.has_code("INV 00123")
    assert not index.has_code("INV-00124")


def test_file_signatures() -> None:
    assert SourceFile.from_bytes("a", b"%PDF-1.7...").media_type == "application/pdf"
    assert SourceFile.from_bytes("b", b"\x89PNG\r\n\x1a\n...").media_type == "image/png"
    with pytest.raises(UnsupportedFileError):
        SourceFile.from_bytes("c", b"hello")


def test_every_generated_pdf_grounds_its_ground_truth() -> None:
    """The grounding check must never flag a correct value on any layout."""
    checked = 0
    for record in eval_records():
        if record.file_format != "pdf" or record.document.doc_type is DocumentType.OTHER:
            continue
        source = source_for(record.doc_id)
        results = ground(record.document, pdf_text(source.data))
        missing = sorted(k for k, ok in results.items() if not ok)
        assert missing == [], (record.doc_id, record.layout, missing)
        checked += 1
    assert checked > 350


def test_a_changed_value_is_not_grounded() -> None:
    record = next(
        r
        for r in eval_records()
        if r.file_format == "pdf" and r.document.doc_type is DocumentType.INVOICE
    )
    source = source_for(record.doc_id)
    assert record.document.total is not None
    wrong = record.document.model_copy(
        update={
            "total": record.document.total + Decimal("1.00"),
            "issue_date": date(1999, 1, 1),
            "vendor_name": "Someone Else Entirely",
        }
    )
    results = ground(wrong, pdf_text(source.data))
    assert not results["total"]
    assert not results["issue_date"]
    assert not results["vendor_name"]


def test_pdf_text_is_safe_from_many_threads() -> None:
    # PDFium itself is not thread-safe; without the module lock this crashes the process.
    pdfs = [r for r in eval_records() if r.file_format == "pdf"][:6]
    sources = [source_for(r.doc_id) for r in pdfs]
    expected = [pdf_text(s.data) for s in sources]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda s: pdf_text(s.data), sources * 10))
    assert results == expected * 10
