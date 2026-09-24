# AccrueBoard

A bookkeeping AI pipeline combined with a live board for coordinating human and AI work.

Documents come in and are classified, extracted, validated, coded to the client's chart of
accounts, and risk-scored. Each one then either posts automatically to a double-entry ledger or
becomes a task for a human reviewer. Every item shows up on a live board, with its stage, how long
it has been sitting there, and a full audit trail of what happened and why. Reviewer corrections
flow back into a per-client knowledge store, so the next similar document is coded with more
confidence.

> **Status:** early development. Built and tested so far:
> - the domain core: money, validation, double-entry posting, duplicate and outlier detection,
>   routing, the task lifecycle and bottleneck rules
> - the synthetic-data generator
> - the pipeline: classification, grounded extraction, the account-coding cascade, routing and
>   posting, run by workers
> - the coordination API: review actions with a feedback loop, database-enforced audit and
>   ledger integrity, and live updates over SSE
> - the web UI: a live board with bottleneck alerts, task review (document viewer, per-field
>   verification, coding signals, routing explanation, audit trail), ledger and knowledge views,
>   and demo controls
>
> - the feedback loop, measured: on vendors first seen after the history, coding goes from 71%
>   (model only) to 100% once reviewers' confirmations are fed back
>   ([docs/evaluation.md](docs/evaluation.md))
> - the review assistant: a LangGraph agent that investigates every held document with
>   read-only tools and recommends approve, reject or hold, with evidence. On the validation
>   split it matches the expected action for 34 of 37 held documents
>   ([docs/evaluation.md](docs/evaluation.md))
>
> The full evaluation report is in progress; unbuilt parts are marked as planned.

## How it works

```
intake → classify → extract → validate → code → score → route ─┬─ auto-post → ledger
                                                               └─ needs review → human → ledger
                                                                         │
                                         corrections → per-client knowledge store
```

- **Fixed workflow where correctness matters.** The pipeline is plain Python with an explicit
  state machine. An LLM (Claude) is used only for narrow jobs: classifying, extracting and
  choosing an account. Every routing decision is deterministic and explainable.
- **An agent where judgment helps.** A tool-using Review Assistant (LangGraph) investigates
  flagged items and suggests a resolution, citing its evidence. It cannot post or approve
  anything.
- **Confidence comes from checks, not from the model's opinion of itself.** It is built from
  signals that can be verified: whether an extracted value actually appears in the document,
  whether the arithmetic adds up, whether independent coding methods agree, and whether a
  second extraction pass agrees with the first.
- **Measured, not asserted.** An evaluation suite over seeded synthetic data reports:
  - extraction and coding accuracy
  - anomaly precision and recall
  - automation rate vs error-escape rate

  *(planned)*

## Data

The system is evaluated on a deterministic synthetic dataset for a fictional US online
retailer:
- 12 months of coded history
- validation and test splits of rendered PDF invoices, receipts (some as noisy PNG scans),
  credit notes, statements and quotes
- seeded anomalies and hard negatives, each defined objectively

Every document is generated from ground truth first, so extraction, coding and anomaly
detection are scored exactly. See [docs/synthetic-data.md](docs/synthetic-data.md) and the
examples in [`data/sample/`](data/sample/).

## Stack

- **Backend:** Python 3.13, FastAPI, SQLAlchemy, Alembic
- **Database:** Postgres with pgvector and pg_trgm
- **Frontend:** React, Vite, TypeScript
- **LLM:** Anthropic Claude
- **Retrieval:** local embeddings (fastembed), Postgres full-text and trigram search; per-client scikit-learn classifier
- **Real-time:** Server-Sent Events driven by Postgres `LISTEN/NOTIFY`

## Quick start

Prerequisites: Docker. For local development you also need [uv](https://docs.astral.sh/uv/),
Node 24 and pnpm.

```bash
cp .env.example .env              # add ANTHROPIC_API_KEY when the pipeline lands
docker compose up --build         # Postgres + app on http://localhost:8000
```

### Local development

```bash
docker compose up -d db                   # Postgres on localhost:5433
cd backend
uv sync
uv run poe migrate                        # apply database migrations
uv run accrueboard datagen                # generate the synthetic dataset into data/generated
uv run accrueboard seed                   # load the client and 12 months of posted history
uv run poe dev                            # API on http://localhost:8000
uv run accrueboard worker                 # process queued documents (needs ANTHROPIC_API_KEY or recordings)
uv run accrueboard eval learning-curve    # coding accuracy as reviewed documents are fed back
uv run accrueboard eval review-assistant  # does the assistant recommend the right action? (model)

cd ../frontend
pnpm install
pnpm dev                                  # UI on http://localhost:5173 (proxies /api)
```

### Checks

```bash
cd backend
uv run poe check              # ruff, pyright, unit tests, frontend lint/typecheck/tests, repo guard
uv run poe test-integration   # needs the database running
```

## Scope

AccrueBoard is a working demonstration of the mechanism, not a production accounting system.
These are intentionally out of scope:
- bank feed ingestion and payment matching
- multi-currency
- OCR of messy scans or handwriting
- authentication
- accounting periods and closing
- sales-side and cost-of-goods recognition
- multi-state sales-tax rules beyond a plausibility check

## License

[MIT](LICENSE)
