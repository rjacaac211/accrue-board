"""The review assistant's prompt, review policy and tool definitions.

Bump ``REVIEW_VERSION`` whenever the prompt, the policy or a tool definition changes: it is part
of every recorded request and of the audit trail.
"""

from typing import Any

from accrueboard.domain.routing import Rule
from accrueboard.llm.types import ToolSpec

REVIEW_VERSION = "review-assistant-v1"
PURPOSE = "review_assistant"
SUBMIT = "submit_review"

REVIEW_SYSTEM_TEMPLATE = """\
You assist the bookkeeping team of {client_name}, {business}. A document was held for human \
review instead of being posted automatically. Investigate it and recommend what the reviewer \
should do. You only advise: a person makes the decision, so be accurate, cite what you found, \
and say so when the evidence is unclear.

How to work:
- Read the case you are given, then use the tools to check each reason the document was held. \
Do not accept a flag at face value, and do not dismiss one without looking.
- Stay within the evidence. Quote document numbers, dates, amounts and accounts exactly as the \
tools return them.
- When you are done, call submit_review once. Keep the summary to two or three sentences a \
busy reviewer can act on.

Review policy of the firm:
- Reject a duplicate: the same bill already recorded, whether as the identical file, with the \
number formatted differently, or re-issued with a new number but the same total and items a \
few days later. Also reject a second credit note for an invoice that was already credited. \
Before rejecting, compare the two documents' vendor, items, amounts and dates. Separate \
shipments on one purchase order, bills from different vendors that share a number, and \
recurring monthly charges are not duplicates.
- Reject anything that is not a bill (a statement of account, a quotation): there is nothing \
to post.
- Hold (block) a document the vendor must correct or the client must confirm: its own \
arithmetic does not add up, it charges sales tax on stock bought for resale, or its amount is \
far outside the vendor's usual range without an explanation on the document. Say in \
`question` exactly what to ask and whom.
- Otherwise recommend approval, even when a policy check fired (a first-time vendor, a total \
over the review cap, a mildly unusual amount, a credit note for an invoice not found): those \
exist so a person looks, not because something is wrong. Mark such a flag as a false positive \
only when the evidence shows it does not apply; a policy check that correctly fired is \
confirmed even when you recommend approval.
- Always give an account for every line, even when recommending reject or hold, so the \
reviewer can see how it would be coded. Choose from the chart below. Prefer how this client \
coded the same kind of item before; an item's nature beats the vendor's usual account when \
they disagree. Equipment over the capitalization threshold is moved to fixed assets \
automatically after approval, so code it to its equipment account.

Chart of accounts (codes you may use):
{chart}"""


def _string() -> dict[str, Any]:
    return {"type": "string"}


def _nullable_string() -> dict[str, Any]:
    return {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


INVESTIGATION_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_document_text",
        description=(
            "The text printed on the document under review, read from the PDF's text layer. "
            "Use it to check an extracted value or wording on the page. Image receipts have "
            "no text layer."
        ),
        input_schema=_object({}),
    ),
    ToolSpec(
        name="vendor_history",
        description=(
            "How this client dealt with a vendor before: accounts its past line items were "
            "coded to, the median document total, and its most recent documents with their "
            "status. Use it for unusual amounts, coding questions and first-time vendors."
        ),
        input_schema=_object({"vendor_name": _string()}),
    ),
    ToolSpec(
        name="similar_transactions",
        description=(
            "Past line items of this client most similar to a description (any vendor), with "
            "the accounts they were coded to. Use it to decide how to code a line."
        ),
        input_schema=_object({"query": _string()}),
    ),
    ToolSpec(
        name="get_document",
        description=(
            "Full details of another document of this client by its id (for example the "
            "earlier document a duplicate flag points to): vendor, number, dates, lines, "
            "totals, status, the accounts it was posted to, and whether its file is identical "
            "to the document under review."
        ),
        input_schema=_object({"document_id": _string()}),
    ),
    ToolSpec(
        name="ledger_lookup",
        description=(
            "Find this client's documents by document number (formatting such as INV- prefixes, "
            "'#' and leading zeros is ignored), optionally for one vendor only. Use it to find "
            "the invoice a credit note refers to, or other documents sharing a number."
        ),
        input_schema=_object({"document_number": _string(), "vendor_name": _nullable_string()}),
    ),
)


def submit_tool(accounts: list[str], line_count: int) -> ToolSpec:
    """The final answer, as a tool whose schema constrains accounts to the client's chart."""
    return ToolSpec(
        name=SUBMIT,
        description=(
            "Submit your recommendation. Call this exactly once, after investigating. "
            f"The document has {line_count} line(s); give one entry per line, numbered from 0."
        ),
        input_schema=_object(
            {
                "action": {"type": "string", "enum": ["approve", "reject", "block"]},
                "summary": _string(),
                "question": {
                    **_nullable_string(),
                    "description": "For block: what to ask, and whom. Otherwise null.",
                },
                "rule_assessments": {
                    "type": "array",
                    "description": "One entry per reason the document was held.",
                    "items": _object(
                        {
                            "rule": {"type": "string", "enum": [r.value for r in Rule]},
                            "verdict": {
                                "type": "string",
                                "enum": ["confirmed", "false_positive", "uncertain"],
                            },
                            "reason": _string(),
                        }
                    ),
                },
                "lines": {
                    "type": "array",
                    "items": _object(
                        {
                            "line": {"type": "integer"},
                            "account": {"type": "string", "enum": accounts},
                            "reason": _string(),
                        }
                    ),
                },
                "evidence": {
                    "type": "array",
                    "description": "The facts your recommendation rests on.",
                    "items": _object(
                        {
                            "source": {
                                "type": "string",
                                "enum": ["case", *(t.name for t in INVESTIGATION_TOOLS)],
                            },
                            "detail": _string(),
                            "document_id": _nullable_string(),
                        }
                    ),
                },
            }
        ),
    )
