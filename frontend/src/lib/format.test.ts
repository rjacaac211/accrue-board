import { describe, expect, it } from 'vitest'
import { formatAge, formatMoney, formatPercent, formatQuantity, ruleLabel } from './format'

describe('formatMoney', () => {
  it.each([
    ['1234.5', '$1,234.50'],
    ['0.07', '$0.07'],
    ['1000000', '$1,000,000.00'],
    ['-21.50', '-$21.50'],
    ['0012.30', '$12.30'],
    ['99999999999.99', '$99,999,999,999.99'],
  ])('formats %s as %s', (input, expected) => {
    expect(formatMoney(input)).toBe(expected)
  })

  it('shows a dash for missing values', () => {
    expect(formatMoney(null)).toBe('—')
    expect(formatMoney('')).toBe('—')
  })
})

describe('formatAge', () => {
  it.each([
    [42, '42s'],
    [60 * 12, '12m'],
    [60 * 60 * 5 + 600, '5h 10m'],
    [86400 * 3 + 3600 * 4, '3d 4h'],
    [86400 * 2, '2d'],
  ])('formats %d seconds as %s', (seconds, expected) => {
    expect(formatAge(seconds)).toBe(expected)
  })
})

describe('labels', () => {
  it('uses readable rule names', () => {
    expect(ruleLabel('first_time_vendor')).toBe('First-time vendor')
    expect(ruleLabel('something_new')).toBe('something new')
  })

  it('formats percentages', () => {
    expect(formatPercent(0.925, 1)).toBe('92.5%')
    expect(formatPercent(null)).toBe('—')
  })
})

describe('formatQuantity', () => {
  it.each([
    ['46.0000', '46'],
    ['2.5000', '2.5'],
    ['12', '12'],
    ['0.1250', '0.125'],
  ])('formats %s as %s', (input, expected) => {
    expect(formatQuantity(input)).toBe(expected)
  })
})
