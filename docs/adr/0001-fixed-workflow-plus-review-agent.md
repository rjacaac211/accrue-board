# ADR 0001: Fixed workflow for the pipeline, agent only for review assistance

- **Status:** accepted
- **Date:** 2026-09-24

## Context
The bookkeeping pipeline always runs the same steps in the same order: classify, extract,
validate, code, score, route. Anything that writes to the ledger has to be:
- **reproducible:** the same input gives the same path every time
- **auditable:** each decision can be traced to a line of code

The coordination layer also needs a single, authoritative record of each task's lifecycle state,
because the live board, the age-in-stage alerts and the audit trail all read it.

Agent frameworks such as LangGraph keep their own execution state in a checkpointer. If they
orchestrated the pipeline, the checkpointer and the task table would both record "where is this
item", and the two would have to be kept in sync.

Investigating a flagged item is a different kind of work. Which lookups are useful depends on why
the item was flagged and on what earlier lookups found. That is a tool-using loop in which the
model chooses the next step.

## Decision
- **The pipeline is plain Python.** It is built from typed step functions and an explicit
  lifecycle state machine. LLM calls are narrow and schema-constrained. Postgres is the only
  source of truth for task state, and every state transition writes its audit row in the same
  transaction.
- **The Review Assistant is a LangGraph agent** with read-only tools: vendor history, similar
  transactions, duplicate candidate, document text, chart of accounts, and ledger lookup. It
  writes a suggestion with evidence. It cannot change task state or post to the ledger, and every
  tool call it makes is recorded in the audit log.
- The agent uses no LangGraph checkpointer, because each investigation is a single short run.

## Consequences
- Routing and posting behaviour can be unit-tested without an LLM, and it is explained in plain
  code.
- "Agent" in this project refers only to the part that actually chooses its own next step.
- The agent's usefulness is measured separately: how often its suggestions agree with ground
  truth on flagged items.
