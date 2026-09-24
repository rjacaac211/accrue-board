# Evaluation

All results are measured on the synthetic dataset described in
[synthetic-data.md](synthetic-data.md) (seed 7).
- **History:** the only source the knowledge store starts with.
- **Validation:** used for feedback and calibration.
- **Test:** used only for reported numbers.

The full evaluation report (extraction, anomaly precision and recall, routing, cost) is part
of milestone M8. This page currently covers the feedback loop and the review assistant.

## Feedback loop: does coding improve as reviewers confirm and correct?

```bash
cd backend
uv run accrueboard eval learning-curve                                  # baselines, free
uv run accrueboard eval learning-curve --with-model --new-vendors-only  # adds the model cascade
```

**Method:**
- Reviewed documents from the validation split are fed back in arrival order, with their correct
  accounts, as a reviewer's approvals and corrections would be.
- After each step, every coding method codes every test line.
- Test documents never enter the knowledge store; the harness refuses to run if one would.

Test lines are split by whether their vendor appears in the history. The second group is vendors
the system first meets through reviewed documents.

The coding methods compared:

| Method | What it is |
|---|---|
| `vendor_rule` | The vendor's most common past account; abstains for unknown vendors |
| `classifier` | Per-client TF-IDF + logistic regression, retrained on every change |
| `knn` | Majority account of the five most similar confirmed items |
| `cascade` | The production coder: vendor memory, retrieval and the language model, with the classifier as a second opinion |

### Baselines on the full test split (526 lines)

| Reviewed docs | Vendor rule | Classifier | kNN |
|---:|---:|---:|---:|
| 0 | 89.7% | 98.7% | 98.9% |
| 50 | 89.5% | 98.7% | 98.9% |
| 100 | 89.7% | 98.7% | 98.9% |
| 151 (all) | 91.1% | 100.0% | 98.9% |

- On repeat purchases from known vendors, retrieval and the classifier are already near perfect,
  so feedback on familiar vendors changes little.
- The vendor rule stays around 90%, because multi-purpose vendors (a wholesale club, a
  print-and-ship shop) defeat a one-account-per-vendor rule.

### Vendors first seen after the history (7 test lines, the language model included)

| Reviewed docs | Vendor rule | Classifier | kNN | Cascade |
|---:|---:|---:|---:|---:|
| 0 | 0.0% | 0.0% | 28.6% | **71.4%** |
| 50 | 0.0% | 0.0% | 28.6% | 71.4% |
| 100 | 0.0% | 0.0% | 28.6% | 71.4% |
| 151 (all) | 100.0% | 100.0% | 28.6% | **100.0%** |

- **Before feedback, only the language model codes unfamiliar vendors well.** It reads the
  chart of accounts and the item description, while the learned methods have nothing to go on.
- **Feedback closes the gap.** The first reviewed document from each new vendor (they arrive
  between documents 100 and 151) teaches the vendor rule, the classifier and the cascade.
- **A majority vote learns slowly.** Two confirmed examples from a new vendor are outvoted by
  older look-alike items, so kNN is kept as a signal, not as the decision.
- **The sample is small.** It is 7 lines from 4 documents, so these numbers show the direction
  of the effect, not a precise rate. The M8 report will add confidence intervals.

The same loop is tested end to end on Postgres (`tests/integration/test_feedback_loop.py`):
1. A reviewer recodes a first-time vendor's document.
2. On that vendor's next document, the vendor history shows the correction.
3. The retrained classifier predicts the reviewer's account.
4. The reviewer's entries appear among the retrieved examples.
5. Coding confidence drops because the model's choice now conflicts with what was learned.

*Run on 2026-09-24 with claude-sonnet-5 for coding and the local bge-small embedding model.*

## Review assistant: does it recommend the right action?

```bash
cd backend
uv run accrueboard eval review-assistant                # validation split; calls or replays the model
uv run accrueboard eval review-assistant --limit 5      # a quick look
```

**Method** (see [ADR 0006](adr/0006-review-assistant.md)):
- The split's documents arrive in order in a fresh database, seeded with the history only, and
  go through the real pipeline.
- A ground-truth oracle does the reading and coding. Every held document was therefore held by
  the routing rules, and the assistant is judged on its own.
- The assistant investigates each held document when it is held, seeing only what had arrived
  by then.
- The expected action follows the labels and the review policy in the assistant's prompt:
  - duplicates and non-bills: reject
  - documents the vendor must correct or the client must confirm: hold
  - everything else: approve
- An amount outlier that occurred naturally may be held or approved. The label only says the
  amount crossed the outlier definition.

### Validation split (37 held documents, claude-sonnet-5)

| Expected action | Agreement |
|---|---:|
| approve | 100.0% (12/12) |
| approve or hold (natural outliers) | 100.0% (4/4) |
| hold | 75.0% (6/8) |
| reject | 92.3% (12/13) |
| **all** | **91.9% (34/37)** |

| Label | Agreement |
|---|---:|
| no anomaly (held for low confidence or a policy check) | 100.0% (10/10) |
| exact file, renumbered and near duplicates | 100.0% (9/9) |
| arithmetic error, tax on resale inventory | 100.0% (5/5) |
| unsupported document | 100.0% (2/2) |
| first-time vendor, over the review cap | 100.0% (4/4) |
| amount outlier | 71.4% (5/7) |
| duplicate credit note | 50.0% (1/2) |

- It never recommended rejecting or holding a sound document. All 16 documents that should be
  approved were approved: the held-for-a-policy-check and low-confidence cases a reviewer
  would otherwise clear by hand.
- **The three misses were all approvals.** In each, the assistant argued the flag was
  explained:
  - **A software subscription billed at 11.6× its usual $49.** The assistant found an earlier
    annual plan from the same vendor coded to prepaid expenses. It read the bill as the annual
    renewal and suggested that account. The generator injected this outlier on a vendor that
    really does sell annual plans, so the case is ambiguous.
  - **A repair bill 10× the vendor's median.** The assistant judged a larger HVAC job
    plausible. Under the review policy (hold a far-out amount unless the document explains
    it), this is a genuine miss.
  - **A second credit note against an invoice.** It credits a different quantity than the
    first one, and the assistant read it as a further partial credit. The generator labels any
    second credit note a duplicate, even when the amounts differ, so this label is ambiguous
    too.
- **Accounts:** 55 of 56 suggested accounts were correct. The one change is the prepaid
  account above. The oracle proposes correct accounts, so this measures that the assistant
  does not break good coding, not that it fixes bad coding.
- **Cost:** $0.84 for 37 documents ($0.023 each), 2.4 model turns on average. The document
  text and vendor history are the tools it reaches for most.
- These numbers are from the validation split, and the prompt was written before this run
  (it was not tuned on it). The held-out test split and the end-to-end run, with real
  extraction and coding, are part of the M8 report. The small counts per label are directions,
  not rates.

*Run on 2026-09-24 with claude-sonnet-5 and the local bge-small embedding model.*
