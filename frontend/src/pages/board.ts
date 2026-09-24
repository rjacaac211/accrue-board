import type { Card, TaskState } from '@/api/types'

export interface Column {
  id: string
  title: string
  hint: string
  states: TaskState[]
}

export const COLUMNS: Column[] = [
  { id: 'incoming', title: 'Incoming', hint: 'Queued or being processed', states: ['queued', 'processing'] },
  { id: 'review', title: 'Needs review', hint: 'Waiting for a person', states: ['needs_review'] },
  { id: 'blocked', title: 'Blocked', hint: 'Waiting on information', states: ['blocked'] },
  { id: 'failed', title: 'Failed', hint: 'Pipeline errors to retry', states: ['failed'] },
  {
    id: 'done',
    title: 'Done',
    hint: 'Recently posted or rejected',
    states: ['auto_approved', 'approved', 'posted', 'rejected'],
  },
]

const ALERT_RANK = { breach: 0, warning: 1 } as const

/** Cards per column. Waiting columns list the most urgent first; Done lists the newest first. */
export function groupCards(cards: Card[]): Record<string, Card[]> {
  const grouped: Record<string, Card[]> = Object.fromEntries(COLUMNS.map((c) => [c.id, []]))
  for (const card of cards) {
    const column = COLUMNS.find((c) => c.states.includes(card.state))
    if (column) grouped[column.id].push(card)
  }
  for (const column of COLUMNS) {
    grouped[column.id].sort((a, b) => {
      if (column.id === 'done') return a.age_seconds - b.age_seconds
      const rank = (c: Card) => (c.alert ? ALERT_RANK[c.alert] : 2)
      return rank(a) - rank(b) || b.age_seconds - a.age_seconds
    })
  }
  return grouped
}
