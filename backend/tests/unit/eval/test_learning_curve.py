from functools import cache

import pytest

from accrueboard.datagen.generate import generate
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import Split, load_anomaly_catalog, load_client
from accrueboard.eval.learning_curve import ALL, CurvePoint, learning_curve, to_markdown
from accrueboard.llm.client import FakeLLM
from accrueboard.retrieval.embeddings import HashingEmbedder

SPEC = load_client("fernhill")


@cache
def records() -> tuple[GroundTruth, ...]:
    return tuple(generate(SPEC, load_anomaly_catalog(), 7))


def point(points: list[CurvePoint], step: int, method: str) -> CurvePoint:
    return next(p for p in points if p.reviewed_documents == step and p.method == method)


@pytest.fixture(scope="module")
def new_vendor_curve() -> list[CurvePoint]:
    return learning_curve(
        list(records()), SPEC, HashingEmbedder(), steps=[0, ALL], new_vendors_only=True
    )


def test_steps_resolve_all_and_clip(new_vendor_curve: list[CurvePoint]) -> None:
    feedback = [
        r for r in records() if r.split is Split.VALIDATION and r.document.doc_type.value != "other"
    ]
    steps = sorted({p.reviewed_documents for p in new_vendor_curve})
    assert steps == [0, len(feedback)]
    assert {p.method for p in new_vendor_curve} == {"vendor_rule", "classifier", "knn"}


def test_feedback_teaches_new_vendors(new_vendor_curve: list[CurvePoint]) -> None:
    before = point(new_vendor_curve, 0, "classifier")
    after = point(
        new_vendor_curve, max(p.reviewed_documents for p in new_vendor_curve), "classifier"
    )
    assert before.new_vendor.lines > 0
    assert before.history_vendor.lines == 0  # only new-vendor documents were evaluated
    assert before.overall.accuracy == 0.0  # the classifier cannot predict an unseen vendor
    assert after.overall.accuracy is not None
    assert after.overall.accuracy > 0.0
    assert after.knowledge_entries > before.knowledge_entries
    assert point(new_vendor_curve, 0, "vendor_rule").abstained == before.overall.lines


def test_cascade_runs_with_a_model_and_limit() -> None:
    llm = FakeLLM(lambda _: {"lines": []})  # the coder falls back when the model gives nothing
    points = learning_curve(
        list(records()), SPEC, HashingEmbedder(), steps=[0], llm=llm, test_limit=5
    )
    cascade = point(points, 0, "cascade")
    assert cascade.overall.lines > 0
    assert len(llm.calls) == 5
    assert all(call.purpose == "code" for call in llm.calls)


def test_markdown_report(new_vendor_curve: list[CurvePoint]) -> None:
    table = to_markdown(new_vendor_curve)
    assert table.startswith("| Reviewed docs |")
    assert "| classifier | 0.0% (" in table
    assert "— (0)" in table  # no history-vendor lines in this run
