import { expect, test, type Page } from '@playwright/test'

// The demo walkthrough from docs/demo.md, paced for a viewer. It expects the stack from
// `python scripts/run_demo.py --capture`: a fresh client with the first ten documents processed.

const pause = (page: Page, ms = 2500) => page.waitForTimeout(ms)

async function openCard(page: Page, column: string, vendor: RegExp) {
  const card = page.getByRole('region', { name: column }).getByRole('link').filter({ hasText: vendor })
  await expect(card.first()).toBeVisible()
  await card.first().click()
  await expect(page.getByRole('tab', { name: 'Document' })).toBeVisible()
}

async function assistantReady(page: Page) {
  const card = page.getByRole('region', { name: 'Review assistant' })
  await expect(card.getByText('Recommends')).toBeVisible()
  await card.scrollIntoViewIfNeeded()
  return card
}

async function backToBoard(page: Page) {
  await page.getByRole('link', { name: 'Board', exact: true }).first().click()
  await expect(page.getByRole('heading', { name: 'Work board' })).toBeVisible()
  await pause(page, 1500)
}

test('demo walkthrough', async ({ page }) => {
  // 1. The board: every document from arrival to the ledger, with some history on it.
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Work board' })).toBeVisible()
  await pause(page, 4000)

  // 2. New documents arrive and move across the board live.
  await page.getByRole('button', { name: 'Feed 5' }).click()
  await pause(page, 3000)
  const incoming = page.getByRole('region', { name: 'Incoming' })
  await expect(incoming.getByText('Queued or being processed')).toBeVisible()
  await expect(page.getByRole('region', { name: 'Needs review' }).getByRole('link').filter({ hasText: /Boxcraft/ })).toBeVisible()
  await pause(page, 4000)

  // 3. A re-sent bill: held as a duplicate; the assistant compares it with the original.
  await openCard(page, 'Needs review', /Boxcraft/)
  await pause(page, 2000)
  await page.getByRole('tab', { name: 'Why' }).click()
  await pause(page, 3500)
  await assistantReady(page)
  await pause(page, 6000)
  await page.getByRole('button', { name: 'Reject' }).click()
  await page.getByRole('textbox', { name: 'Note' }).fill('Duplicate of an invoice already posted.')
  await pause(page, 1500)
  await page.getByRole('button', { name: 'Confirm' }).click()
  await pause(page, 3000)
  await page.getByRole('tab', { name: 'Audit trail' }).click()
  await pause(page, 4000)

  // 4. Sales tax charged on stock for resale: the assistant says what to ask the vendor.
  await backToBoard(page)
  await openCard(page, 'Needs review', /Silverline/)
  const hold = await assistantReady(page)
  await pause(page, 6000)
  const question = (await hold.getByText('Ask:').locator('..').textContent()) ?? ''
  await page.getByRole('button', { name: 'Block' }).click()
  await page.getByRole('textbox', { name: 'Note' }).fill(question.replace(/^Ask:\s*/, '') || 'Ask the vendor.')
  await pause(page, 2000)
  await page.getByRole('button', { name: 'Confirm' }).click()
  await pause(page, 3000)

  // 5. An unusual amount the assistant explains from the vendor's history: approve and post.
  await backToBoard(page)
  await openCard(page, 'Needs review', /Cloudcart/)
  await assistantReady(page)
  await pause(page, 6000)
  await page.getByRole('button', { name: 'Approve and post' }).click()
  await pause(page, 1500)
  await page.getByRole('button', { name: 'Confirm' }).click()
  await expect(page.getByText(/Posted\./)).toBeVisible()
  await pause(page, 2500)
  await page.getByRole('tab', { name: 'Ledger' }).click()
  await pause(page, 4000)

  // 6. The ledger balances; the reviewed lines are now knowledge for the next document.
  await page.getByRole('link', { name: 'Ledger' }).click()
  await expect(page.getByText('Debits equal credits')).toBeVisible()
  await pause(page, 4000)
  await page.getByRole('link', { name: 'Knowledge' }).click()
  await pause(page, 4000)

  // 7. Time passes: work waiting too long is flagged.
  await backToBoard(page)
  for (let day = 0; day < 3; day++) {
    await page.getByRole('button', { name: '+1 day' }).click()
    await pause(page, 1500)
  }
  await pause(page, 5000)
})
