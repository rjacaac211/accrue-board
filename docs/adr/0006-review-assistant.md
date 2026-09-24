# ADR 0006: The review assistant

- **Status:** accepted
- **Date:** 2026-09-24

## Context
[ADR 0001](0001-fixed-workflow-plus-review-agent.md) keeps the pipeline a fixed workflow and
reserves an agent for one job: investigating a document held for review and recommending what
the reviewer should do. This ADR records how that agent is built.

The requirements:
- **Suggest-only.** It must be impossible for the assistant to change a task's state or post to
  the ledger.
- **Reproducible.** Like the pipeline's model calls, an investigation must replay from
  recordings, with its cost recorded.
- **Harmless when it fails.** A model outage or a bug in the assistant must never undo or block
  the pipeline's work.
- **Measured.** Its recommendations must be scored against the dataset's ground truth.

## Decision

### The loop
- The loop is a LangGraph `StateGraph` with two nodes:
  - `think` asks the model for its next move
  - `act` runs the tools the model asked for
- A conditional edge loops back until the model submits a recommendation or a turn limit is
  reached (8 turns). There is no checkpointer.
- The model is called through the project's own client (`ToolUseRequest` in `llm/types.py`), not
  through a LangChain chat model. Each turn is one request whose canonical form includes the
  whole conversation so far. So the record/replay layer keys, stores and replays an
  investigation turn by turn, exactly like pipeline calls, and every turn's cost lands in
  `llm_calls`.
- **Every turn must call a tool** (`tool_choice: any`), and the answer is itself a tool,
  `submit_review`. Its schema limits accounts to the client's codable chart and rules to known
  rule names. Tool definitions use strict mode.
- **On the last turn the model is forced to call `submit_review`.** A submission that fails
  validation goes back to the model as a tool error it can fix. Examples: the wrong number of
  lines, or a hold without the question to ask. If the turns run out, the run is stored as
  failed.

### The tools
All the tools are plain read-only queries, scoped to the task's client:

| Tool | What it returns |
|---|---|
| `get_document_text` | The PDF text layer of the document under review |
| `vendor_history` | Past accounts, posted totals (median and largest) and recent documents for a vendor |
| `similar_transactions` | The client's most similar past line items, and how they were coded |
| `get_document` | Another document by id: lines, totals, status, posted accounts, journal lines, and whether its file is identical |
| `ledger_lookup` | Documents by normalized document number, optionally for one vendor |

- Compared with the list in ADR 0001, two things changed:
  - `get_duplicate_candidate` became the more general `get_document`. The routing explanation
    already names the flagged document's id, and the same tool serves credit-note references.
  - The chart of accounts is in the system prompt rather than behind a tool. The model needs it
    for every document, so a tool call would only add a turn.
- The review policy is written in the system prompt, as a firm's review playbook:
  - reject duplicates and non-bills
  - hold what the vendor must correct or the client must confirm
  - approve the rest, even when a policy check fired

### Storing the outcome
- The outcome is stored on the task (`tasks.assistant`, JSONB). It holds:
  - the suggestion
  - the pipeline's proposed accounts at the time
  - the trail of tool calls, with each result truncated for storage
  - the model, the prompt version, the number of turns and the cost
- One audit event records the run: actor `review-assistant`, action `assistant_suggested` or
  `assistant_failed`. Its details list every tool call, the request keys and the cost. It changes
  no state, and it sits in the task's hash chain like any other event.
- The task row is not locked while the model works. It is locked only to store the outcome, so
  reviewers are never blocked by an investigation in progress.

### When it runs
- The worker runs it right after routing a document to review, in its own transaction. A
  failure is stored as a failed run. A crash is logged and swallowed. Either way, the
  pipeline's result is already committed.
- `POST /api/tasks/{id}/assistant` runs it again on demand. The reviewer's screen shows the card
  and a "use suggested accounts" button. The button only fills the review form; approving is
  still a separate human action.
- `REVIEW_ASSISTANT=false` turns off the automatic run, and `MODEL_ASSISTANT` chooses the model.

## Consequences
- The assistant cannot act. Its only write paths are its own column, its model-call records and
  one audit event.
- Investigations replay deterministically as long as the tool results are identical. The
  evaluation makes sure they are by running in a fresh database with deterministic ids and a
  fixed clock.
- **Evaluation (`accrueboard eval review-assistant`):**
  - The split runs through the real pipeline, with a ground-truth oracle standing in for
    extraction and coding. So every held document was held by the routing rules, not by a
    reading mistake.
  - The assistant investigates each held document at the moment it is held.
  - Its recommendation is scored against the action the labels and the review policy call for.
  - An amount outlier that occurred naturally, rather than being injected, may be either held or
    approved: its label only says the amount crossed the outlier definition.
- The end-to-end evaluation, with real extraction and coding, is part of the full evaluation
  report.
