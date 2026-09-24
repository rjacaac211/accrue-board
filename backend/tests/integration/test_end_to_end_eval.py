"""The end-to-end evaluation harness on Postgres, with the ground-truth oracle for a model."""

import hashlib

import pytest
from sqlalchemy.orm import sessionmaker

from accrueboard.datagen.generate import generate
from accrueboard.datagen.render import render
from accrueboard.datagen.spec import Split, load_anomaly_catalog
from accrueboard.db.models import Client
from accrueboard.db.session import get_engine
from accrueboard.eval.end_to_end import run, summarize
from accrueboard.eval.oracle import Oracle
from accrueboard.eval.report import render as render_report
from accrueboard.llm.client import FakeLLM

from .conftest import unique_spec
from .helpers import EMBEDDER, MODELS
from .test_review_assistant_eval import always_approve

pytestmark = pytest.mark.integration

LIMIT = 12


def test_run_scores_calibrates_and_reviews() -> None:
    spec = unique_spec()
    records = generate(spec, load_anomaly_catalog(), 7)
    oracle = Oracle()
    for split in (Split.VALIDATION, Split.TEST):
        arriving = sorted(
            (r for r in records if r.split is split and r.file is not None),
            key=lambda r: (r.received_at, r.doc_id),
        )[:LIMIT]
        for record in arriving:
            oracle.register(hashlib.sha256(render(record, spec)).hexdigest(), record)
    llm = FakeLLM(oracle, tool_handler=always_approve)
    sessions = sessionmaker(bind=get_engine(), expire_on_commit=False)

    result = run(
        records,
        spec,
        sessions,
        llm=llm,
        models=MODELS,
        assistant_model="claude-sonnet-5",
        embedder=EMBEDDER,
        limit=LIMIT,
        workers=1,
    )

    assert len(result.outcomes) == 2 * LIMIT
    assert result.calibration is not None
    with sessions() as session:
        client = session.get(Client, spec.id)
        assert client is not None
        assert client.config["auto_post_threshold"] == result.calibration.threshold
    assert result.threshold_used["test"] == result.calibration.threshold

    # The oracle reads and codes perfectly, so nothing auto-posted can be wrong.
    for o in result.outcomes:
        if o.true_type != "other":
            assert o.fields
            assert all(o.fields.values())
            assert o.lines_correct == o.lines
        if o.state == "posted":
            assert not o.wrong_to_post
    held = [o for o in result.outcomes if o.state == "needs_review"]
    assert held
    assert all(o.reviewer_action in {"approve", "reject", "block"} for o in held)
    # The assistant investigates held documents in the test split only.
    assert all(bool(o.assistant_expected) == (o.split == "test") for o in held)

    test = summarize(result, Split.TEST)
    assert test["documents"] == LIMIT
    assert test["classification"]["accuracy"]["rate"] == 1.0
    assert test["routing"]["actual"]["auto_posted_wrong"] == 0
    report = render_report(
        {
            "meta": {
                "client": spec.id,
                "seed": 7,
                "models": {"classify": "c", "extract": "e", "verify": "v", "code": "k"},
                "assistant_model": "a",
                "embedder": "hashing",
                "max_escape_rate": 0.01,
            },
            "calibration": test["routing"]["at_threshold"],
            "validation": summarize(result, Split.VALIDATION),
            "test": test,
        }
    )
    assert "## Headline (test split)" in report
    assert "| Classification accuracy | 100.0%" in report
