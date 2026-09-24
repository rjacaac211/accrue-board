import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import type { Card, TaskDetail } from './api/types'
import { makeQueryClient } from './lib/query'

const card = (overrides: Partial<Card>): Card => ({
  task_id: 't1',
  state: 'needs_review',
  state_entered_at: '2026-03-01T10:00:00Z',
  age_seconds: 30 * 3600,
  alert: 'warning',
  received_at: '2026-03-01T10:00:00Z',
  filename: 'bill.pdf',
  doc_type: 'invoice',
  vendor: 'Oakridge Textiles Inc.',
  document_number: 'INV-01061',
  total: '1234.50',
  score: 0.97,
  rules: ['first_time_vendor'],
  summary: 'Needs review. First-time vendor.',
  assignee_id: null,
  ...overrides,
})

const detail: TaskDetail = {
  card: card({}),
  client_id: 'fernhill',
  media_type: 'application/pdf',
  has_file: true,
  extracted: {
    doc_type: 'invoice',
    vendor_name: 'Oakridge Textiles Inc.',
    vendor_state: 'NC',
    document_number: 'INV-01061',
    issue_date: '2026-03-01',
    due_date: '2026-03-31',
    po_number: null,
    lines: [
      { description: 'Linen throw blanket', quantity: '10', unit_price: '123.45', amount: '1234.50', taxable: false },
    ],
    subtotal: '1234.50',
    discount: '0.00',
    shipping: '0.00',
    tax_rate: null,
    tax: '0.00',
    total: '1234.50',
    payment_method: null,
    referenced_document_number: null,
  },
  extraction: {
    confidence: 1,
    second_pass: false,
    second_pass_reason: null,
    ungrounded: [],
    unparsable: [],
    fields: [
      { field: 'total', grounded: true, agreed: null, confidence: 1 },
      { field: 'vendor_name', grounded: false, agreed: true, confidence: 0.7 },
    ],
  },
  coding: {
    accounts: ['1300'],
    confidence: 0.97,
    vendor_known: false,
    vendor_history: {},
    lines: [
      {
        line_index: 0,
        account: '1300',
        reason: 'Stock for resale',
        llm_account: '1300',
        classifier_account: '1300',
        classifier_probability: 0.9,
        vendor_rule_account: null,
        neighbour_account: '1300',
        neighbour_share: 1,
        confidence: 0.92,
      },
    ],
  },
  routing: {
    outcome: 'needs_review',
    hits: [{ rule: 'first_time_vendor', severity: 'hard', detail: 'first document from Oakridge' }],
    score: 0.92,
    threshold: 0.9,
    extraction_confidence: 1,
    coding_confidence: 0.92,
    factors: {},
    summary: 'Needs review. First-time vendor.',
  },
  line_accounts: ['1300'],
  last_error: null,
  attempts: 1,
  audit: [],
  audit_intact: true,
  entries: [],
  calls: [],
  cost_usd: '0.012',
}

const responses: Record<string, unknown> = {
  '/api/clients': [{ id: 'fernhill', name: 'Fernhill Home Goods LLC', business: 'retail', auto_post_threshold: 0.9 }],
  '/api/users': [
    { id: 'u_alex', name: 'Alex Rivera', role: 'reviewer' },
    { id: 'u_jordan', name: 'Jordan Lee', role: 'senior' },
  ],
  '/api/clock': { now: '2026-03-02T16:00:00Z', offset_hours: 0, demo_mode: false },
  '/api/clients/fernhill/board': [
    card({}),
    card({ task_id: 't2', state: 'posted', vendor: 'Boxcraft Packaging Supply', rules: [], alert: null, age_seconds: 60 }),
  ],
  '/api/clients/fernhill/stats': {
    counts: { needs_review: 1, posted: 1 },
    processed: 2,
    auto_posted: 1,
    human_reviewed: 1,
    automation_rate: 0.5,
    llm_cost_usd: '0.024',
    llm_calls: 6,
    replayed_calls: 6,
  },
  '/api/clients/fernhill/bottlenecks': {
    now: '2026-03-02T16:00:00Z',
    alerts: [{ task_id: 't1', state: 'needs_review', level: 'warning', age: 'PT30H', limit: 'PT24H', message: 'x' }],
    congestion: null,
    counts: { needs_review: 1 },
  },
  '/api/clients/fernhill/accounts': [
    { code: '1300', name: 'Inventory', type: 'asset', description: '', role: 'inventory' },
    { code: '6100', name: 'Office Supplies', type: 'expense', description: '', role: null },
    { code: '2000', name: 'Accounts Payable', type: 'liability', description: '', role: 'accounts_payable' },
  ],
  '/api/tasks/t1': detail,
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  window.localStorage.clear()
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') {
      return new Response(JSON.stringify({ task_id: 't1', state: 'posted', journal_entry: 'je_1', knowledge_entries: 1 }))
    }
    const body = responses[url]
    return body === undefined
      ? new Response(JSON.stringify({ detail: 'not found' }), { status: 404 })
      : new Response(JSON.stringify(body))
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  window.history.pushState({}, '', '/')
})

describe('board', () => {
  it('shows tasks in their columns with rules, age and the bottleneck banner', async () => {
    render(<App client={makeQueryClient()} />)
    const review = await screen.findByRole('region', { name: 'Needs review' })
    expect(await within(review).findByText('Oakridge Textiles Inc.')).toBeInTheDocument()
    expect(within(review).getByText('First-time vendor')).toBeInTheDocument()
    expect(within(review).getByText('1d 6h')).toBeInTheDocument()
    expect(within(review).getByText('$1,234.50')).toBeInTheDocument()
    const done = screen.getByRole('region', { name: 'Done' })
    expect(within(done).getByText('Boxcraft Packaging Supply')).toBeInTheDocument()
    expect(await screen.findByText(/1 task waiting too long/)).toBeInTheDocument()
    expect(screen.getByText('50%')).toBeInTheDocument()
  })
})

describe('task review', () => {
  it('shows how each value was verified and sends only the reviewer’s changes', async () => {
    window.history.pushState({}, '', '/tasks/t1')
    render(<App client={makeQueryClient()} />)

    expect(await screen.findByLabelText('Total')).toHaveValue('1234.50')
    expect(screen.getAllByLabelText('Found in the document text').length).toBeGreaterThan(0)
    expect(screen.getByLabelText('Confirmed by a second independent reading')).toBeInTheDocument()

    const account = await screen.findByLabelText('Account for line 1')
    await waitFor(() => expect(within(account).getByRole('option', { name: /6100/ })).toBeInTheDocument())
    expect(within(account).queryByRole('option', { name: /2000/ })).toBeNull() // payables are not codable
    fireEvent.change(account, { target: { value: '6100' } })

    fireEvent.click(screen.getByRole('button', { name: 'Approve and post' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm' }))

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === 'POST')
      expect(post).toBeDefined()
      const [url, init] = post as [string, RequestInit]
      expect(url).toBe('/api/tasks/t1/approve')
      expect(JSON.parse(String(init.body))).toEqual({ reviewer_id: 'u_alex', note: '', accounts: ['6100'] })
    })
  })

  it('explains the routing decision', async () => {
    window.history.pushState({}, '', '/tasks/t1')
    render(<App client={makeQueryClient()} />)
    fireEvent.click(await screen.findByRole('tab', { name: 'Why' }))
    expect(await screen.findByText('first document from Oakridge')).toBeInTheDocument()
    expect(screen.getByText('Needs a person')).toBeInTheDocument()
  })
})
