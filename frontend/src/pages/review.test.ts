import { describe, expect, it } from 'vitest'
import type { ExtractedDocument } from '@/api/types'
import { approveChanges, checkStatus, setField, visibleFields } from './review'

const doc: ExtractedDocument = {
  doc_type: 'invoice',
  vendor_name: 'Acme',
  vendor_state: 'TX',
  document_number: 'INV-1',
  issue_date: '2026-03-01',
  due_date: '2026-03-31',
  po_number: null,
  lines: [{ description: 'Widget', quantity: '2', unit_price: '5.00', amount: '10.00', taxable: false }],
  subtotal: '10.00',
  discount: '0.00',
  shipping: '0.00',
  tax_rate: null,
  tax: '0.00',
  total: '10.00',
  payment_method: null,
  referenced_document_number: null,
}

describe('checkStatus', () => {
  it('explains how a field was verified', () => {
    expect(checkStatus({ field: 'total', grounded: true, agreed: null, confidence: 1 })).toBe('verified')
    expect(checkStatus({ field: 'total', grounded: false, agreed: null, confidence: 0.5 })).toBe('not-found')
    expect(checkStatus({ field: 'total', grounded: false, agreed: true, confidence: 0.7 })).toBe('second-reader')
    expect(checkStatus({ field: 'total', grounded: true, agreed: false, confidence: 0.3 })).toBe('disagreed')
    expect(checkStatus({ field: 'total', grounded: null, agreed: true, confidence: 0.9 })).toBe('second-reader')
    expect(checkStatus({ field: 'total', grounded: null, agreed: null, confidence: 0 })).toBe('unparsable')
    expect(checkStatus(undefined)).toBe('unchecked')
  })
})

describe('editing', () => {
  it('keeps nullable fields null and money fields zero when cleared', () => {
    expect(setField(doc, 'po_number', '  ').po_number).toBeNull()
    expect(setField(doc, 'shipping', '').shipping).toBe('0.00')
    expect(setField(doc, 'total', ' 12.00 ').total).toBe('12.00')
  })

  it('shows fields relevant to the document type', () => {
    expect(visibleFields(doc)).toContain('due_date')
    expect(visibleFields(doc)).not.toContain('payment_method')
    expect(visibleFields({ ...doc, doc_type: 'credit_note' })).toContain('referenced_document_number')
  })
})

describe('approveChanges', () => {
  it('sends nothing when the reviewer only confirms', () => {
    expect(approveChanges(doc, { ...doc }, ['6100'], ['6100'])).toEqual({})
  })

  it('sends only what changed', () => {
    const edited = setField(doc, 'total', '11.00')
    expect(approveChanges(doc, edited, ['6100'], ['6100'])).toEqual({ document: edited })
    expect(approveChanges(doc, doc, ['6100'], ['6200'])).toEqual({ accounts: ['6200'] })
  })
})
