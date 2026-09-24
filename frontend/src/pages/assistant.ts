import type { AssistantRun, SuggestedAction, Verdict } from '@/api/types'

export const ACTION_LABELS: Record<SuggestedAction, string> = {
  approve: 'Approve',
  reject: 'Reject',
  block: 'Hold for information',
}

export const ACTION_TONES: Record<SuggestedAction, string> = {
  approve: 'bg-emerald-50 text-emerald-800 ring-emerald-600/20',
  reject: 'bg-red-50 text-red-700 ring-red-600/20',
  block: 'bg-amber-50 text-amber-800 ring-amber-600/20',
}

export const VERDICT_LABELS: Record<Verdict, string> = {
  confirmed: 'Confirmed',
  false_positive: 'Likely false positive',
  uncertain: 'Uncertain',
}

export const TOOL_LABELS: Record<string, string> = {
  case: 'Case summary',
  get_document_text: 'Read the document text',
  vendor_history: 'Vendor history',
  similar_transactions: 'Similar transactions',
  get_document: 'Compared a document',
  ledger_lookup: 'Ledger lookup',
  submit_review: 'Submitted recommendation',
}

export const toolLabel = (tool: string): string => TOOL_LABELS[tool] ?? tool.replaceAll('_', ' ')

export interface AccountChange {
  line: number
  current: string | undefined
  suggested: string
  reason: string
}

/** Lines where the assistant suggests a different account from the one currently selected. */
export function accountChanges(run: AssistantRun | null, selected: string[]): AccountChange[] {
  const lines = run?.suggestion?.lines ?? []
  return [...lines]
    .sort((a, b) => a.line - b.line)
    .filter((l) => selected[l.line] !== l.account)
    .map((l) => ({ line: l.line, current: selected[l.line], suggested: l.account, reason: l.reason }))
}

/** The selection with the assistant's accounts applied (lines it did not cover are kept). */
export function applySuggestion(run: AssistantRun, selected: string[]): string[] {
  const next = [...selected]
  for (const line of run.suggestion?.lines ?? []) {
    if (line.line >= 0 && line.line < next.length) next[line.line] = line.account
  }
  return next
}

/** A short one-line description of a tool call's input, for the investigation trail. */
export function describeInput(input: Record<string, unknown>): string {
  return Object.values(input)
    .filter((v) => v !== null && v !== '')
    .map((v) => (typeof v === 'string' ? v : JSON.stringify(v)))
    .join(', ')
}
