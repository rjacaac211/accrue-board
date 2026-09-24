# CLAUDE.md

Guidance for AI coding assistants working in this repository.

## Repository hygiene (hard rules)
- **This repository must read as a standalone product.** Never reference private reference
  material, third-party organizations, or anything outside the project's own scope. This applies to:
  - code, comments, docs and config
  - commit messages, branch names, and PR titles and descriptions
- `scripts/check_forbidden_terms.py` enforces this in pre-commit and CI. Its term list is kept
  outside the repository on purpose (`.git/info/forbidden-terms.txt` locally, a CI secret in
  Actions). Never add terms to tracked files.
- Never write absolute local paths into tracked files. Use paths relative to the repo root.
- Commits use Conventional Commits (`feat(pipeline): …`, `fix(ledger): …`, `test(domain): …`).
  The only trailer is `Co-Authored-By: Claude …`. Do not add session links or "generated with"
  footers.
- Work happens on one branch and one PR per milestone (merge commits, not squash). Open the PR
  only after the full local gate, the integration tests and CI pass. The maintainer reviews and
  merges every PR; never merge one yourself.

## Layout
- `backend/src/accrueboard/domain/`: **pure** domain logic (money, validation, journal,
  anomalies, scoring, routing, lifecycle). No IO, no clock, no network. Pyright strict.
- `backend/src/accrueboard/{pipeline,llm,retrieval,services,agents,db,api,datagen,eval}`: the
  adapters and services around the domain. `services/` owns database-backed operations (task
  transitions with audit, ledger posting, seeding).
- `frontend/`: React + Vite + TypeScript single-page app, served by FastAPI in production.
- `docs/adr/`: architecture decision records. Add one for any significant design choice.

## Money and time
- Money is always `decimal.Decimal`. **Never use float for money.** Store it as
  `NUMERIC(14,2)` and send it as a string in JSON.
- Round with `ROUND_HALF_UP` to cents, and only at defined points: line amount, tax, allocation.
- Every journal entry must balance: total debits equal total credits.
- Never call `datetime.now()` directly (ruff bans it). Take an `accrueboard.clock.Clock`, so
  tests can freeze time and the demo can fast-forward it.

## Commands (run from `backend/`)
- `uv run poe check`: the full local gate. Runs lint, typecheck, unit tests, the frontend
  checks and the repo guard.
- `uv run poe fmt`: format and autofix.
- `uv run poe test`: unit tests only. `uv run poe test-integration` needs
  `docker compose up -d db` and `uv run poe migrate` first.
- `uv run poe dev`: run the API with reload. In `frontend/`, `pnpm dev` runs the UI and
  proxies `/api`.
- `python scripts/run_e2e.py` (from the repo root): the Playwright smoke test on a real stack,
  with `LLM_MODE=oracle` standing in for the model. `python scripts/run_demo.py` starts a demo
  stack with real models.
- `uv run accrueboard eval end-to-end --replay`: reproduce the committed evaluation results from
  `data/results/` without an API key. A live run (no `--replay`) costs money: ask first.

## Testing rules
- Write tests first for domain logic, and make them pure unit tests.
- Unit tests never call an LLM. LLM paths use fakes, or recorded responses replayed from
  `tests/fixtures/llm_recordings/`.
- Tests that need Postgres are marked `@pytest.mark.integration`.
- The audit log and ledger are append-only in the database. Integration tests run inside a
  rolled-back transaction and use a unique client id; never try to delete audit or ledger rows.
