import type { ExtractedDocument, FieldCheck } from '@/api/types'

export type CheckStatus = 'verified' | 'second-reader' | 'not-found' | 'disagreed' | 'unparsable' | 'unchecked'

/** How a field's value was verified, in plain terms (see ADR 0003). */
export function checkStatus(check: FieldCheck | undefined): CheckStatus {
  if (!check) return 'unchecked'
  if (check.confidence === 0) return 'unparsable'
  if (check.agreed === false) return 'disagreed'
  if (check.grounded === false) return check.agreed ? 'second-reader' : 'not-found'
  if (check.grounded === null) return check.agreed ? 'second-reader' : 'unchecked'
  return 'verified'
}

export const CHECK_LABELS: Record<CheckStatus, string> = {
  verified: 'Found in the document text',
  'second-reader': 'Confirmed by a second independent reading',
  'not-found': 'Not found in the document text',
  disagreed: 'The second reading disagreed',
  unparsable: 'The value could not be read',
  unchecked: 'Not checked',
}

const HEADER_FIELDS = [
  'vendor_name',
  'document_number',
  'issue_date',
  'due_date',
  'po_number',
  'referenced_document_number',
  'subtotal',
  'discount',
  'shipping',
  'tax_rate',
  'tax',
  'total',
  'payment_method',
] as const

export type HeaderField = (typeof HEADER_FIELDS)[number]

export const FIELD_LABELS: Record<HeaderField, string> = {
  vendor_name: 'Vendor',
  document_number: 'Document number',
  issue_date: 'Issue date',
  due_date: 'Due date',
  po_number: 'PO number',
  referenced_document_number: 'Credits invoice',
  subtotal: 'Subtotal',
  discount: 'Discount',
  shipping: 'Shipping',
  tax_rate: 'Tax rate',
  tax: 'Sales tax',
  total: 'Total',
  payment_method: 'Paid by',
}

/** Header fields worth showing for a document type. */
export function visibleFields(doc: ExtractedDocument): HeaderField[] {
  return HEADER_FIELDS.filter((field) => {
    if (field === 'referenced_document_number') return doc.doc_type === 'credit_note'
    if (field === 'payment_method') return doc.doc_type === 'receipt'
    if (field === 'due_date') return doc.doc_type === 'invoice'
    return true
  })
}

const NOT_NULL_MONEY = new Set(['discount', 'shipping', 'tax'])

/** Apply one edited header value to a draft, keeping the API's shape (null vs "0.00"). */
export function setField(doc: ExtractedDocument, field: HeaderField, raw: string): ExtractedDocument {
  const value = raw.trim()
  if (NOT_NULL_MONEY.has(field)) return { ...doc, [field]: value === '' ? '0.00' : value }
  return { ...doc, [field]: value === '' ? null : value }
}

export interface ApproveChanges {
  document?: ExtractedDocument
  accounts?: string[]
}

/** Only send what the reviewer actually changed. */
export function approveChanges(
  original: ExtractedDocument,
  draft: ExtractedDocument,
  proposedAccounts: string[],
  accounts: string[],
): ApproveChanges {
  const changes: ApproveChanges = {}
  if (JSON.stringify(original) !== JSON.stringify(draft)) changes.document = draft
  if (accounts.some((code, i) => code !== proposedAccounts[i])) changes.accounts = accounts
  return changes
}
