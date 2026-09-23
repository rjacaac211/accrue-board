"""Rendered documents must be deterministic and must print every value extraction is scored on."""

import io
from decimal import Decimal

import pypdfium2 as pdfium
import pytest
from PIL import Image

from accrueboard.datagen.generate import generate
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.render import STYLES, percent, render, render_pdf
from accrueboard.datagen.spec import (
    EVAL_SPLITS,
    ClientSpec,
    load_anomaly_catalog,
    load_client,
)
from accrueboard.domain.documents import DocumentType


@pytest.fixture(scope="module")
def spec() -> ClientSpec:
    return load_client("fernhill")


@pytest.fixture(scope="module")
def samples(spec: ClientSpec) -> list[GroundTruth]:
    """One evaluation record per (layout, document type, file format) combination."""
    records = generate(spec, load_anomaly_catalog(), 7)
    picked: dict[tuple[str, str, str], GroundTruth] = {}
    for r in records:
        if r.split in EVAL_SPLITS:
            picked.setdefault((r.layout, r.document.doc_type.value, r.file_format), r)
    return list(picked.values())


def pdf_text(data: bytes) -> str:
    document = pdfium.PdfDocument(data)
    try:
        return document[0].get_textpage().get_text_range()
    finally:
        document.close()


def test_samples_cover_every_layout(samples: list[GroundTruth]) -> None:
    layouts = {r.layout for r in samples}
    assert set(STYLES) | {"receipt", "statement", "quote"} <= layouts
    assert {"pdf", "png"} <= {r.file_format for r in samples}


def test_rendering_is_deterministic(samples: list[GroundTruth], spec: ClientSpec) -> None:
    for record in samples:
        assert render(record, spec) == render(record, spec), record.doc_id


def test_every_scored_value_is_printed(samples: list[GroundTruth], spec: ClientSpec) -> None:
    for record in samples:
        doc = record.document
        if doc.doc_type is DocumentType.OTHER:
            continue
        text = pdf_text(render_pdf(record, spec)).replace(",", "")
        assert doc.vendor_name is not None
        assert doc.vendor_name.upper() in text.upper(), record.doc_id
        assert doc.document_number is not None
        assert doc.document_number in text, record.doc_id
        assert doc.total is not None
        assert f"{doc.total:.2f}" in text, record.doc_id
        assert doc.subtotal is not None
        assert f"{doc.subtotal:.2f}" in text, record.doc_id
        for item in doc.lines:
            assert item.description[:34] in text, (record.doc_id, item.description)
            assert f"{item.amount:.2f}" in text, record.doc_id
        if doc.tax > 0:
            assert f"{doc.tax:.2f}" in text, record.doc_id
        if doc.tax_rate is not None:
            assert percent(doc.tax_rate) in text, record.doc_id
        if doc.discount > 0:
            assert f"{doc.discount:.2f}" in text, record.doc_id
        if doc.shipping > 0:
            assert f"{doc.shipping:.2f}" in text, record.doc_id
        if doc.referenced_document_number:
            assert doc.referenced_document_number in text, record.doc_id
        if doc.po_number:
            assert doc.po_number in text, record.doc_id


def test_credit_notes_and_other_documents_are_titled(
    samples: list[GroundTruth], spec: ClientSpec
) -> None:
    for record in samples:
        text = pdf_text(render_pdf(record, spec))
        if record.document.doc_type is DocumentType.CREDIT_NOTE:
            assert "CREDIT MEMO" in text
        if record.document.doc_type is DocumentType.OTHER:
            assert ("STATEMENT OF ACCOUNT" in text) or ("QUOTATION" in text)


def test_png_receipts_are_images(samples: list[GroundTruth], spec: ClientSpec) -> None:
    pngs = [r for r in samples if r.file_format == "png"]
    assert pngs
    for record in pngs:
        image = Image.open(io.BytesIO(render(record, spec)))
        assert image.format == "PNG"
        assert image.mode == "L"
        assert image.size[0] > 500


@pytest.mark.parametrize(
    ("rate", "text"),
    [("0.0825", "8.25%"), ("0.08875", "8.875%"), ("0.07", "7%"), ("0.1025", "10.25%")],
)
def test_percent_format(rate: str, text: str) -> None:
    assert percent(Decimal(rate)) == text
