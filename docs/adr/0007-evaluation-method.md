# ADR 0007: How the system is evaluated end to end

- **Status:** accepted
- **Date:** 2026-09-24

## Context
The headline claims are:
- how much work posts itself
- how often something wrong slips through
- how well documents are read and coded
- how well anomalies are caught
- what it all costs

Each needs a definition that can be checked, and a run that others can reproduce without
paying for model calls.

## Decision

### The run
`accrueboard eval end-to-end` sends the whole system through the validation split and then the
test split:
- in a fresh database seeded only with the history split
- with real models, in arrival order

Three choices shape the run:

- **A simulated reviewer.** Every held document is resolved as the ground truth says it should
  be:
  - duplicates and non-bills are rejected
  - documents needing the vendor or the client are held
  - everything else is approved, with the correct values and accounts

  Approvals feed the knowledge store and the settled history, as they would in production.
- **Test-then-train (prequential) scoring.** Each document is scored as the system handled it
  *before* its own review. Later documents may learn from earlier ones: that is the product
  working as designed, and no document ever sees its own label.
- **A calibrated threshold.** After the validation split, the auto-post threshold is set to the
  most automation whose auto-posted documents are at most 1% wrong. The test split then runs at
  that value, and the reported numbers come from the test split only.

### What counts as wrong
A document is **wrong to post** if posting it as the pipeline read and coded it would put an
error in the ledger. That happens in two ways.

It should not be posted at all:
- a duplicate
- a non-bill
- a vendor arithmetic error
- sales tax on resale stock
- an injected amount outlier

Or its journal entry would differ from the correct one:
- the document type, vendor, date or amounts
- a receipt's payment method (an invoice always credits payables)
- any line's account, after the capitalization rule

Both are checked exactly against the generator's ground truth. A policy check that fired
correctly (a first-time vendor, a total over the review cap) is not an error.

### Reporting
- Every proportion is reported with its count and a Wilson 95% interval.
- The automation-against-escape curve is recomputed from the recorded scores. It ignores the
  second-order effect a different threshold would have on later documents' history, and the
  report says so.

### Reproducibility
- Warm-up: classification and extraction depend only on the file, so they are requested in
  parallel first. The ordered pass then replays them. PDFium is serialized by a process-wide
  lock, because it is not thread-safe.
- Every model response the run used is packed into a deterministic archive,
  `data/results/recordings.tar.gz`. `--replay` re-runs the evaluation from it, with no key, and
  checks that the numbers match `data/results/end-to-end.json` exactly.
- Replay needs the same embedding model: the coding prompt includes retrieved examples.
- The replay reproduces the committed results exactly on Windows and on Linux (in the app's
  container). The `Evaluation replay` workflow re-checks it weekly, on demand, and when
  evaluated code changes on main.

### The browser smoke test
The smoke test runs the real stack with `LLM_MODE=oracle`, so it needs no key and is fully
deterministic. It checks the plumbing and the UI flow, not model quality: model quality is what
this evaluation measures.

## Consequences
- The reported numbers can be regenerated offline by anyone with the repository.
- The simulated reviewer is a perfect reviewer, so the numbers describe the system with careful
  reviewers. The feedback loop cannot be harmed by reviewer mistakes in this setup.
- A live re-run gives slightly different numbers, because model outputs vary. That is why the
  recorded run is the reference.
