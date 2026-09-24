# ADR 0004: Integrity rules enforced by the database

- **Status:** accepted
- **Date:** 2026-09-24

## Context
The audit trail and the ledger are what a reviewer, an auditor or the client relies on. The
domain layer already checks every state change and every journal entry, but checks that live
only in application code can be bypassed by a bug, a script or a manual fix-up.

## Decision
The migration (`0002_core_schema`) puts the rules that must always hold into Postgres itself.

| Rule | Mechanism |
|---|---|
| Audit log is append-only | A trigger rejects `UPDATE`, `DELETE` and `TRUNCATE` on `audit_log` |
| Audit events are tamper-evident | Each event stores the hash of its predecessor for the same task; `verify_chain` recomputes the chain |
| Ledger is append-only | Triggers reject `UPDATE` and `DELETE` on journal entries and lines; corrections are reversing entries |
| Every entry balances | A deferred constraint trigger checks debits against credits when the transaction commits |
| Lines are one-sided | `CHECK (debit >= 0 AND credit >= 0 AND (debit > 0) <> (credit > 0))` |
| Task states are valid | `CHECK (state IN (...lifecycle states...))` |
| Changes are announced | A trigger sends `NOTIFY task_events` on every task insert or update, delivered only on commit |

The hash chain is per task, not global. A global chain would force every writer through a
single lock, while per-task chains let tasks be processed in parallel and still reveal any edit
to a task's history.

Retrieval uses Postgres too:
- a generated `tsvector` column with a GIN index for keyword search
- a trigram index for fuzzy vendor matching
- `pgvector` for embedding similarity (an exact scan, which is fine at this scale)

The in-memory store used by the evaluation implements the same interface. An integration test
checks that the two agree on the top retrieved account for at least 95% of real queries.

## Consequences
- Integration tests prove each rule directly against Postgres, including that the database
  alone refuses an unbalanced entry, and that a forged audit row (written with triggers
  disabled) breaks the hash chain.
- Test isolation relies on transactions that are rolled back, because rows in append-only
  tables cannot be deleted. Starting fresh means recreating the database volume.
- `alembic check` runs in CI, so the ORM models and the migrations cannot drift apart.
