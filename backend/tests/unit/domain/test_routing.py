from typing import Any

import pytest
from pydantic import ValidationError

from accrueboard.domain.documents import DocumentType, ExtractedDocument
from accrueboard.domain.duplicates import DuplicateKind, DuplicateMatch
from accrueboard.domain.money import money
from accrueboard.domain.outliers import OutlierAssessment, OutlierBasis, OutlierLevel
from accrueboard.domain.routing import (
    Outcome,
    RoutingConfig,
    RoutingInput,
    Rule,
    Severity,
    decide,
    evaluate_rules,
)
from accrueboard.domain.validation import validate_document

from .factories import INVENTORY, OFFICE, TODAY, invoice

CONFIG = RoutingConfig(auto_post_threshold=0.85)


def routing_input(**overrides: Any) -> RoutingInput:
    doc = overrides.pop("doc", invoice())
    data: dict[str, Any] = {
        "doc": doc,
        "validation_issues": validate_document(doc, today=TODAY),
        "ungrounded_fields": frozenset(),
        "duplicates": (),
        "outlier": None,
        "line_accounts": (OFFICE, OFFICE),
        "inventory_account": INVENTORY,
        "vendor_known": True,
        "credit_reference_found": None,
        "extraction_confidence": 1.0,
        "coding_confidence": 0.95,
    }
    data.update(overrides)
    return RoutingInput(**data)


def rules(inp: RoutingInput) -> set[Rule]:
    return {hit.rule for hit in evaluate_rules(inp, CONFIG)}


def outlier(level: OutlierLevel) -> OutlierAssessment:
    return OutlierAssessment(
        basis=OutlierBasis.VENDOR,
        level=level,
        z=9.0 if level is OutlierLevel.HARD else 3.0,
        history_size=6,
        median=money("500.00"),
        ratio_to_median=9.7,
        explanation="4850.00 is 9.70x the vendor median",
    )


# ------------------------------------------------------------------ clean path


def test_clean_confident_document_auto_posts() -> None:
    decision = decide(routing_input(), CONFIG)
    assert decision.outcome is Outcome.AUTO_POST
    assert decision.hits == ()
    assert decision.score == pytest.approx(0.95)
    assert "0.95" in decision.summary


def test_score_is_the_weaker_confidence() -> None:
    decision = decide(routing_input(extraction_confidence=0.7, coding_confidence=0.99), CONFIG)
    assert decision.score == pytest.approx(0.7)
    assert decision.outcome is Outcome.NEEDS_REVIEW
    assert "below" in decision.summary


def test_threshold_is_inclusive() -> None:
    decision = decide(routing_input(coding_confidence=0.85), CONFIG)
    assert decision.outcome is Outcome.AUTO_POST


def test_confidences_must_be_probabilities() -> None:
    with pytest.raises(ValidationError):
        routing_input(coding_confidence=1.2)


# ------------------------------------------------------------------ hard rules


def test_unsupported_type_is_the_only_hit() -> None:
    doc = ExtractedDocument(doc_type=DocumentType.OTHER)
    inp = routing_input(doc=doc, line_accounts=(), vendor_known=False)
    assert rules(inp) == {Rule.UNSUPPORTED_TYPE}
    assert decide(inp, CONFIG).outcome is Outcome.NEEDS_REVIEW


def test_each_validation_issue_is_a_hard_hit() -> None:
    doc = invoice(total="1.00", document_number=None)
    hits = evaluate_rules(routing_input(doc=doc), CONFIG)
    assert [h.rule for h in hits] == [Rule.VALIDATION_FAILED, Rule.VALIDATION_FAILED]
    assert all(h.severity is Severity.HARD for h in hits)


def test_ungrounded_critical_field() -> None:
    assert rules(routing_input(ungrounded_fields=frozenset({"total"}))) == {
        Rule.UNGROUNDED_CRITICAL_FIELD
    }
    # A non-critical field that could not be grounded only lowers extraction confidence upstream.
    assert rules(routing_input(ungrounded_fields=frozenset({"po_number"}))) == set()


@pytest.mark.parametrize(
    ("kind", "rule"),
    [
        (DuplicateKind.EXACT_FILE, Rule.DUPLICATE_FILE),
        (DuplicateKind.SAME_NUMBER, Rule.DUPLICATE_NUMBER),
        (DuplicateKind.DUPLICATE_CREDIT_NOTE, Rule.DUPLICATE_CREDIT_NOTE),
    ],
)
def test_hard_duplicates(kind: DuplicateKind, rule: Rule) -> None:
    match = DuplicateMatch(kind=kind, other_doc_id="doc-7", detail="x")
    hits = evaluate_rules(routing_input(duplicates=(match,)), CONFIG)
    assert [(h.rule, h.severity) for h in hits] == [(rule, Severity.HARD)]
    assert "doc-7" in hits[0].detail


def test_hard_outlier() -> None:
    assert rules(routing_input(outlier=outlier(OutlierLevel.HARD))) == {Rule.AMOUNT_OUTLIER}


def test_over_materiality_cap() -> None:
    config = RoutingConfig(auto_post_threshold=0.85, materiality_cap=money("200.00"))
    hits = evaluate_rules(routing_input(), config)
    assert [h.rule for h in hits] == [Rule.OVER_MATERIALITY]
    config = RoutingConfig(auto_post_threshold=0.85, materiality_cap=money("232.43"))
    assert evaluate_rules(routing_input(), config) == ()


def test_sales_tax_on_resale_inventory() -> None:
    # The first line is taxable; coding it to Inventory means tax was charged on stock for resale.
    assert rules(routing_input(line_accounts=(INVENTORY, OFFICE))) == {Rule.TAX_ON_RESALE_INVENTORY}
    # The exempt second line on Inventory is fine.
    assert rules(routing_input(line_accounts=(OFFICE, INVENTORY))) == set()


def test_first_time_vendor() -> None:
    assert rules(routing_input(vendor_known=False)) == {Rule.FIRST_TIME_VENDOR}


def test_any_hard_rule_overrides_high_confidence() -> None:
    decision = decide(
        routing_input(vendor_known=False, extraction_confidence=1.0, coding_confidence=1.0),
        CONFIG,
    )
    assert decision.outcome is Outcome.NEEDS_REVIEW
    assert "first-time vendor" in decision.summary.lower()


# ------------------------------------------------------------------ soft signals


def test_near_duplicate_lowers_score() -> None:
    match = DuplicateMatch(kind=DuplicateKind.NEAR_DUPLICATE, other_doc_id="doc-3", detail="x")
    decision = decide(routing_input(duplicates=(match,), coding_confidence=1.0), CONFIG)
    assert [h.severity for h in decision.hits] == [Severity.SOFT]
    assert decision.score == pytest.approx(CONFIG.soft_factors[Rule.NEAR_DUPLICATE])
    assert decision.factors == {"near_duplicate": CONFIG.soft_factors[Rule.NEAR_DUPLICATE]}


def test_soft_factor_applies_once_per_rule() -> None:
    matches = tuple(
        DuplicateMatch(kind=DuplicateKind.NEAR_DUPLICATE, other_doc_id=f"d{i}", detail="x")
        for i in range(3)
    )
    decision = decide(routing_input(duplicates=matches, coding_confidence=1.0), CONFIG)
    assert decision.score == pytest.approx(CONFIG.soft_factors[Rule.NEAR_DUPLICATE])


def test_mild_outlier_is_soft() -> None:
    hits = evaluate_rules(routing_input(outlier=outlier(OutlierLevel.SOFT)), CONFIG)
    assert [(h.rule, h.severity) for h in hits] == [(Rule.MILD_OUTLIER, Severity.SOFT)]


def test_credit_note_reference_not_found_is_soft() -> None:
    doc = invoice(doc_type=DocumentType.CREDIT_NOTE, referenced_document_number="INV-9")
    hits = evaluate_rules(routing_input(doc=doc, credit_reference_found=False), CONFIG)
    assert [(h.rule, h.severity) for h in hits] == [
        (Rule.CREDIT_REFERENCE_NOT_FOUND, Severity.SOFT)
    ]


def test_decision_records_its_inputs() -> None:
    decision = decide(routing_input(), CONFIG)
    assert decision.threshold == 0.85
    assert decision.extraction_confidence == 1.0
    assert decision.coding_confidence == 0.95
