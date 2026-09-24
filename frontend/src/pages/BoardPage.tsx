import { useRef } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, CalendarClock, Upload } from 'lucide-react'
import { toast } from 'sonner'
import { useBoard, useBottlenecks, useStats, useUpload, useUsers } from '@/api/hooks'
import type { Card, DueAlert } from '@/api/types'
import { StateBadge } from '@/components/StateBadge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { docTypeLabel, formatAge, formatMoney, formatPercent, ruleLabel } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useSession } from '@/state/session-context'
import { COLUMNS, groupCards } from './board'

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border bg-background px-4 py-3" title={hint}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-xl font-semibold tabular-nums">{value}</div>
    </div>
  )
}

function StatsStrip({ clientId }: { clientId: string }) {
  const { data } = useStats(clientId)
  if (!data) return <Skeleton className="h-16 w-full" />
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <Stat label="Documents processed" value={String(data.processed)} />
      <Stat
        label="Auto-posted"
        value={formatPercent(data.automation_rate)}
        hint={`${data.auto_posted} posted without a person, ${data.human_reviewed} sent to review`}
      />
      <Stat label="Waiting for review" value={String(data.counts.needs_review ?? 0)} />
      <Stat
        label="Model spend"
        value={formatMoney(data.llm_cost_usd)}
        hint={`${data.llm_calls} model calls (${data.replayed_calls} served from recordings)`}
      />
    </div>
  )
}

function BottleneckPanel({ clientId }: { clientId: string }) {
  const { data } = useBottlenecks(clientId)
  if (!data || (data.alerts.length === 0 && data.due.length === 0 && !data.congestion)) return null
  const breaches = data.alerts.filter((a) => a.level === 'breach').length
  const overdue = data.due.filter((a) => a.level === 'breach').length
  return (
    <div
      role="status"
      className={cn(
        'flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border px-4 py-2.5 text-sm',
        breaches || overdue
          ? 'border-red-200 bg-red-50 text-red-900'
          : 'border-amber-200 bg-amber-50 text-amber-900',
      )}
    >
      {data.alerts.length > 0 && (
        <span className="flex items-center gap-1.5 font-medium">
          <AlertTriangle className="size-4" />
          {data.alerts.length} task{data.alerts.length === 1 ? '' : 's'} waiting too long
          {breaches > 0 && ` (${breaches} past the limit)`}
        </span>
      )}
      {data.due.length > 0 && (
        <span className="flex items-center gap-1.5 font-medium">
          <CalendarClock className="size-4" />
          {data.due.length} unpaid invoice{data.due.length === 1 ? '' : 's'} near the due date
          {overdue > 0 && ` (${overdue} due or overdue, escalated to a senior)`}
        </span>
      )}
      {data.congestion && <span>{data.congestion.message}</span>}
    </div>
  )
}

function DueBadge({ due }: { due: DueAlert }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium',
        due.level === 'breach' ? 'bg-red-100 text-red-800' : 'bg-amber-100 text-amber-900',
      )}
      title={`Invoice due ${due.due_date}`}
    >
      <CalendarClock className="size-3" />
      {due.message.replace(/^\w/, (c) => c.toUpperCase())}
    </span>
  )
}

function TaskCard({ card, assignee }: { card: Card; assignee?: string }) {
  return (
    <Link
      to={`/tasks/${card.task_id}`}
      className={cn(
        'block rounded-lg border bg-background p-3 shadow-xs transition hover:border-foreground/20 hover:shadow-sm',
        card.alert === 'breach' && 'border-red-300 ring-1 ring-red-200',
        card.alert === 'warning' && 'border-amber-300',
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">{card.vendor ?? card.filename}</div>
          <div className="truncate text-xs text-muted-foreground">
            {docTypeLabel(card.doc_type)}
            {card.document_number && ` · ${card.document_number}`}
          </div>
        </div>
        <div className="shrink-0 text-right text-sm font-medium tabular-nums">{formatMoney(card.total)}</div>
      </div>
      {(card.rules.length > 0 || card.due) && (
        <div className="mt-2 flex flex-wrap gap-1">
          {card.due && <DueBadge due={card.due} />}
          {card.rules.slice(0, 3).map((rule) => (
            <span key={rule} className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
              {ruleLabel(rule)}
            </span>
          ))}
          {card.rules.length > 3 && (
            <span className="text-[11px] text-muted-foreground">+{card.rules.length - 3}</span>
          )}
        </div>
      )}
      <div className="mt-2 flex items-center justify-between text-xs text-muted-foreground">
        <span className="flex items-center gap-2">
          <StateBadge state={card.state} />
          {card.score !== null && <span className="tabular-nums">score {card.score.toFixed(2)}</span>}
        </span>
        <span
          className={cn(
            'tabular-nums',
            card.alert === 'breach' && 'font-semibold text-red-700',
            card.alert === 'warning' && 'font-medium text-amber-700',
          )}
          title={`In this stage for ${formatAge(card.age_seconds)}`}
        >
          {assignee && <span className="mr-2 text-foreground/70">{assignee}</span>}
          {formatAge(card.age_seconds)}
        </span>
      </div>
    </Link>
  )
}

function UploadButton({ clientId }: { clientId: string }) {
  const input = useRef<HTMLInputElement>(null)
  const upload = useUpload(clientId)
  return (
    <>
      <input
        ref={input}
        type="file"
        accept="application/pdf,image/png,image/jpeg"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file)
            upload.mutate(file, {
              onSuccess: () => toast.success(`Queued ${file.name}`),
              onError: (error) => toast.error(error.message),
            })
          e.target.value = ''
        }}
      />
      <Button size="sm" variant="outline" onClick={() => input.current?.click()} disabled={upload.isPending}>
        <Upload data-icon="inline-start" />
        Upload document
      </Button>
    </>
  )
}

export function BoardPage() {
  const { clientId } = useSession()
  const board = useBoard(clientId)
  const users = useUsers()
  const names = Object.fromEntries((users.data ?? []).map((u) => [u.id, u.name.split(' ')[0]]))
  const grouped = groupCards(board.data ?? [])

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Work board</h1>
          <p className="text-sm text-muted-foreground">
            Every document from arrival to the ledger, updated live.
          </p>
        </div>
        {clientId && <UploadButton clientId={clientId} />}
      </div>
      {clientId && <StatsStrip clientId={clientId} />}
      {clientId && <BottleneckPanel clientId={clientId} />}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
        {COLUMNS.map((column) => (
          <section key={column.id} aria-label={column.title} className="flex min-h-40 flex-col rounded-xl bg-muted/60 p-2">
            <header className="flex items-baseline justify-between px-1.5 pb-2 pt-1">
              <h2 className="text-sm font-semibold">{column.title}</h2>
              <span className="text-xs tabular-nums text-muted-foreground" title={column.hint}>
                {grouped[column.id].length}
              </span>
            </header>
            <div className="flex flex-col gap-2">
              {board.isLoading && <Skeleton className="h-24" />}
              {grouped[column.id].map((card) => (
                <TaskCard
                  key={card.task_id}
                  card={card}
                  assignee={card.assignee_id ? names[card.assignee_id] : undefined}
                />
              ))}
              {!board.isLoading && grouped[column.id].length === 0 && (
                <p className="px-1.5 py-6 text-center text-xs text-muted-foreground">{column.hint}</p>
              )}
            </div>
          </section>
        ))}
      </div>
    </div>
  )
}
