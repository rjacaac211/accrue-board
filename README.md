# AccrueBoard

A bookkeeping AI pipeline combined with a live board for coordinating human and AI work.

Documents come in and are classified, extracted, validated, coded to the client's chart of
accounts, and risk-scored. Each one then either posts automatically to a double-entry ledger or
becomes a task for a human reviewer. Every item shows up on a live board, with its stage, how long
it has been sitting there, and a full audit trail of what happened and why. Reviewer corrections
flow back into a per-client knowledge store, so the next similar document is coded with more
confidence.

> **Status:** early development. The domain core (money, validation, double-entry posting,
> duplicate and outlier detection, routing, task lifecycle and bottleneck rules) is built and
> tested. The pipeline, UI and evaluation are in progress; unbuilt parts are marked as planned.

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
  anything. *(planned)*
- **Confidence comes from checks, not from the model's opinion of itself.** It is built from
  signals that can be verified: whether an extracted value actually appears in the document,
  whether the arithmetic adds up, whether independent coding methods agree, and whether a
  second extraction pass agrees with the first. *(planned)*
- **Measured, not asserted.** An evaluation suite over seeded synthetic data reports:
  - extraction and coding accuracy
  - anomaly precision and recall
  - automation rate vs error-escape rate

  *(planned)*

## Stack

- **Backend:** Python 3.13, FastAPI, SQLAlchemy, Alembic
- **Database:** Postgres with pgvector and pg_trgm
- **Frontend:** React, Vite, TypeScript
- **LLM:** Anthropic Claude
- **Retrieval:** local embeddings (fastembed) with full-text search; per-client scikit-learn classifier *(planned)*
- **Real-time:** Server-Sent Events driven by Postgres `LISTEN/NOTIFY` *(planned)*

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
uv run poe dev                            # API on http://localhost:8000

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
