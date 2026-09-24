# ADR 0005: Live updates over SSE, computed bottlenecks, and a shared demo clock

- **Status:** accepted
- **Date:** 2026-09-24

## Context
The board has to change the moment a task moves, and the demo has to show a task aging past its
review deadline in minutes rather than days. Several processes are involved (the API and one or
more workers), and they must agree on both the task state and the time.

## Decision

### Live updates use Server-Sent Events fed by Postgres `NOTIFY`
- **The trigger:** a trigger on `tasks` sends `NOTIFY task_events` on every change (ADR 0004).
  Postgres delivers it only if the transaction commits.
- **Why SSE:** updates flow one way, from server to browser; actions are ordinary REST calls. SSE
  is plain HTTP and browsers reconnect it automatically, so WebSockets are not needed.
- **Listening:** each SSE connection holds a synchronous `LISTEN` connection, which a background
  thread drains into an asyncio queue.
  - **Why not psycopg's async mode:** it does not run on the default Windows event loop.
  - **Why drain every notification:** waiting for one notification at a time dropped the others
    that arrived in the same network read. An integration test now checks that every state
    change arrives.
- **Heartbeat:** sent every 10 seconds, carrying the shared clock's time.

### Bottlenecks are computed when asked, not stored
Age-in-stage alerts and review congestion come from the pure domain function (`age_alerts`),
applied to each active task's `state_entered_at` at the current time. Computing them on request
means an alert can never be stale, and a clock fast-forward takes effect immediately. The
frontend refetches bottlenecks when a task event or heartbeat arrives.

This departs from the plan, which had a worker tick store alerts. That is only worth adding if
alerts ever need to be acknowledged or escalated (the optional SLA milestone).

### One clock for every process
`SharedClock` is real time plus an offset stored in `app_settings`. Each process rereads the
offset at most once per second.
- **Demo endpoints:** `/api/demo/clock/advance` and `/reset`. With `DEMO_MODE=true`, the demo can
  fast-forward the whole system: the API, the workers and the SSE heartbeats.
- **Drip-feed:** `/api/demo/clients/{id}/feed` queues the next generated documents in arrival
  order.

### Review actions are REST calls validated by the domain
Each action checks the reviewer and the task state, and is audited with the reviewer's
identity. The actions are approve (with optional edits), reject, block, unblock, reopen, retry
and assign. An approval re-validates the document, posts it, and writes confirmed or corrected
lines to the knowledge store.

## Consequences
- The UI needs only REST calls plus one `EventSource`.
- Integration tests use their own database (`<name>_test`, created and migrated automatically).
  Development data is never touched by the test suite.
