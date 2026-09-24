import type { TaskState } from '@/api/types'

/** Format a decimal string as dollars without converting it to a binary float. */
export function formatMoney(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const trimmed = value.trim()
  const negative = trimmed.startsWith('-')
  const [whole = '0', fraction = ''] = trimmed.replace(/^[-+]/, '').split('.')
  const cents = (fraction + '00').slice(0, 2)
  const grouped = whole.replace(/^0+(?=\d)/, '').replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${negative ? '-' : ''}$${grouped}.${cents}`
}

/** A quantity without trailing zeros ("46.0000" -> "46", "2.5000" -> "2.5"). */
export function formatQuantity(value: string): string {
  if (!value.includes('.')) return value
  return value.replace(/0+$/, '').replace(/\.$/, '')
}

/** Compact duration: 3d 4h, 5h 10m, 12m, 40s. */
export function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`
  const minutes = Math.floor(seconds / 60)
  const days = Math.floor(minutes / 1440)
  const hours = Math.floor((minutes % 1440) / 60)
  const mins = minutes % 60
  if (days) return hours ? `${days}d ${hours}h` : `${days}d`
  if (hours) return mins ? `${hours}h ${mins}m` : `${hours}h`
  return `${mins}m`
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

export function formatDateTime(iso: string): string {
  const date = new Date(iso)
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export const STATE_LABELS: Record<TaskState, string> = {
  queued: 'Queued',
  processing: 'Processing',
  auto_approved: 'Auto-approved',
  needs_review: 'Needs review',
  approved: 'Approved',
  blocked: 'Blocked',
  rejected: 'Rejected',
  failed: 'Failed',
  posted: 'Posted',
}

export const STATE_TONES: Record<TaskState, string> = {
  queued: 'bg-slate-100 text-slate-700 ring-slate-200',
  processing: 'bg-sky-50 text-sky-700 ring-sky-200',
  auto_approved: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  needs_review: 'bg-amber-50 text-amber-800 ring-amber-200',
  approved: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  blocked: 'bg-orange-50 text-orange-800 ring-orange-200',
  rejected: 'bg-zinc-100 text-zinc-600 ring-zinc-200',
  failed: 'bg-red-50 text-red-700 ring-red-200',
  posted: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
}

export const RULE_LABELS: Record<string, string> = {
  unsupported_type: 'Unsupported document type',
  validation_failed: 'Validation failed',
  ungrounded_critical_field: 'Key field not found in document',
  duplicate_file: 'Duplicate file',
  duplicate_number: 'Duplicate document number',
  duplicate_credit_note: 'Duplicate credit note',
  amount_outlier: 'Unusual amount',
  over_materiality: 'Over review cap',
  tax_on_resale_inventory: 'Sales tax on resale inventory',
  first_time_vendor: 'First-time vendor',
  near_duplicate: 'Possible duplicate',
  mild_outlier: 'Somewhat unusual amount',
  credit_reference_not_found: 'Credited document not found',
}

export const ruleLabel = (rule: string): string => RULE_LABELS[rule] ?? rule.replaceAll('_', ' ')

export const docTypeLabel = (docType: string | null): string =>
  docType ? docType.replace('_', ' ').replace(/^\w/, (c) => c.toUpperCase()) : 'Unclassified'
