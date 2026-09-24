"""Run the review assistant on a task and store what it found.

The assistant is suggest-only: it never changes a task's state. Its outcome is stored on the
task (``tasks.assistant``) and summarised in one audit event by the ``review-assistant`` actor,
listing every tool it called; its model calls are recorded with their cost like the pipeline's.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from accrueboard.agents.review_assistant.graph import (
    MAX_TURNS,
    Investigation,
    Step,
    investigate,
)
from accrueboard.agents.review_assistant.prompts import REVIEW_SYSTEM_TEMPLATE, REVIEW_VERSION
from accrueboard.agents.review_assistant.tools import Suggestion, Toolbox
from accrueboard.clock import Clock
from accrueboard.db.models import Client, LLMCall, Task
from accrueboard.domain.accounts import ChartOfAccounts
from accrueboard.domain.documents import ExtractedDocument
from accrueboard.domain.lifecycle import Actor, TaskState
from accrueboard.llm.client import ToolUseClient
from accrueboard.llm.types import LLMError
from accrueboard.pipeline.coding import ClientContext
from accrueboard.pipeline.extraction import CallRecord
from accrueboard.retrieval.embeddings import Embedder
from accrueboard.retrieval.pg_store import PgKnowledgeStore
from accrueboard.services.clients import load_chart
from accrueboard.services.tasks import record_event

log = logging.getLogger(__name__)

ACTOR = "review-assistant"
REVIEWABLE = (TaskState.NEEDS_REVIEW.value, TaskState.BLOCKED.value)


class AssistantRun(BaseModel):
    """What is stored on the task after the assistant ran."""

    status: Literal["done", "failed"]
    suggestion: Suggestion | None
    error: str | None = None
    proposed_accounts: list[str]
    """The pipeline's accounts when the assistant ran, to show where it differs."""
    steps: list[Step]
    notes: list[str]
    model: str
    prompt_version: str
    model_turns: int
    cost_usd: Decimal
    replayed: bool
    ran_at: datetime


class AssistantUnavailableError(ValueError):
    """The task cannot be investigated (the message says why)."""


# ---------------------------------------------------------------------------- the case brief


def system_prompt(context: ClientContext) -> str:
    chart = context.chart
    lines = []
    for code in context.codable_accounts:
        account = chart.get(code)
        note = f" - {account.description}" if account.description else ""
        lines.append(f"{code} {account.name}{note}")
    return REVIEW_SYSTEM_TEMPLATE.format(
        client_name=context.name, business=context.business, chart="\n".join(lines)
    )


def _name(chart: ChartOfAccounts, code: str | None) -> str:
    if code is None:
        return "none"
    return f"{code} {chart.get(code).name}" if chart.has(code) else code


def case_brief(task: Task, chart: ChartOfAccounts) -> str:
    """The case as the reviewer's screen shows it: flags, document, proposed coding."""
    document = task.document
    routing: dict[str, Any] = task.routing or {}
    coding: dict[str, Any] = task.coding or {}
    out = [
        f"Document {document.id} (file {document.filename}, received "
        f"{document.received_at.date().isoformat()}) was held for review.",
        "",
        "Why it was held:",
    ]
    hits = routing.get("hits") or []
    out += [f"- [{h['severity']}] {h['rule']}: {h['detail']}" for h in hits]
    if not hits:
        out.append("- no rule fired; the confidence score was below the auto-post threshold")
    if routing:
        out.append(
            f"Score {routing.get('score', 0):.2f} against threshold "
            f"{routing.get('threshold', 0):.2f} (extraction confidence "
            f"{routing.get('extraction_confidence', 0):.2f}, coding confidence "
            f"{routing.get('coding_confidence', 0):.2f})."
        )

    if document.extracted is None:
        out += ["", "Nothing could be extracted from this document."]
        return "\n".join(out)
    doc = ExtractedDocument.model_validate(document.extracted)
    out += ["", f"The document as extracted ({doc.doc_type.value.replace('_', ' ')}):"]
    fields = {
        "vendor": doc.vendor_name,
        "vendor state": doc.vendor_state,
        "number": doc.document_number,
        "issue date": doc.issue_date,
        "due date": doc.due_date,
        "PO number": doc.po_number,
        "refers to document": doc.referenced_document_number,
        "payment method": doc.payment_method.value if doc.payment_method else None,
    }
    out += [f"- {label}: {value}" for label, value in fields.items() if value is not None]

    ungrounded = set((task.extraction or {}).get("ungrounded") or [])
    if ungrounded:
        out.append(f"- not found in the document text: {', '.join(sorted(ungrounded))}")

    line_codings = {c["line_index"]: c for c in coding.get("lines") or []}
    proposed = list(coding.get("accounts") or [])
    posted = list(task.line_accounts or [])
    out.append("Lines (proposed account, and the signals behind it):")
    for i, item in enumerate(doc.lines):
        c = line_codings.get(i, {})
        signals = [
            f"vendor rule {_name(chart, c.get('vendor_rule_account'))}",
            f"classifier {_name(chart, c.get('classifier_account'))}",
            f"similar items {_name(chart, c.get('neighbour_account'))} "
            f"({c.get('neighbour_share', 0):.0%})",
            f"model {_name(chart, c.get('llm_account'))}",
        ]
        after = (
            f"; moved to {_name(chart, posted[i])} by the capitalization rule"
            if i < len(posted) and i < len(proposed) and posted[i] != proposed[i]
            else ""
        )
        out.append(
            f"{i}. {item.description} | qty {item.quantity} x {item.unit_price} = {item.amount}"
            f"{' | taxable' if item.taxable else ''}"
            f" -> {_name(chart, proposed[i] if i < len(proposed) else None)}{after}"
            f" [{'; '.join(signals)}; confidence {c.get('confidence', 0):.2f}]"
        )
    out.append(
        f"Subtotal {doc.subtotal}, discount {doc.discount}, shipping {doc.shipping}, "
        f"tax {doc.tax}{f' (rate {doc.tax_rate})' if doc.tax_rate is not None else ''}, "
        f"total {doc.total}."
    )
    history = coding.get("vendor_history") or {}
    if history:
        counts = ", ".join(f"{_name(chart, code)} x{n}" for code, n in history.items())
        out.append(f"This vendor's past line items were coded to: {counts}.")
    elif coding:
        out.append("This vendor has no coding history with this client.")
    return "\n".join(out)


# ---------------------------------------------------------------------------- running


class ReviewAssistant:
    """Investigates tasks held for review. One instance per process; runs are independent."""

    def __init__(
        self,
        llm: ToolUseClient,
        model: str,
        embedder: Embedder,
        clock: Clock,
        *,
        max_turns: int = MAX_TURNS,
    ) -> None:
        self.llm = llm
        self.model = model
        self.embedder = embedder
        self.clock = clock
        self.max_turns = max_turns

    def run(self, session: Session, task_id: str) -> AssistantRun:
        """Investigate a task in the caller's transaction and store the outcome on it.

        The task is not locked while the model works (reviewers can keep acting on it); the
        row is locked only to store the outcome.
        """
        task = session.get(Task, task_id)
        if task is None:
            raise AssistantUnavailableError(f"unknown task {task_id!r}")
        if task.state not in REVIEWABLE:
            raise AssistantUnavailableError(
                f"task is {task.state}; the assistant only looks at tasks held for review"
            )
        client = session.get(Client, task.client_id)
        if client is None:
            raise AssistantUnavailableError("task has no client")
        chart = load_chart(session, client.id)
        context = ClientContext(client.id, client.name, client.business, chart)
        doc = (
            ExtractedDocument.model_validate(task.document.extracted)
            if task.document.extracted
            else None
        )
        toolbox = Toolbox(
            session=session,
            task=task,
            chart=chart,
            store=PgKnowledgeStore(session, self.embedder, now=self.clock.now),
        )
        try:
            result = investigate(
                self.llm,
                model=self.model,
                system=system_prompt(context),
                brief=case_brief(task, chart),
                toolbox=toolbox,
                accounts=context.codable_accounts,
                line_count=len(doc.lines) if doc else 0,
                max_turns=self.max_turns,
            )
            error = None if result.suggestion else "no valid recommendation within the turn limit"
        except LLMError as exc:
            log.warning("review assistant failed on %s: %s", task.id, exc)
            result = Investigation(None, [], [], 0, [])
            error = f"{type(exc).__name__}: {exc}"
        return self._store(session, task, result, error)

    def _store(
        self, session: Session, task: Task, result: Investigation, error: str | None
    ) -> AssistantRun:
        session.refresh(task, with_for_update=True)
        now = self.clock.now()
        run = AssistantRun(
            status="done" if result.suggestion else "failed",
            suggestion=result.suggestion,
            error=error,
            proposed_accounts=list((task.coding or {}).get("accounts") or []),
            steps=result.steps,
            notes=result.notes,
            model=self.model,
            prompt_version=REVIEW_VERSION,
            model_turns=result.model_turns,
            cost_usd=sum((c.cost_usd for c in result.calls), Decimal(0)),
            replayed=bool(result.calls) and all(c.replayed for c in result.calls),
            ran_at=now,
        )
        task.assistant = run.model_dump(mode="json")
        task.updated_at = now  # announces the new suggestion to live views
        _record_calls(session, task.id, result.calls, now)
        suggestion = result.suggestion
        record_event(
            session,
            task,
            now=now,
            actor=ACTOR,
            actor_kind=Actor.MACHINE,
            action="assistant_suggested" if suggestion else "assistant_failed",
            details={
                "suggested_action": suggestion.action.value if suggestion else None,
                "suggested_accounts": suggestion.accounts if suggestion else None,
                "tool_calls": [{"tool": s.tool, "input": s.input} for s in result.steps],
                "model": self.model,
                "prompt_version": REVIEW_VERSION,
                "request_keys": [c.request_key for c in result.calls],
                "cost_usd": str(run.cost_usd),
                "error": error,
            },
        )
        return run


def _record_calls(session: Session, task_id: str, calls: list[CallRecord], now: datetime) -> None:
    session.add_all(
        LLMCall(
            task_id=task_id,
            purpose=c.purpose,
            prompt_version=c.prompt_version,
            model=c.model,
            request_key=c.request_key,
            cost_usd=c.cost_usd,
            latency_ms=c.latency_ms,
            input_tokens=c.input_tokens,
            output_tokens=c.output_tokens,
            replayed=c.replayed,
            created_at=now,
        )
        for c in calls
    )
