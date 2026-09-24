import type { TaskState } from '@/api/types'
import { STATE_LABELS, STATE_TONES } from '@/lib/format'
import { cn } from '@/lib/utils'

export function StateBadge({ state, className }: { state: TaskState; className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset',
        STATE_TONES[state],
        className,
      )}
    >
      {STATE_LABELS[state]}
    </span>
  )
}
