# Architecture

## Processes
| Process | Role |
|---|---|
| `db` | Postgres 17 with the `vector` and `pg_trgm` extensions. Holds task state, the ledger, the audit log, the knowledge store and the work queue. |
| `app` | FastAPI. Serves the REST API, the SSE event stream, and the built frontend. |
| `worker` *(planned)* | Claims queued tasks with `SELECT … FOR UPDATE SKIP LOCKED`, runs the pipeline, and runs the periodic bottleneck check. |

## Task lifecycle
```
Queued → Processing → AutoApproved → Posted
                    → NeedsReview → Approved → Posted
                                  → Rejected
                                  ⇄ Blocked
                    → Blocked
                    → Failed → Queued (retry)
Posted → reopen: a reversing journal entry is posted, then → NeedsReview
```
- Transitions not in this table are errors.
- A human decision is final. An edit is re-validated (arithmetic, balance, capitalization) but is
  not re-scored.
- Every transition is one database transaction that updates the task, appends an audit row, and
  sends `NOTIFY`. Because `NOTIFY` is delivered only on commit, the live board never shows a
  change that was rolled back.

## Pipeline
1. **Classify** into invoice, receipt, credit note or other.
2. **Extract** the fields using a structured-output schema.
3. **Ground** each extracted value against the document's text layer. A second extraction pass
   runs when something doesn't check out.
4. **Validate** the arithmetic, tax plausibility, dates and required fields.
5. **Code** each line to an account:
   - vendor memory first
   - then hybrid retrieval over the client's confirmed history (full-text plus embeddings)
   - then the LLM chooses an account, with a per-client classifier as an independent second opinion
6. **Score:**
   1. Hard rules send an item to review regardless of its score.
   2. Otherwise the confidence score is the weaker of extraction and coding confidence.
   3. Soft signals, such as a possible near-duplicate, reduce it further.
7. **Route.** Items at or above a calibrated threshold auto-post; the rest go to review.
8. **Post** a balanced double-entry journal entry (accrual basis).

See [ADR 0001](adr/0001-fixed-workflow-plus-review-agent.md) for why the pipeline is a fixed
workflow and only review assistance is an agent.
