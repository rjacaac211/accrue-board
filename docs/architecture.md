# Architecture

## Processes
| Process | Role |
|---|---|
| `db` | Postgres 17 with the `vector` and `pg_trgm` extensions. Holds task state, the ledger, the audit log, the knowledge store and the work queue. |
| `app` | FastAPI. Serves the REST API, the SSE event stream (`/api/events`), and the built frontend. On start it applies migrations and, on a fresh installation, generates the dataset and seeds the demo client (`accrueboard bootstrap`). |
| `worker` | Claims queued tasks with `SELECT … FOR UPDATE SKIP LOCKED` and a lease, runs the pipeline, requeues tasks whose lease expired, and runs the review assistant on documents held for review. |

## Model modes
`LLM_MODE` chooses where model answers come from:

| Mode | Behaviour |
|---|---|
| `auto` (default) | Serve a recorded response if one exists; otherwise call the API and record it |
| `live` | Always call the API; record nothing |
| `record` | Always call the API and overwrite the recording |
| `replay` | Recorded responses only; needs no key (a missing recording is an error) |
| `oracle` | No model: answers come from the synthetic dataset's ground truth. For the browser smoke test and keyless demos; the review assistant is unavailable |

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
9. **Investigate.** A document held for review is investigated by the review assistant, a
   LangGraph agent with read-only tools. It recommends approve, reject or hold, with an account
   per line and the evidence it found. It only suggests: a person decides.

See [ADR 0001](adr/0001-fixed-workflow-plus-review-agent.md) for why the pipeline is a fixed
workflow and only review assistance is an agent, and
[ADR 0004](adr/0004-database-enforced-integrity.md) for the integrity rules enforced by Postgres.
[ADR 0006](adr/0006-review-assistant.md) describes the review assistant.

## API
| Endpoint | Purpose |
|---|---|
| `GET /api/clients/{id}/board` | Active tasks and recently settled ones, with age and alert level |
| `GET /api/tasks/{id}` | Everything about one task: extraction checks, coding signals, routing decision, audit trail (with chain check), journal entries, model calls and cost |
| `GET /api/tasks/{id}/file` | The original document |
| `POST /api/tasks/{id}/approve` | Approve, optionally with a corrected document or accounts; posts and feeds the knowledge store |
| `POST /api/tasks/{id}/assistant` | Run the review assistant on a held task (suggest-only) and store its recommendation |
| `POST /api/tasks/{id}/{reject,block,unblock,reopen,retry,assign}` | Other review actions (reject, block and reopen need a reason) |
| `GET /api/clients/{id}/bottlenecks` | Age-in-stage alerts and review congestion at the current (shared) time |
| `GET /api/clients/{id}/ledger`, `/knowledge`, `/stats` | Journal and trial balance, knowledge entries, processing statistics |
| `POST /api/clients/{id}/documents` | Upload a document |
| `GET /api/events?client_id=` | Server-Sent Events: task changes and a heartbeat |
| `POST /api/demo/...` | With `DEMO_MODE=true`: advance/reset the shared clock, drip-feed generated documents |

See [ADR 0005](adr/0005-live-updates-and-demo-clock.md).
