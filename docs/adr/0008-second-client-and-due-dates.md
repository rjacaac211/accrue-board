# ADR 0008: A second client, and due-date escalation

- **Status:** accepted
- **Date:** 2026-09-25

## Context
Two claims were only tested against one client:
- **Everything is scoped per client:** the chart, vendor memory, retrieval, the classifier and
  the thresholds.
- **The rules generalize:** they are not tuned to one retailer's amounts.

Separately, the bottleneck rules only measured how long work had waited. For an unpaid invoice,
what matters more is its due date: an invoice due tomorrow is urgent however recently it
arrived.

## Decision

### The second client
- **A trades client with a different shape.** *Ridgeline Remodeling* is a residential
  contractor ([synthetic-data.md](../synthetic-data.md)). Its construction chart covers job
  materials, subcontractors, equipment rental, disposal, permits and work in progress. It has
  lumpy amounts, a $25,000 review cap and a $5,000 capitalization threshold.
- **One vendor shared with the retailer, coded differently.** Metro Wholesale Club appears for
  both clients. An integration test checks that every coding signal (vendor memory, the
  classifier, the retrieved neighbours) uses only the right client's knowledge.
- **The generator becomes client-agnostic.** The inventory account comes from the client's
  roles instead of a fixed code, and injected over-materiality amounts scale with the client's
  cap.
  - These changes, plus two bug fixes for rare seeds, keep the first client's seed-7 dataset
    byte-identical. The evaluation's committed recordings therefore stay valid.
  - The two bugs: a duplicate could be dated before its source when that source arrived after
    the split's end, and accidental same-total bills could look like unlabelled re-issues.
  - Both were found by running the generator's property tests over more seeds, and those seeds
    are now part of the suite.
- **`accrueboard bootstrap` sets up every client.** So `docker compose up` gives a board with a
  client switcher.

### Due dates and escalation
- **At-risk invoices.** An unpaid invoice (not already charged, and with a printed due date)
  that is waiting on a person (held for review, blocked, or failed) is at risk:
  - **Warning:** due within 3 days.
  - **Breach:** due today or overdue.

  The rule is pure domain code (`domain/bottleneck.py`), configurable per call, and tested
  without a database.
- **The document's clock.** "Today" for a document is its arrival time plus the time since it
  was queued.
  - In normal operation that is simply today.
  - Demo and evaluation documents keep their original arrival dates, which a date check needs.
    Their due dates are judged by how long they have waited, not by the calendar, which would
    call every one of them months overdue.
- **Escalation.**
  - When a due date is breached, the task is reassigned to a senior reviewer.
  - The actor is `sla-monitor`, a machine, and the change is recorded in the task's
    hash-chained audit trail with the reason ("overdue by 2 days").
  - It happens once per task: a task already with a senior reviewer is left alone.
  - Escalation reassigns work; it never approves, rejects or changes state.
- **Where it runs.** Workers sweep every few seconds. The board and the bottleneck banner show
  the alerts, and each card shows its own due status.
- **One clock for everyone.** Workers now use the same shared clock as the API. Fast-forwarding
  the demo clock moves everyone's "now", so escalation can be demonstrated. Before this, workers
  used system time, which contradicted ADR 0005.

## Consequences
- **Scoping is tested, not asserted.** A cross-client leak in retrieval or the classifier would
  fail an integration test.
- **Free evidence only, so far.** The second client's evaluation covers coding baselines and the
  learning curve, which need no model calls
  ([evaluation.md](../evaluation.md#second-client-coding-baselines)). A full end-to-end run with
  real models costs about as much as the first client's, and is left for when it is wanted.
- **Escalation depends on the worker.** With no worker running, alerts still show on the board,
  but nothing is reassigned.
