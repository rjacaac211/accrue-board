"""Account coding: a cascade of independent signals, decided by the language model.

For each line item:
1. Vendor memory: how this client coded this vendor before. "Consistent" means at least 3
   past lines, 90% or more to one account.
2. Hybrid retrieval: the most similar confirmed past line items (BM25 + embeddings, RRF).
3. The language model picks the account, seeing the chart of accounts, the vendor history and
   the retrieved examples.
4. A per-client classifier predicts independently, as a second opinion.

Coding confidence reflects how well the signals agree (see ``line_confidence``); the model's own
view of its certainty is not used. The rule of thumb: a choice backed by a consistent vendor
history or by both the classifier and the retrieved neighbours is trusted; a choice that
contradicts a consistent vendor history is the least trusted of all.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from accrueboard.domain.accounts import AccountRole, ChartOfAccounts
from accrueboard.domain.documents import ExtractedDocument, LineItem
from accrueboard.domain.duplicates import normalize_vendor
from accrueboard.llm.client import LLMClient
from accrueboard.llm.prompts import CODE_SYSTEM_TEMPLATE, CODE_VERSION, code_schema
from accrueboard.llm.types import LLMError, LLMRequest, TextPart
from accrueboard.pipeline.extraction import CallRecord
from accrueboard.retrieval.classifier import AccountClassifier, Prediction
from accrueboard.retrieval.knowledge import Hit, KnowledgeStore

MIN_VENDOR_HISTORY = 3
VENDOR_CONSISTENCY = 0.9
EXAMPLES_PER_LINE = 6
NEIGHBOURS_FOR_VOTE = 5

VENDOR_AGREES = 0.97
VENDOR_CONFLICT = 0.4
BOTH_AGREE = 0.92
CLASSIFIER_AGREES = 0.8
NEIGHBOURS_AGREE = 0.7
UNSUPPORTED = 0.5
NO_MODEL_ANSWER = 0.3


@dataclass(frozen=True)
class ClientContext:
    client_id: str
    name: str
    business: str
    chart: ChartOfAccounts

    @property
    def codable_accounts(self) -> list[str]:
        """Accounts a purchase line may be coded to (the model sees only these)."""
        excluded = {
            self.chart.role(AccountRole.BANK),
            self.chart.role(AccountRole.CARD_CLEARING),
            self.chart.role(AccountRole.ACCOUNTS_PAYABLE),
            self.chart.role(AccountRole.FIXED_ASSETS),
        }
        return [
            a.code
            for a in self.chart.accounts
            if a.type.value in ("asset", "expense") and a.code not in excluded
        ]


class LineCoding(BaseModel):
    model_config = ConfigDict(frozen=True)

    line_index: int
    account: str
    reason: str
    llm_account: str | None
    classifier_account: str | None
    classifier_probability: float | None
    vendor_rule_account: str | None
    neighbour_account: str | None
    neighbour_share: float
    neighbour_ids: tuple[str, ...]
    confidence: float


class CodingResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    accounts: tuple[str, ...]
    lines: tuple[LineCoding, ...]
    confidence: float
    vendor_key: str | None
    vendor_known: bool
    vendor_history: dict[str, int]
    call: CallRecord | None


def vendor_rule(history: Counter[str]) -> str | None:
    total = sum(history.values())
    if total < MIN_VENDOR_HISTORY:
        return None
    account, count = history.most_common(1)[0]
    return account if count / total >= VENDOR_CONSISTENCY else None


def neighbour_vote(hits: list[Hit]) -> tuple[str | None, float]:
    top = hits[:NEIGHBOURS_FOR_VOTE]
    if not top:
        return None, 0.0
    counts = Counter(h.entry.account for h in top)
    account, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    return account, count / len(top)


def line_confidence(
    *,
    chosen: str | None,
    rule: str | None,
    prediction: Prediction | None,
    neighbour_accounts: list[str],
) -> float:
    if chosen is None:
        return NO_MODEL_ANSWER
    if rule is not None:
        return VENDOR_AGREES if chosen == rule else VENDOR_CONFLICT
    top = neighbour_accounts[:NEIGHBOURS_FOR_VOTE]
    share = top.count(chosen) / len(top) if top else 0.0
    if prediction is not None and prediction.account == chosen:
        return BOTH_AGREE if prediction.probability >= 0.6 and share >= 0.6 else CLASSIFIER_AGREES
    return NEIGHBOURS_AGREE if share >= 0.6 else UNSUPPORTED


class Coder:
    """Codes documents for one client. Call ``refresh`` after the knowledge store changes."""

    def __init__(
        self, store: KnowledgeStore, llm: LLMClient, model: str, context: ClientContext
    ) -> None:
        self.store = store
        self.llm = llm
        self.model = model
        self.context = context
        self.classifier = AccountClassifier()
        self.refresh()

    def refresh(self) -> None:
        self.classifier.fit(self.store.entries(self.context.client_id))

    # ------------------------------------------------------------------ signals

    def vendor_key(self, doc: ExtractedDocument) -> tuple[str | None, bool]:
        raw = normalize_vendor(doc.vendor_name)
        if raw is None:
            return None, False
        resolved = self.store.resolve_vendor(self.context.client_id, raw)
        return (resolved or raw), resolved is not None

    def examples(self, doc: ExtractedDocument, item: LineItem) -> list[Hit]:
        query = f"{doc.vendor_name or ''} | {item.description}"
        return self.store.search(self.context.client_id, query, EXAMPLES_PER_LINE)

    # ------------------------------------------------------------------ baselines (evaluation)

    def baseline_vendor_rule(self, doc: ExtractedDocument) -> list[str | None]:
        """Most common past account for the vendor, whatever its consistency."""
        key, known = self.vendor_key(doc)
        if key is None or not known:
            return [None] * len(doc.lines)
        history = self.store.vendor_accounts(self.context.client_id, key)
        top = history.most_common(1)[0][0] if history else None
        return [top] * len(doc.lines)

    def baseline_classifier(self, doc: ExtractedDocument) -> list[str | None]:
        out: list[str | None] = []
        for item in doc.lines:
            prediction = self.classifier.predict(doc.vendor_name or "", item.description)
            out.append(prediction.account if prediction else None)
        return out

    def baseline_knn(self, doc: ExtractedDocument) -> list[str | None]:
        return [neighbour_vote(self.examples(doc, item))[0] for item in doc.lines]

    # ------------------------------------------------------------------ prompt

    def _system(self) -> str:
        chart = self.context.chart
        lines = []
        for code in self.context.codable_accounts:
            account = chart.get(code)
            note = f" - {account.description}" if account.description else ""
            lines.append(f"{code} {account.name}{note}")
        return CODE_SYSTEM_TEMPLATE.format(
            client_name=self.context.name, business=self.context.business, chart="\n".join(lines)
        )

    def _user(
        self, doc: ExtractedDocument, history: Counter[str], per_line: list[list[Hit]]
    ) -> str:
        chart = self.context.chart
        out = [
            f"Document: {doc.doc_type.value.replace('_', ' ')} from {doc.vendor_name}, "
            f"issued {doc.issue_date}."
        ]
        if history:
            summary = ", ".join(
                f"{code} {chart.get(code).name} x{n}"
                for code, n in sorted(history.items(), key=lambda item: (-item[1], item[0]))
            )
            out.append(f"Past line items from this vendor were coded to: {summary}.")
        else:
            out.append("There are no past documents from this vendor.")
        for i, (item, hits) in enumerate(zip(doc.lines, per_line, strict=True)):
            out.append(
                f"\nLine {i}: {item.description} | quantity {item.quantity} | amount {item.amount}"
            )
            if hits:
                out.append("Similar past line items:")
                out += [
                    f"- {h.entry.vendor_name} | {h.entry.description} -> "
                    f"{h.entry.account} {chart.get(h.entry.account).name}"
                    for h in hits
                ]
        return "\n".join(out)

    # ------------------------------------------------------------------ coding

    def code(self, doc: ExtractedDocument) -> CodingResult:
        client = self.context.client_id
        key, known = self.vendor_key(doc)
        history = self.store.vendor_accounts(client, key) if key and known else Counter[str]()
        rule = vendor_rule(history)
        per_line = [self.examples(doc, item) for item in doc.lines]

        choices: dict[int, tuple[str, str]] = {}
        call: CallRecord | None = None
        if doc.lines:
            codes = self.context.codable_accounts
            request = LLMRequest(
                purpose="code",
                prompt_version=CODE_VERSION,
                model=self.model,
                system=self._system(),
                parts=(TextPart(text=self._user(doc, history, per_line)),),
                output_schema=code_schema(codes),
                max_tokens=2048,
            )
            try:
                response = self.llm.complete(request)
                call = CallRecord.of(request, response)
                choices = self._parse(response.output, len(doc.lines), codes)
            except LLMError:
                choices = {}

        lines: list[LineCoding] = []
        for i, (item, hits) in enumerate(zip(doc.lines, per_line, strict=True)):
            prediction = self.classifier.predict(doc.vendor_name or "", item.description)
            neighbour, share = neighbour_vote(hits)
            llm_choice = choices.get(i)
            chosen = llm_choice[0] if llm_choice else None
            fallback = rule or (prediction.account if prediction else None) or neighbour
            account = chosen or fallback or self.context.codable_accounts[0]
            confidence = line_confidence(
                chosen=chosen,
                rule=rule,
                prediction=prediction,
                neighbour_accounts=[h.entry.account for h in hits],
            )
            lines.append(
                LineCoding(
                    line_index=i,
                    account=account,
                    reason=llm_choice[1] if llm_choice else "no model answer; used fallback",
                    llm_account=chosen,
                    classifier_account=prediction.account if prediction else None,
                    classifier_probability=prediction.probability if prediction else None,
                    vendor_rule_account=rule,
                    neighbour_account=neighbour,
                    neighbour_share=share,
                    neighbour_ids=tuple(h.entry.entry_id for h in hits[:NEIGHBOURS_FOR_VOTE]),
                    confidence=confidence,
                )
            )

        return CodingResult(
            accounts=tuple(line.account for line in lines),
            lines=tuple(lines),
            confidence=min((line.confidence for line in lines), default=0.0),
            vendor_key=key,
            vendor_known=known,
            vendor_history=dict(history),
            call=call,
        )

    @staticmethod
    def _parse(
        output: dict[str, Any], n_lines: int, codes: list[str]
    ) -> dict[int, tuple[str, str]]:
        choices: dict[int, tuple[str, str]] = {}
        for row in output.get("lines", []):
            index, account = row.get("line"), row.get("account")
            if isinstance(index, int) and 0 <= index < n_lines and account in codes:
                choices.setdefault(index, (account, str(row.get("reason", ""))))
        return choices
