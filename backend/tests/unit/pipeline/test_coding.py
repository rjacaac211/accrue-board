from collections import Counter
from functools import cache
from typing import Any

import pytest

from accrueboard.datagen.generate import generate
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import EVAL_SPLITS, load_anomaly_catalog, load_client
from accrueboard.domain.documents import DocumentType
from accrueboard.llm.client import FakeLLM
from accrueboard.llm.types import LLMError, LLMRequest
from accrueboard.pipeline.coding import (
    BOTH_AGREE,
    NO_MODEL_ANSWER,
    UNSUPPORTED,
    VENDOR_AGREES,
    VENDOR_CONFLICT,
    Coder,
    line_confidence,
    vendor_rule,
)
from accrueboard.retrieval.classifier import Prediction
from accrueboard.retrieval.embeddings import HashingEmbedder
from accrueboard.retrieval.knowledge import MemoryKnowledgeStore
from accrueboard.retrieval.seed import client_context, history_entries

SPEC = load_client("fernhill")


@cache
def records() -> tuple[GroundTruth, ...]:
    return tuple(generate(SPEC, load_anomaly_catalog(), 7))


@cache
def seeded_store() -> MemoryKnowledgeStore:
    store = MemoryKnowledgeStore(HashingEmbedder())
    store.add(history_entries(records()))
    return store


def eval_bills() -> list[GroundTruth]:
    return [
        r
        for r in records()
        if r.split in EVAL_SPLITS and r.document.doc_type is not DocumentType.OTHER
    ]


def coder_with(answer: Any) -> tuple[Coder, FakeLLM]:
    llm = FakeLLM(answer)
    return Coder(seeded_store(), llm, "claude-sonnet-5", client_context(SPEC)), llm


def truthful(record: GroundTruth) -> Any:
    def answer(request: LLMRequest) -> dict[str, Any]:
        assert request.purpose == "code"
        return {
            "lines": [
                {"line": i, "account": a, "reason": "matches history"}
                for i, a in enumerate(record.line_accounts)
            ]
        }

    return answer


def record_where(predicate: Any) -> GroundTruth:
    return next(r for r in eval_bills() if predicate(r))


# ------------------------------------------------------------------ pure pieces


def test_vendor_rule_needs_enough_consistent_history() -> None:

    assert vendor_rule(Counter({"1300": 10})) == "1300"
    assert vendor_rule(Counter({"1300": 9, "6100": 1})) == "1300"
    assert vendor_rule(Counter({"1300": 8, "6100": 2})) is None
    assert vendor_rule(Counter({"1300": 2})) is None


@pytest.mark.parametrize(
    ("chosen", "rule", "prediction", "neighbours", "expected"),
    [
        ("1300", "1300", None, [], VENDOR_AGREES),
        ("6100", "1300", None, [], VENDOR_CONFLICT),
        ("5300", None, Prediction("5300", 0.8, ()), ["5300"] * 5, BOTH_AGREE),
        ("5300", None, Prediction("5300", 0.4, ()), ["5300"] * 5, 0.8),
        ("5300", None, Prediction("6100", 0.9, ()), ["5300"] * 4 + ["6100"], 0.7),
        ("5300", None, Prediction("6100", 0.9, ()), ["6100"] * 5, UNSUPPORTED),
        (None, "1300", None, [], NO_MODEL_ANSWER),
    ],
)
def test_line_confidence(
    chosen: str | None,
    rule: str | None,
    prediction: Prediction | None,
    neighbours: list[str],
    expected: float,
) -> None:
    assert line_confidence(
        chosen=chosen, rule=rule, prediction=prediction, neighbour_accounts=neighbours
    ) == pytest.approx(expected)


# ------------------------------------------------------------------ coding documents


def test_consistent_vendor_with_agreeing_model_is_trusted() -> None:
    record = record_where(lambda r: r.vendor_id == "oakridge" and not r.anomalies)
    coder, llm = coder_with(truthful(record))
    result = coder.code(record.document)
    assert result.accounts == record.line_accounts
    assert result.vendor_known
    assert result.vendor_history
    assert all(line.vendor_rule_account == "1300" for line in result.lines)
    assert result.confidence == VENDOR_AGREES
    assert result.call is not None
    assert len(llm.calls) == 1


def test_model_contradicting_vendor_history_is_doubted() -> None:
    record = record_where(lambda r: r.vendor_id == "oakridge" and not r.anomalies)

    def wrong(_: LLMRequest) -> dict[str, Any]:
        return {
            "lines": [
                {"line": i, "account": "6100", "reason": "office?"}
                for i in range(len(record.document.lines))
            ]
        }

    result = coder_with(wrong)[0].code(record.document)
    assert result.accounts == ("6100",) * len(record.document.lines)
    assert result.confidence == VENDOR_CONFLICT


def test_first_time_vendor_has_no_history() -> None:
    record = record_where(lambda r: any(a.type == "first_time_vendor" for a in r.anomalies))
    result = coder_with(truthful(record))[0].code(record.document)
    assert not result.vendor_known
    assert result.vendor_history == {}
    assert all(line.vendor_rule_account is None for line in result.lines)


def test_model_failure_falls_back_with_low_confidence() -> None:
    record = record_where(lambda r: r.vendor_id == "oakridge" and not r.anomalies)

    def broken(_: LLMRequest) -> dict[str, Any]:
        raise LLMError("refused")

    result = coder_with(broken)[0].code(record.document)
    assert result.accounts == ("1300",) * len(record.document.lines)  # vendor rule fallback
    assert result.confidence == NO_MODEL_ANSWER
    assert result.call is None


def test_invalid_model_rows_are_ignored() -> None:
    record = record_where(lambda r: r.vendor_id == "oakridge" and len(r.document.lines) >= 2)

    def partial(_: LLMRequest) -> dict[str, Any]:
        return {
            "lines": [
                {"line": 0, "account": "1300", "reason": "ok"},
                {"line": 99, "account": "1300", "reason": "bad index"},
            ]
        }

    result = coder_with(partial)[0].code(record.document)
    assert result.lines[0].llm_account == "1300"
    assert result.lines[1].llm_account is None
    assert result.confidence == NO_MODEL_ANSWER


def test_prompt_shows_history_and_examples_and_is_deterministic() -> None:
    record = record_where(lambda r: r.vendor_id == "boxcraft" and not r.anomalies)
    coder, llm = coder_with(truthful(record))
    coder.code(record.document)
    coder.code(record.document)
    first, second = llm.calls
    assert first.key == second.key
    text = first.parts[0].text  # type: ignore[union-attr]
    assert "Past line items from this vendor were coded to: 5300 Packaging Supplies" in text
    assert "Similar past line items:" in text
    assert "Fernhill Home Goods LLC" in first.system
    enum = first.output_schema["properties"]["lines"]["items"]["properties"]["account"]["enum"]
    assert "1500" not in enum  # fixed assets come only from the capitalization rule
    assert "2000" not in enum  # never code a purchase to payables
    assert "1300" in enum


def test_classifier_baseline_is_a_meaningful_second_opinion() -> None:
    coder = coder_with(lambda _: {"lines": []})[0]
    correct = total = 0
    for record in eval_bills():
        for predicted, truth in zip(
            coder.baseline_classifier(record.document), record.line_accounts, strict=True
        ):
            correct += predicted == truth
            total += 1
    assert correct / total > 0.85


def test_vendor_rule_baseline_abstains_for_unknown_vendors() -> None:
    record = record_where(lambda r: any(a.type == "first_time_vendor" for a in r.anomalies))
    coder = coder_with(lambda _: {"lines": []})[0]
    assert set(coder.baseline_vendor_rule(record.document)) == {None}
    assert all(p is not None for p in coder.baseline_knn(record.document))
