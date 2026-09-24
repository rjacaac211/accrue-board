from decimal import Decimal
from functools import cache

from accrueboard.agents.review_assistant.tools import SuggestedAction
from accrueboard.datagen.generate import generate
from accrueboard.datagen.records import AnomalyLabel, GroundTruth
from accrueboard.datagen.spec import EVAL_SPLITS, load_anomaly_catalog, load_client
from accrueboard.domain.routing import Rule
from accrueboard.eval.review_assistant import (
    POLICY,
    Case,
    expected_actions,
    summarize,
    to_markdown,
)

A = SuggestedAction


@cache
def records() -> tuple[GroundTruth, ...]:
    return tuple(generate(load_client("fernhill"), load_anomaly_catalog(), 7))


def labelled(*labels: AnomalyLabel) -> GroundTruth:
    base = next(r for r in records() if r.split in EVAL_SPLITS)
    return base.model_copy(update={"anomalies": labels})


def label(kind: str, *, injected: bool = True) -> AnomalyLabel:
    return AnomalyLabel(type=kind, expected_rule=Rule.AMOUNT_OUTLIER, injected=injected)


def test_policy_covers_every_anomaly_type() -> None:
    catalog = load_anomaly_catalog()
    assert {a.id for a in catalog.anomalies} == set(POLICY)


def test_expected_actions() -> None:
    assert expected_actions(labelled()) == {A.APPROVE}
    assert expected_actions(labelled(label("first_time_vendor"))) == {A.APPROVE}
    assert expected_actions(labelled(label("near_duplicate"))) == {A.REJECT}
    assert expected_actions(labelled(label("amount_outlier"))) == {A.BLOCK}
    assert expected_actions(labelled(label("amount_outlier", injected=False))) == {
        A.APPROVE,
        A.BLOCK,
    }
    # The strictest label wins.
    assert expected_actions(labelled(label("first_time_vendor"), label("arithmetic_error"))) == {
        A.BLOCK
    }
    assert expected_actions(
        labelled(label("tax_on_resale_inventory"), label("exact_file_duplicate"))
    ) == {A.REJECT}


def case(anomalies: list[str], expected: list[str], suggested: str | None) -> Case:
    return Case(
        doc_id="d",
        anomalies=anomalies,
        rules=[],
        expected=expected,
        suggested=suggested,
        agrees=suggested in expected,
        lines=2,
        lines_correct=2 if suggested else 0,
        proposed_correct=2,
        cost_usd=Decimal("0.02"),
        model_turns=2,
        tools=["get_document", "submit_review"],
        replayed=False,
    )


def test_summary_and_report() -> None:
    summary = summarize(
        [
            case(["near_duplicate"], ["reject"], "reject"),
            case([], ["approve"], "approve"),
            case([], ["approve"], "block"),
            case(["arithmetic_error"], ["block"], None),
        ]
    )
    assert summary.cases == 4
    assert summary.agreement.agree == 2
    assert summary.failed == 1
    assert summary.by_anomaly["none"].rate == 0.5
    assert summary.by_expected["block"].cases == 1
    assert summary.lines_correct == 6
    assert summary.cost_usd == Decimal("0.08")
    assert summary.tool_use == {"get_document": 4, "submit_review": 4}
    report = to_markdown(summary)
    assert "Recommendation matches the expected action: 50.0% (2/4)" in report
    assert "| near_duplicate | 100.0% (1/1) |" in report
