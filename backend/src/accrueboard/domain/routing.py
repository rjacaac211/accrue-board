"""Rule-then-score routing: decide whether a document can post without a human.

1. Hard rules. Any hit sends the document to review, whatever its score. They cover definite
   problems (validation failures, duplicates, clear outliers) and deliberate policy
   (materiality cap, first-time vendors).
2. Score. Otherwise the score is the weaker of extraction and coding confidence (the weakest
   link), multiplied by a factor for each kind of soft signal present.
3. Threshold. The document auto-posts if the score is at least the threshold, which is
   calibrated on held-out data rather than chosen by hand.

Every decision carries the hits, factors, confidences and threshold that produced it, so the
audit trail can explain it in plain language.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from accrueboard.domain.documents import SUPPORTED_TYPES, DocumentType, ExtractedDocument
from accrueboard.domain.duplicates import DuplicateKind, DuplicateMatch
from accrueboard.domain.money import money
from accrueboard.domain.outliers import OutlierAssessment, OutlierLevel
from accrueboard.domain.validation import ValidationIssue

CRITICAL_FIELDS = frozenset({"vendor_name", "issue_date", "total"})


class Severity(StrEnum):
    HARD = "hard"
    SOFT = "soft"


class Rule(StrEnum):
    # hard
    UNSUPPORTED_TYPE = "unsupported_type"
    VALIDATION_FAILED = "validation_failed"
    UNGROUNDED_CRITICAL_FIELD = "ungrounded_critical_field"
    DUPLICATE_FILE = "duplicate_file"
    DUPLICATE_NUMBER = "duplicate_number"
    DUPLICATE_CREDIT_NOTE = "duplicate_credit_note"
    AMOUNT_OUTLIER = "amount_outlier"
    OVER_MATERIALITY = "over_materiality"
    TAX_ON_RESALE_INVENTORY = "tax_on_resale_inventory"
    FIRST_TIME_VENDOR = "first_time_vendor"
    # soft
    NEAR_DUPLICATE = "near_duplicate"
    MILD_OUTLIER = "mild_outlier"
    CREDIT_REFERENCE_NOT_FOUND = "credit_reference_not_found"

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS: Mapping[Rule, str] = MappingProxyType(
    {
        Rule.UNSUPPORTED_TYPE: "Unsupported document type",
        Rule.VALIDATION_FAILED: "Validation failed",
        Rule.UNGROUNDED_CRITICAL_FIELD: "Key field not found in document",
        Rule.DUPLICATE_FILE: "Duplicate file",
        Rule.DUPLICATE_NUMBER: "Duplicate document number",
        Rule.DUPLICATE_CREDIT_NOTE: "Duplicate credit note",
        Rule.AMOUNT_OUTLIER: "Unusual amount",
        Rule.OVER_MATERIALITY: "Over review cap",
        Rule.TAX_ON_RESALE_INVENTORY: "Sales tax on resale inventory",
        Rule.FIRST_TIME_VENDOR: "First-time vendor",
        Rule.NEAR_DUPLICATE: "Possible duplicate",
        Rule.MILD_OUTLIER: "Somewhat unusual amount",
        Rule.CREDIT_REFERENCE_NOT_FOUND: "Credited document not found",
    }
)


_DUPLICATE_RULES: Mapping[DuplicateKind, Rule] = MappingProxyType(
    {
        DuplicateKind.EXACT_FILE: Rule.DUPLICATE_FILE,
        DuplicateKind.SAME_NUMBER: Rule.DUPLICATE_NUMBER,
        DuplicateKind.DUPLICATE_CREDIT_NOTE: Rule.DUPLICATE_CREDIT_NOTE,
        DuplicateKind.NEAR_DUPLICATE: Rule.NEAR_DUPLICATE,
    }
)

DEFAULT_SOFT_FACTORS: Mapping[Rule, float] = MappingProxyType(
    {
        Rule.NEAR_DUPLICATE: 0.6,
        Rule.MILD_OUTLIER: 0.8,
        Rule.CREDIT_REFERENCE_NOT_FOUND: 0.8,
    }
)


@dataclass(frozen=True)
class RoutingConfig:
    auto_post_threshold: float
    """Minimum score to auto-post. Calibrated on the validation split (see the eval suite)."""
    materiality_cap: Decimal = field(default_factory=lambda: money("10000.00"))
    soft_factors: Mapping[Rule, float] = DEFAULT_SOFT_FACTORS


class RuleHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: Rule
    severity: Severity
    detail: str


class RoutingInput(BaseModel):
    """Everything routing needs, gathered by the pipeline before the decision."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    doc: ExtractedDocument
    validation_issues: tuple[ValidationIssue, ...]
    ungrounded_fields: frozenset[str]
    duplicates: tuple[DuplicateMatch, ...]
    outlier: OutlierAssessment | None
    line_accounts: tuple[str, ...]
    inventory_account: str
    vendor_known: bool
    credit_reference_found: bool | None
    """For credit notes: whether the referenced document is in the ledger. None otherwise."""
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    coding_confidence: float = Field(ge=0.0, le=1.0)


class Outcome(StrEnum):
    AUTO_POST = "auto_post"
    NEEDS_REVIEW = "needs_review"


class RoutingDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    hits: tuple[RuleHit, ...]
    score: float
    threshold: float
    extraction_confidence: float
    coding_confidence: float
    factors: dict[str, float]
    summary: str


def evaluate_rules(inp: RoutingInput, config: RoutingConfig) -> tuple[RuleHit, ...]:
    """All hard and soft rule hits for a document, in a stable order."""
    doc = inp.doc
    if doc.doc_type not in SUPPORTED_TYPES:
        return (
            RuleHit(
                rule=Rule.UNSUPPORTED_TYPE,
                severity=Severity.HARD,
                detail=f"document classified as '{doc.doc_type.value}', which cannot be posted",
            ),
        )

    hits = [
        RuleHit(rule=Rule.VALIDATION_FAILED, severity=Severity.HARD, detail=issue.message)
        for issue in inp.validation_issues
    ]

    critical = sorted(inp.ungrounded_fields & CRITICAL_FIELDS)
    if critical:
        hits.append(
            RuleHit(
                rule=Rule.UNGROUNDED_CRITICAL_FIELD,
                severity=Severity.HARD,
                detail=f"extracted {', '.join(critical)} not found in the document text",
            )
        )

    for match in inp.duplicates:
        rule = _DUPLICATE_RULES[match.kind]
        hits.append(
            RuleHit(
                rule=rule,
                severity=Severity.HARD if match.is_hard else Severity.SOFT,
                detail=f"{match.detail} (see {match.other_doc_id})",
            )
        )

    if inp.outlier is not None and inp.outlier.level is not OutlierLevel.NONE:
        hard = inp.outlier.level is OutlierLevel.HARD
        hits.append(
            RuleHit(
                rule=Rule.AMOUNT_OUTLIER if hard else Rule.MILD_OUTLIER,
                severity=Severity.HARD if hard else Severity.SOFT,
                detail=inp.outlier.explanation,
            )
        )

    if doc.total is not None and doc.total > config.materiality_cap:
        hits.append(
            RuleHit(
                rule=Rule.OVER_MATERIALITY,
                severity=Severity.HARD,
                detail=f"total {doc.total} exceeds the review cap of {config.materiality_cap}",
            )
        )

    if doc.tax > 0 and any(
        item.taxable and code == inp.inventory_account
        for item, code in zip(doc.lines, inp.line_accounts, strict=False)
    ):
        hits.append(
            RuleHit(
                rule=Rule.TAX_ON_RESALE_INVENTORY,
                severity=Severity.HARD,
                detail="sales tax charged on stock bought for resale (normally exempt)",
            )
        )

    if not inp.vendor_known:
        hits.append(
            RuleHit(
                rule=Rule.FIRST_TIME_VENDOR,
                severity=Severity.HARD,
                detail=f"first document from {doc.vendor_name or 'this vendor'}",
            )
        )

    if doc.doc_type is DocumentType.CREDIT_NOTE and inp.credit_reference_found is False:
        hits.append(
            RuleHit(
                rule=Rule.CREDIT_REFERENCE_NOT_FOUND,
                severity=Severity.SOFT,
                detail=(
                    f"referenced document {doc.referenced_document_number} is not in the ledger"
                ),
            )
        )
    return tuple(hits)


def decide(inp: RoutingInput, config: RoutingConfig) -> RoutingDecision:
    hits = evaluate_rules(inp, config)
    factors = {
        hit.rule.value: config.soft_factors[hit.rule]
        for hit in hits
        if hit.severity is Severity.SOFT
    }
    score = min(inp.extraction_confidence, inp.coding_confidence)
    for factor in factors.values():
        score *= factor

    hard = [hit for hit in hits if hit.severity is Severity.HARD]
    if hard:
        outcome = Outcome.NEEDS_REVIEW
        reasons = "; ".join(f"{h.rule.label}: {h.detail.rstrip('.')}" for h in hard)
        summary = f"Needs review. {reasons}."
    elif score >= config.auto_post_threshold:
        outcome = Outcome.AUTO_POST
        summary = (
            f"Auto-post: confidence {score:.2f} meets the threshold "
            f"{config.auto_post_threshold:.2f}."
        )
    else:
        outcome = Outcome.NEEDS_REVIEW
        weak = "extraction" if inp.extraction_confidence <= inp.coding_confidence else "coding"
        soft = f" after {', '.join(factors)}" if factors else ""
        summary = (
            f"Needs review. Confidence {score:.2f}{soft} is below the threshold "
            f"{config.auto_post_threshold:.2f} (weakest: {weak})."
        )

    return RoutingDecision(
        outcome=outcome,
        hits=hits,
        score=score,
        threshold=config.auto_post_threshold,
        extraction_confidence=inp.extraction_confidence,
        coding_confidence=inp.coding_confidence,
        factors=factors,
        summary=summary,
    )
