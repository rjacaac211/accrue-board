# AccrueBoard

A bookkeeping AI pipeline combined with a live board for coordinating human and AI work.

Documents come in and are classified, extracted, validated, coded to the client's chart of
accounts, and risk-scored. Each one then either posts automatically to a double-entry ledger or
becomes a task for a human reviewer. Every item shows up on a live board, with its stage, how long
it has been sitting there, and a full audit trail of what happened and why. Reviewer corrections
flow back into a per-client knowledge store, so the next similar document is coded with more
confidence.

## Results

Measured end to end on the retailer's 307 held-out test documents with real models (Wilson 95% intervals in
brackets). Full report: [docs/eval-results.md](docs/eval-results.md).

| | |
|---|---:|
| Documents posted without a person | **79.2%** (243/307) [74%, 83%] |
| Auto-posted documents that were wrong | **0 of 243** [0%, 2%] |
| Documents that must not post as read, stopped for review | **39 of 39** [91%, 100%] |
| Bills extracted with every field exactly right | 96.4% [94%, 98%] |
| Line items coded to the correct account | 98.3% [97%, 99%] |
| Review assistant recommends the expected action on held documents | 87.5% (56/64) [77%, 94%] |
| Model cost per document (including the assistant) | $0.022 |

- The auto-post threshold (0.776) was calibrated on a separate validation split: the most
  automation that keeps auto-posted documents at most 1% wrong.
- Every number can be reproduced offline, without an API key:
  `uv run accrueboard eval end-to-end --replay`.
- The data is synthetic, so the numbers show the mechanism works, not how it would do on a
  real client's paperwork. See [Scope](#scope) and [docs/evaluation.md](docs/evaluation.md).
  The evaluation also caught a real bookkeeping bug before any number was reported.

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
- **Nothing slips through quietly.** The board flags work waiting too long in any stage. An
  unpaid invoice nearing its due date while it waits on a person is flagged too, and once it
  is due it is escalated to a senior reviewer, with the reason in the audit trail.
- **Per client, and tested as such.** Two fictional clients (an online retailer and a
  remodeling contractor) have their own charts, vendors, knowledge stores and classifiers. A
  shared vendor is coded differently for each, and a test checks that nothing crosses over.
- **Measured, not asserted.** An evaluation suite over seeded synthetic data reports:
  - extraction and coding accuracy
  - anomaly precision and recall
  - automation against error escape
  - how the feedback loop and the review assistant perform

  Every reported number replays offline from committed recordings.

## Data

The system is evaluated on deterministic synthetic datasets for two fictional US clients: an
online retailer, and a remodeling contractor with a construction chart of accounts. Each has:
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
cp .env.example .env              # add your ANTHROPIC_API_KEY
docker compose up --build         # Postgres, the app and a worker on http://localhost:8000
```

On first start the app generates the synthetic dataset and seeds the demo client (a fictional
online retailer with 12 months of history). Open the board and press **Feed 5** to send it
documents. See [docs/demo.md](docs/demo.md) for a guided walkthrough.

**No API key?** Add `LLM_MODE=oracle` to `.env`. The model is then replaced by the synthetic
dataset's own ground truth, so the pipeline, board and review flow all work offline, but the
review assistant is unavailable.

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
uv run accrueboard eval end-to-end --replay  # reproduce the reported results offline

cd ../frontend
pnpm install
pnpm dev                                  # UI on http://localhost:5173 (proxies /api)
```

### Checks

```bash
cd backend
uv run poe check              # ruff, pyright, unit tests, frontend lint/typecheck/tests, repo guard
uv run poe test-integration   # needs the database running
cd ..
python scripts/run_e2e.py     # browser smoke test on a real stack (offline model; needs pnpm build)
python scripts/run_demo.py    # a fresh demo stack with real models on http://localhost:8020
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

The evaluation uses synthetic documents for one fictional client, generated from known ground
truth so every result can be checked exactly. The documents are clean renders and phone-style
scans, not real-world paperwork, so treat the numbers as evidence that the mechanism works.

## License

[MIT](LICENSE)
