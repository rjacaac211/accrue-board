# Demo script

A 6 to 8 minute walkthrough of AccrueBoard. It follows the recorded capture
(`frontend/e2e/demo.capture.ts`) step by step, so a voice-over can be laid on the video, or the
same steps can be recorded live.

## Setting up

```bash
docker compose up -d db
cd frontend && pnpm install && pnpm build && cd ..
python scripts/run_demo.py --prefeed 10              # fresh demo stack at http://localhost:8020
python scripts/run_demo.py --prefeed 10 --capture    # or record the walkthrough (data/demo/)
```

`run_demo.py` does four things:
- recreates a separate demo database, so the development database is untouched
- seeds the fictional retailer's 12 months of history
- starts the API and a worker with the real models (`ANTHROPIC_API_KEY` in `.env`)
- processes the first ten documents, so the board opens with some history on it

A live recording costs a few cents in model calls. Re-runs replay recorded responses where the
requests are identical.

**Without an API key:** start the stack with `LLM_MODE=oracle`. The model is replaced by the
synthetic dataset's own ground truth, and the review assistant is unavailable. This is how the
browser smoke test runs in CI.

## The walkthrough

### 1. The board (30 s)
*Show:* the work board, with columns from Incoming to Done, and the stats strip.

*Say:*
- Every document the business receives becomes a task. The board shows each one from arrival
  to the ledger, how long it has been in its stage, and why it is where it is.
- Two parts work together:
  - A fixed pipeline reads and codes documents, and posts the clear ones.
  - People, helped by an agent, handle the rest.

### 2. Documents arrive (60 s)
*Do:* click **Feed 5**.

*Say:*
- Five documents arrive: PDFs and phone photos of receipts. Workers pick them up.
- For each document, the pipeline:
  1. classifies it
  2. extracts every field with Claude
  3. checks each value against the text actually printed on the page
  4. validates the arithmetic
  5. codes each line to the chart of accounts
- The cards move without a reload: Postgres announces every committed change, and the board
  listens over server-sent events.
- Most documents post themselves. Their confidence comes from checks that can be verified, not
  from the model's opinion of itself:
  - is the value on the page?
  - does it add up?
  - do independent coding methods agree?

### 3. A re-sent bill (90 s)
*Do:* open the **Boxcraft** card in *Needs review*, then the **Why** tab.

*Say:*
- It's held because a hard rule fired. The document number matches one already in the books,
  in a different format.
- A rule like that always goes to a person, whatever the confidence.

*Show:* the **Review assistant** card.

*Say:*
- A LangGraph agent investigated the document before anyone opened it, using read-only tools:
  it pulled up the original invoice, compared the lines and read the page.
- It recommends rejecting, and cites the evidence.
- It can't act: it suggests, and a person decides.

*Do:* **Reject**, with a reason. Then open **Audit trail**.

*Say:*
- Every step is here: the pipeline's, the assistant's, the reviewer's.
- Each event is hash-chained to the one before it, and the database refuses to edit or delete
  them.

### 4. Sales tax on stock for resale (60 s)
*Do:* back to the board, then open **Silverline**.

*Say:*
- An inventory supplier charged sales tax on goods bought for resale, which should be exempt.
- The assistant recommends holding the document, and says exactly what to ask the vendor.

*Do:* **Block**, using the assistant's question as the note.

### 5. An unusual amount with an explanation (60 s)
*Do:* open **Cloudcart**.

*Say:*
- This bill is thirteen times the vendor's median, so the outlier rule held it.
- The assistant checked the vendor's history: the fee on it varies with sales volume, and
  similar months exist. So it recommends approving.

*Do:* **Approve and post**, then the **Ledger** tab.

*Say:*
- Approving posts a balanced double-entry journal entry.
- The reviewed lines go into this client's knowledge store, so the next similar document is
  coded with that knowledge.

### 6. The books (45 s)
*Do:* open the **Ledger** page, then **Knowledge**.

*Say:*
- The trial balance is checked: debits equal credits, which is enforced by the database at
  commit.
- The knowledge store shows what reviewers confirmed or corrected, and it is per client.

### 7. Work that waits too long (30 s)
*Do:* back to the board, then click **+1 day** three times.

*Say:*
- The demo clock moves forward.
- Documents waiting on a person past their time limit are flagged, so nothing gets lost in a
  queue.
- Unpaid invoices get a due-date badge as their date approaches. Once due, they are escalated
  to the senior reviewer automatically, and the audit trail says why.

### 8. A second client (optional, 30 s)
*Do:* switch the client to **Ridgeline Remodeling**.

*Say:*
- A contractor with a different chart: job materials, subcontractors, equipment rental.
- It has its own knowledge and its own classifier.
- It buys at the same wholesale club as the retailer, and the same paper towels are coded to
  job materials here and to cleaning supplies there.

### Close (30 s)
*Say:*
- Measured on 300 held-out documents: see [eval-results.md](eval-results.md) for the
  automation rate, the errors that slip through, and what each document costs.
- Every number can be reproduced offline with `accrueboard eval end-to-end --replay`.
