# Synthetic data

AccrueBoard is evaluated on generated data, so every accuracy and detection number is measured
against known ground truth rather than hand labels.

```bash
cd backend
uv run accrueboard datagen            # full dataset -> data/generated/fernhill/ (about 4 s)
uv run accrueboard sample             # one document per layout -> data/sample/ (committed)
```

## Principles
1. **Ground truth first.** Each document starts as a structured record: what is printed on it,
   the correct account for every line, and any anomaly it carries. The PDF or PNG is rendered
   from that record afterwards, so extraction, coding and flagging can all be scored exactly.
2. **Deterministic, on any OS.** The same seed produces byte-identical records and files on
   Windows and Linux; this was verified by comparing hashes of a full dataset generated on each.
   - ReportLab runs in invariant mode.
   - PNG receipt scans are drawn with Pillow (bundled FreeType) and an embedded TrueType font,
     not by rasterizing PDFs, because PDF renderers differ between platforms.
   - PNGs are encoded with the standard library's zlib.
   - Raster noise is seeded.

   This matters because recorded model responses are keyed by input bytes. A test regenerates
   the whole dataset and compares hashes.
3. **Objective anomaly definitions.** Each anomaly is defined by a property of the data in
   [`anomalies.yaml`](../backend/src/accrueboard/datagen/specs/anomalies.yaml), not by what a
   detector happens to flag. For example, an amount outlier is "at least 8× the vendor's history
   median"; the detector's robust z-score is a different, independent test. Where a naturally
   generated document meets a definition (a one-off expensive laptop), it is labelled too, so
   recall and precision are not inflated by unlabelled anomalies.
4. **Hard negatives.** Cases that look suspicious but are legitimate are labelled with the rules
   that must *not* fire, so false positives are measured:
   - recurring identical subscription charges
   - split shipments on one purchase order
   - the same document number from different vendors
   - seasonal restocks
   - first credit notes against an invoice
5. **No leakage.** History seeds the knowledge store and is never rendered or evaluated.
   Validation calibrates thresholds. Test is used only for reported numbers.

## The client
*Fernhill Home Goods LLC* is a fictional online retailer of home goods in Austin, TX. It is on
accrual basis and pays US sales tax on purchases. All vendor names, addresses and amounts are
invented.

- **Chart of accounts:** 30 accounts, including Inventory (asset), Fixed Assets, Accounts
  Payable, Card Clearing, freight, packaging, platform fees, software, marketing, rent and
  utilities, cleaning and breakroom.
- **Recurring vendors (31):** inventory suppliers (tax-exempt, since the stock is bought for
  resale), freight carriers, packaging, SaaS subscriptions, contractors, rent, utilities, and
  card receipts for office supplies, electronics, meals and travel.
- **Coding difficulty on purpose:**
  - Many items have alternative wordings ("LINEN THROW NATURAL 50X60" vs "Throw blanket, linen
    (natural)"), and half of all lines use one.
  - Two card vendors (a wholesale club and a print-and-ship shop) sell items that belong in
    four different accounts, which defeats a per-vendor rule.
  - Subscription vendors occasionally bill an annual plan, which belongs in Prepaid Expenses
    rather than the vendor's usual expense account.
- **New vendors (3):** appear only in the evaluation splits, as first-time-vendor cases.

## Splits (seed 7)
| Split | Period | Documents | Purpose |
|---|---|---|---|
| history | 2024-12 to 2025-11 | 546 | knowledge-store seed (structured only) |
| validation | 2025-12 to 2026-02 | 153 | threshold calibration |
| test | 2026-03 to 2026-08 | 307 | reported results |

Each evaluation split contains invoices, receipts (about 60% of them as noisy PNG scans),
credit notes and unsupported documents (statements and quotations). The test split has 47
injected anomalies plus the naturally occurring ones. Exact counts are in `manifest.json`.

## Layouts
There are six invoice styles, plus receipt, statement and quotation layouts. The invoice styles
vary in:
- fonts and header placement
- number labels (`Invoice #`, `Bill Number`, `Reference`, …)
- date formats (`12/01/2025`, `December 01, 2025`, `2025-12-01`, …)
- money formats (`$1,234.50`, `1,234.50`, `USD 1,234.50`)
- how taxable lines are marked (a `Tax` column, or `*` with a legend)
- whether the tax rate is printed

A test checks that every value extraction is scored on appears in each PDF's text layer.

## Files
```
data/generated/<client>/
  manifest.json            seed, generator version, counts per split and label
  history.jsonl            GroundTruth records (knowledge-store seed)
  validation.jsonl         GroundTruth records in arrival order
  test.jsonl
  documents/<split>/<doc_id>.pdf|png
```
