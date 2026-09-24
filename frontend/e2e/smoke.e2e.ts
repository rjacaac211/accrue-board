import { expect, test } from '@playwright/test'

// The main flow on a real stack: documents arrive, the worker processes them, the board updates
// live, a reviewer approves a held document, and it posts a balanced entry to the ledger.
test('documents flow from the inbox to the ledger', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Work board' })).toBeVisible()

  await page.getByRole('button', { name: 'Feed 5' }).click()
  const review = page.getByRole('region', { name: 'Needs review' })
  const done = page.getByRole('region', { name: 'Done' })
  // No reload: the cards move as the worker's changes arrive over the event stream.
  await expect(done.getByRole('link').first()).toBeVisible({ timeout: 90_000 })
  await expect(review.getByRole('link').first()).toBeVisible({ timeout: 90_000 })

  await review.getByRole('link').first().click()
  await expect(page.getByRole('tab', { name: 'Document' })).toBeVisible()
  await page.getByRole('tab', { name: 'Why' }).click()
  await expect(page.getByText('Needs a person').first()).toBeVisible()
  await page.getByRole('tab', { name: 'Audit trail' }).click()
  await expect(page.getByText(/Audit trail verified/)).toBeVisible()

  await page.getByRole('button', { name: 'Approve and post' }).click()
  await page.getByRole('button', { name: 'Confirm' }).click()
  await expect(page.getByText(/Posted\. \d+ lines? added to the knowledge store\./)).toBeVisible()
  await page.getByRole('tab', { name: 'Ledger' }).click()
  await expect(page.getByRole('cell', { name: /Accounts Payable|Credit Card|Bank/ }).first()).toBeVisible()

  await page.getByRole('link', { name: 'Ledger' }).click()
  await expect(page.getByText('Debits equal credits')).toBeVisible()
})
