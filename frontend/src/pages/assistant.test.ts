import { describe, expect, it } from 'vitest'
import type { AssistantRun } from '@/api/types'
import { accountChanges, applySuggestion, describeInput, toolLabel } from './assistant'

const run = (lines: { line: number; account: string }[]): AssistantRun => ({
  status: 'done',
  suggestion: {
    action: 'approve',
    summary: 'Routine restock.',
    question: null,
    rule_assessments: [],
    lines: lines.map((l) => ({ ...l, reason: 'usual for this item' })),
    evidence: [],
  },
  error: null,
  proposed_accounts: ['1300', '6100'],
  steps: [],
  notes: [],
  model: 'claude-sonnet-5',
  prompt_version: 'review-assistant-v1',
  model_turns: 2,
  cost_usd: '0.021',
  replayed: false,
  ran_at: '2026-01-05T10:00:00Z',
})

describe('assistant suggestions', () => {
  it('lists only the lines where the suggestion differs from the selection', () => {
    const suggested = run([
      { line: 1, account: '5300' },
      { line: 0, account: '1300' },
    ])
    expect(accountChanges(suggested, ['1300', '6100'])).toEqual([
      { line: 1, current: '6100', suggested: '5300', reason: 'usual for this item' },
    ])
    expect(accountChanges(suggested, ['1300', '5300'])).toEqual([])
    expect(accountChanges(null, ['1300'])).toEqual([])
  })

  it('applies suggested accounts without touching other lines', () => {
    const suggested = run([
      { line: 1, account: '5300' },
      { line: 7, account: '9999' },
    ])
    expect(applySuggestion(suggested, ['1300', '6100', '6200'])).toEqual(['1300', '5300', '6200'])
  })

  it('describes tool calls in plain words', () => {
    expect(toolLabel('vendor_history')).toBe('Vendor history')
    expect(toolLabel('new_tool')).toBe('new tool')
    expect(describeInput({ document_number: 'INV-7', vendor_name: null })).toBe('INV-7')
  })
})
