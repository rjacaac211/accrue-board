# Evaluation

All results are measured on the synthetic dataset described in
[synthetic-data.md](synthetic-data.md) (seed 7).
- **History:** the only source the knowledge store starts with.
- **Validation:** used for feedback and calibration.
- **Test:** used only for reported numbers.

The full evaluation report (extraction, anomaly precision and recall, routing, cost) is part
of milestone M8. This page currently covers the feedback loop.

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
