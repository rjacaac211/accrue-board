# ADR 0002: Accounting, validation and routing conventions

- **Status:** accepted
- **Date:** 2026-09-24

## Context
The domain core (`backend/src/accrueboard/domain/`) has to make many small accounting and
statistical choices. Each one changes what counts as "correct" in tests and in the evaluation,
so they are recorded here in one place.

## Decisions

### Money
- **Type:** money is always `Decimal`. Floats are rejected at every boundary, including Pydantic
  fields. Amounts are whole cents; unit prices and quantities allow up to four decimal places.
- **Rounding:** `ROUND_HALF_UP` to the cent, and only when computing a line amount, the tax, or
  an allocation.
- **Allocation:** discounts, shipping and tax are spread across lines with the
  **largest-remainder method**. Each share is first rounded down to the cent, then the leftover
  cents go to the largest fractional remainders. Shares always sum to the total exactly, are
  never negative, and each is within a cent of its exact pro-rata value.
  - This replaces the earlier idea of giving the whole remainder to the largest line, which can
    produce negative shares (e.g. 0.06 split across 12 lines).

### Tax (US sales tax on purchases)
- **Not recoverable:** purchase sales tax is part of the cost of what was bought, so each
  taxable line's landed cost includes its share of the tax.
- **Taxable base:** the taxable lines minus their pro-rata share of any document-level discount.
  Shipping is treated as non-taxable.
- **Printed rate:** if the document prints a rate, `round(base × rate)` must match the tax
  within $0.01.
- **No printed rate:** the implied rate must be between 0% and 12%, which is above every US
  combined state and local rate.

### Posting (accrual basis)
| Document | Debit | Credit |
|---|---|---|
| Invoice | each line account (landed cost) | Accounts Payable |
| Receipt | each line account | Card Clearing or Bank, by payment method |
| Credit note | Accounts Payable | each line account |

- Lines that end up worth $0.00 are dropped from the entry.
- Lines coded to the same account are combined into one journal line.
- Lines may only be coded to asset or expense accounts.
- A correction after posting is a reversing entry linked to the original, never an edit.

### Capitalization
- A line item is re-coded to Fixed Assets when its unit price is **strictly above** $2,500 and
  it was coded to a capitalizable expense account.
- The test is per item, not per line: ten $400 monitors on one line stay expensed.

### Duplicates
- **Normalizing numbers:** document numbers become upper-case alphanumerics, with `INVOICE`,
  `INV` and `NO`-before-a-digit prefixes and leading zeros removed. The normalization is
  idempotent (applying it twice gives the same result). Zeros after a letter are kept
  (`A-0042` → `A0042`).
- **Normalizing vendors:** vendor names drop punctuation, a leading "the", and trailing legal
  suffixes such as Inc and LLC.
- **Categories:** documents are compared only within their category. Invoices and receipts are
  one category, credit notes another, so a credit note is never a duplicate of the invoice it
  credits.
- **Near duplicate:** same vendor, same total, and issue dates within 10 days. It is a soft
  signal only, because monthly recurring charges are about 30 days apart.

### Outliers
- **Score:** the modified z-score on log amounts, `0.6745 × (ln x − median) / MAD`. MAD is the
  median absolute deviation; the log scale means the score depends on ratios, not raw dollar
  differences.
- **MAD floor of 0.05:** without it, a vendor whose history is all the same amount would make
  any change look infinitely unusual. With it, a ~20% price rise scores about 2.5 (soft), while
  a tenfold amount scores above 30.
- **History:** at least 5 of the vendor's own amounts are used; failing that, at least 5 from
  the coded account; failing that, no judgment is made.
- **Levels:** `|z| > 3.5` is a hard rule and `2.5 < |z| ≤ 3.5` is a soft signal.

### Routing
- **Hard rules** (any one forces review):
  - validation issue
  - unsupported document type
  - critical field (vendor, date or total) not found in the document text
  - exact-file, same-number or second-credit-note duplicate
  - hard outlier
  - total over the materiality cap ($10,000)
  - sales tax on a line coded to Inventory
  - first-time vendor
- **Score:** `min(extraction confidence, coding confidence) × the factor for each distinct soft
  signal present`.

  | Soft signal | Factor |
  |---|---|
  | Near duplicate | 0.6 |
  | Mild outlier | 0.8 |
  | Credited document not found | 0.8 |

- **Extraction-pass disagreement** lowers extraction confidence upstream. It is not also counted
  as a soft signal, to avoid penalizing the same evidence twice.
- **Auto-post threshold:** there is no default in code. It is required configuration, set by
  calibration on the validation split.

### Lifecycle
- **Transitions:** there is an explicit table of allowed transitions, each naming whether a
  machine or a human may make it.
- **Human-only moves:** approve, reject, block and reopen. Machines never make these, and
  humans never auto-approve.
- **Posting refused:** `AutoApproved`/`Approved` → `NeedsReview` is a safety path for a posting
  refused at the last step.

### Bottlenecks
| Stage | Warning after | Breach after |
|---|---|---|
| Queued | 15 min | — |
| Processing | 10 min | — |
| NeedsReview | 24 h | 72 h |
| Blocked | 3 days | 5 days |

- Review congestion is flagged when the queue is larger than reviewers × daily capacity.

## Consequences
- Every rule above is deterministic and covered by unit tests. Allocation, posting balance and
  number normalization are also covered by property-based tests.
- Changing any threshold or factor changes the evaluation results. Tuning them is the job of
  the calibration step (M8), not ad hoc edits.
