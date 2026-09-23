from decimal import Decimal
from typing import Any

import pytest

from accrueboard.domain.documents import DocumentType
from accrueboard.pipeline.extraction import (
    AGREED_IMAGE,
    DISAGREED,
    GROUNDED,
    Models,
    classify,
    extract,
    field_confidence,
    to_document,
)

from .helpers import eval_records, oracle, source_for, truth_output

MODELS = Models(classify="claude-haiku-4-5", extract="claude-sonnet-5", verify="claude-sonnet-5")


def first(predicate: Any) -> Any:
    return next(r for r in eval_records() if predicate(r))


def is_clean_pdf_invoice(r: Any) -> bool:
    return (
        r.file_format == "pdf"
        and r.document.doc_type is DocumentType.INVOICE
        and not r.anomalies
        and len(r.document.lines) >= 2
    )


# ------------------------------------------------------------------ parsing model output


def test_to_document_parses_printed_formats() -> None:
    record = first(is_clean_pdf_invoice)
    output = truth_output(record.document)
    output["total"] = f"${record.document.total:,}"
    output["tax_rate"] = "8.25" if output["tax_rate"] else None
    parsed = to_document(output, DocumentType.INVOICE)
    assert parsed.errors == frozenset()
    assert parsed.document.total == record.document.total
    if output["tax_rate"]:
        assert parsed.document.tax_rate == Decimal("0.0825")


def test_bad_values_are_isolated_not_fatal() -> None:
    record = first(is_clean_pdf_invoice)
    output = truth_output(record.document)
    output["total"] = "12,3a"
    output["issue_date"] = "sometime in June"
    output["lines"][0]["amount"] = "n/a"
    parsed = to_document(output, DocumentType.INVOICE)
    assert parsed.errors == {"total", "issue_date", "lines[0]"}
    assert parsed.document.total is None
    assert parsed.document.vendor_name == record.document.vendor_name
    assert len(parsed.document.lines) == len(record.document.lines) - 1


def test_sub_cent_amount_is_an_error() -> None:
    record = first(is_clean_pdf_invoice)
    output = truth_output(record.document)
    output["subtotal"] = "10.005"
    assert "subtotal" in to_document(output, DocumentType.INVOICE).errors


# ------------------------------------------------------------------ classification


def test_classify_returns_type_and_call_record() -> None:
    record = first(is_clean_pdf_invoice)
    llm = oracle(record)
    result = classify(llm, source_for(record.doc_id), MODELS.classify)
    assert result.doc_type is DocumentType.INVOICE
    assert result.call.purpose == "classify"
    assert result.call.model == "claude-haiku-4-5"
    assert llm.calls[0].parts[0].media_type == "application/pdf"  # type: ignore[union-attr]


# ------------------------------------------------------------------ extraction confidence


def test_clean_pdf_needs_no_second_pass() -> None:
    record = first(is_clean_pdf_invoice)
    llm = oracle(record)
    result = extract(
        llm,
        source_for(record.doc_id),
        DocumentType.INVOICE,
        MODELS,
        today=record.received_at.date(),
    )
    assert result.document == record.document
    assert not result.second_pass
    assert result.confidence == GROUNDED
    assert result.ungrounded == frozenset()
    assert [c.purpose for c in result.calls] == ["extract"]


def test_every_clean_pdf_extracts_at_full_confidence() -> None:
    count = 0
    for record in eval_records():
        if record.file_format != "pdf" or record.document.doc_type is DocumentType.OTHER:
            continue
        if any(a.type == "arithmetic_error" for a in record.anomalies):
            continue
        result = extract(
            oracle(record),
            source_for(record.doc_id),
            record.document.doc_type,
            MODELS,
            today=record.received_at.date(),
        )
        assert result.confidence == GROUNDED, (record.doc_id, result.second_pass_reason)
        count += 1
    assert count > 300


def test_images_always_get_a_second_pass() -> None:
    record = first(lambda r: r.file_format == "png")
    result = extract(
        oracle(record),
        source_for(record.doc_id),
        DocumentType.RECEIPT,
        MODELS,
        today=record.received_at.date(),
    )
    assert result.second_pass
    assert result.second_pass_reason == "image has no text layer"
    assert result.confidence == AGREED_IMAGE
    assert all(c.grounded is None for c in result.fields)


def test_document_arithmetic_errors_trigger_a_second_pass_but_stay_confident() -> None:
    record = first(
        lambda r: r.file_format == "pdf" and any(a.type == "arithmetic_error" for a in r.anomalies)
    )
    result = extract(
        oracle(record),
        source_for(record.doc_id),
        record.document.doc_type,
        MODELS,
        today=record.received_at.date(),
    )
    assert result.second_pass
    assert result.second_pass_reason == "document failed validation"
    # Both readers agree on what is printed; the problem is the document, not the extraction.
    assert result.confidence == GROUNDED


def test_invented_total_is_caught_by_grounding_and_the_second_reader() -> None:
    record = first(is_clean_pdf_invoice)

    def hallucinate(purpose: str, output: dict[str, Any]) -> dict[str, Any]:
        if purpose == "extract":
            output["total"] = str(Decimal(output["total"]) + Decimal("100.00"))
        return output

    result = extract(
        oracle(record, hallucinate),
        source_for(record.doc_id),
        DocumentType.INVOICE,
        MODELS,
        today=record.received_at.date(),
    )
    assert "total" in result.ungrounded
    assert result.second_pass
    assert result.disagreements == ("total",)
    assert result.confidence == DISAGREED
    assert [c.purpose for c in result.calls] == ["extract", "verify"]


def test_image_disagreement_lowers_confidence() -> None:
    record = first(lambda r: r.file_format == "png")

    def misread(purpose: str, output: dict[str, Any]) -> dict[str, Any]:
        if purpose == "verify":
            output["lines"][0]["amount"] = "0.01"
        return output

    result = extract(
        oracle(record, misread),
        source_for(record.doc_id),
        DocumentType.RECEIPT,
        MODELS,
        today=record.received_at.date(),
    )
    assert result.disagreements == ("lines[0].amount",)
    assert result.confidence == DISAGREED


def test_unparsable_value_scores_zero_and_triggers_a_second_pass() -> None:
    record = first(is_clean_pdf_invoice)

    def garble(purpose: str, output: dict[str, Any]) -> dict[str, Any]:
        if purpose == "extract":
            output["subtotal"] = "twelve dollars"
        return output

    result = extract(
        oracle(record, garble),
        source_for(record.doc_id),
        DocumentType.INVOICE,
        MODELS,
        today=record.received_at.date(),
    )
    assert result.unparsable == {"subtotal"}
    assert result.second_pass
    assert result.confidence == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("has_text_layer", "grounded", "agreed", "expected"),
    [
        (True, True, None, 1.0),
        (True, True, True, 1.0),
        (True, True, False, 0.3),
        (True, False, True, 0.7),
        (True, False, False, 0.3),
        (True, None, None, 1.0),
        (False, None, True, 0.9),
        (False, None, False, 0.3),
    ],
)
def test_field_confidence_table(
    has_text_layer: bool, grounded: bool | None, agreed: bool | None, expected: float
) -> None:

    assert field_confidence(has_text_layer=has_text_layer, grounded=grounded, agreed=agreed) == (
        expected
    )
