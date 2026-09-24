import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, Check, CircleAlert, CircleHelp, ShieldAlert, ShieldCheck, X } from 'lucide-react'
import { toast } from 'sonner'
import { useAccounts, useAction, useApprove, useAssign, useTask, useUsers, type SimpleAction } from '@/api/hooks'
import type { AccountSummary, ExtractedDocument, LineCoding, TaskDetail } from '@/api/types'
import { StateBadge } from '@/components/StateBadge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import {
  docTypeLabel,
  formatAge,
  formatDateTime,
  formatMoney,
  formatPercent,
  formatQuantity,
  ruleLabel,
} from '@/lib/format'
import { cn } from '@/lib/utils'
import { AssistantCard } from './AssistantCard'
import { useSession } from '@/state/session-context'
import {
  CHECK_LABELS,
  FIELD_LABELS,
  approveChanges,
  checkStatus,
  setField,
  visibleFields,
  type CheckStatus,
} from './review'

const inputClass =
  'h-8 w-full rounded-md border border-input bg-background px-2 text-sm tabular-nums outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:border-transparent disabled:bg-transparent disabled:px-0'

// ---------------------------------------------------------------------------- small pieces

function CheckIcon({ status }: { status: CheckStatus }) {
  const label = CHECK_LABELS[status]
  const icon = {
    verified: <ShieldCheck className="size-4 text-emerald-600" />,
    'second-reader': <Check className="size-4 text-sky-600" />,
    'not-found': <ShieldAlert className="size-4 text-amber-600" />,
    disagreed: <X className="size-4 text-red-600" />,
    unparsable: <CircleAlert className="size-4 text-red-600" />,
    unchecked: <CircleHelp className="size-4 text-muted-foreground/60" />,
  }[status]
  return (
    <span title={label} aria-label={label} className="inline-flex">
      {icon}
    </span>
  )
}

function DocumentViewer({ task }: { task: TaskDetail }) {
  if (!task.has_file) {
    return <div className="grid h-full place-items-center text-sm text-muted-foreground">No file (imported history)</div>
  }
  const src = `/api/tasks/${task.card.task_id}/file`
  return task.media_type === 'application/pdf' ? (
    <iframe title="Document" src={src} className="h-[78vh] w-full rounded-md border bg-white" />
  ) : (
    <div className="h-[78vh] overflow-auto rounded-md border bg-white p-2">
      <img alt="Document scan" src={src} className="mx-auto max-w-full" />
    </div>
  )
}

// ---------------------------------------------------------------------------- review panel

function codingSignals(line: LineCoding | undefined, names: Record<string, string>): string {
  if (!line) return ''
  const parts: string[] = []
  if (line.vendor_rule_account) parts.push(`vendor history: ${line.vendor_rule_account}`)
  if (line.classifier_account)
    parts.push(`classifier: ${line.classifier_account} (${formatPercent(line.classifier_probability)})`)
  if (line.neighbour_account)
    parts.push(`similar items: ${line.neighbour_account} (${formatPercent(line.neighbour_share)})`)
  if (line.llm_account) parts.push(`model: ${line.llm_account} ${names[line.llm_account] ?? ''}`)
  return parts.join(' · ')
}

interface ReviewPanelProps {
  task: TaskDetail
  accounts: AccountSummary[]
  draft: ExtractedDocument
  setDraft: (doc: ExtractedDocument) => void
  lineAccounts: string[]
  setLineAccounts: (accounts: string[]) => void
  editable: boolean
}

function ReviewPanel({ task, accounts, draft, setDraft, lineAccounts, setLineAccounts, editable }: ReviewPanelProps) {
  const checks = useMemo(
    () => Object.fromEntries((task.extraction?.fields ?? []).map((f) => [f.field, f])),
    [task.extraction],
  )
  const names = Object.fromEntries(accounts.map((a) => [a.code, a.name]))
  const codable = accounts.filter(
    (a) => (a.type === 'expense' || a.type === 'asset') && !['bank', 'fixed_assets'].includes(a.role ?? ''),
  )
  const lines = task.coding?.lines ?? []

  return (
    <div className="space-y-5">
      {task.extraction && (
        <p className="text-xs text-muted-foreground">
          Extraction confidence {formatPercent(task.extraction.confidence)}
          {task.extraction.second_pass && ` · second reading: ${task.extraction.second_pass_reason}`}
        </p>
      )}
      <dl className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
        {visibleFields(draft).map((field) => (
          <div key={field} className="grid grid-cols-[8.5rem_1fr_1.25rem] items-center gap-2">
            <dt className="text-xs text-muted-foreground">{FIELD_LABELS[field]}</dt>
            <dd>
              <input
                aria-label={FIELD_LABELS[field]}
                className={inputClass}
                disabled={!editable}
                value={(draft[field] as string | null) ?? ''}
                onChange={(e) => setDraft(setField(draft, field, e.target.value))}
              />
            </dd>
            <CheckIcon status={checkStatus(checks[field])} />
          </div>
        ))}
      </dl>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-muted-foreground">
            <tr>
              <th className="py-1.5 pr-2 font-medium">Item</th>
              <th className="px-2 font-medium">Qty</th>
              <th className="px-2 font-medium">Unit</th>
              <th className="px-2 text-right font-medium">Amount</th>
              <th className="px-2 font-medium" title="Taxable">Tax</th>
              <th className="pl-2 font-medium">Account</th>
            </tr>
          </thead>
          <tbody>
            {draft.lines.map((item, i) => {
              const amountCheck = checkStatus(checks[`lines[${i}].amount`])
              const coding = lines[i]
              const changed = lineAccounts[i] !== task.coding?.accounts[i]
              return (
                <tr key={i} className="border-t align-top">
                  <td className="py-2 pr-2">{item.description}</td>
                  <td className="px-2 py-2 tabular-nums">{formatQuantity(item.quantity)}</td>
                  <td className="px-2 py-2 tabular-nums">{formatMoney(item.unit_price)}</td>
                  <td className="px-2 py-2 text-right tabular-nums">
                    <span className="inline-flex items-center gap-1">
                      {formatMoney(item.amount)} <CheckIcon status={amountCheck} />
                    </span>
                  </td>
                  <td className="px-2 py-2">{item.taxable ? 'T' : ''}</td>
                  <td className="py-1.5 pl-2">
                    <select
                      aria-label={`Account for line ${i + 1}`}
                      className={cn(inputClass, 'w-56', changed && 'border-violet-400 bg-violet-50')}
                      disabled={!editable}
                      value={lineAccounts[i] ?? ''}
                      onChange={(e) => {
                        const next = [...lineAccounts]
                        next[i] = e.target.value
                        setLineAccounts(next)
                      }}
                    >
                      {codable.map((a) => (
                        <option key={a.code} value={a.code}>
                          {a.code} {a.name}
                        </option>
                      ))}
                    </select>
                    {coding && (
                      <div className="mt-1 max-w-72 text-[11px] leading-snug text-muted-foreground">
                        <span
                          className={cn(
                            'mr-1 font-medium',
                            coding.confidence >= 0.9 ? 'text-emerald-700' : coding.confidence >= 0.7 ? 'text-amber-700' : 'text-red-700',
                          )}
                        >
                          {formatPercent(coding.confidence)}
                        </span>
                        {codingSignals(coding, names)}
                        {coding.reason && <div className="italic">“{coding.reason}”</div>}
                      </div>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------- explanation panels

function WhyPanel({ task }: { task: TaskDetail }) {
  const routing = task.routing
  if (!routing) return <p className="text-sm text-muted-foreground">Not routed yet.</p>
  const pct = Math.min(100, Math.round(routing.score * 100))
  return (
    <div className="space-y-4 text-sm">
      <p>{routing.summary}</p>
      <div>
        <div className="mb-1 flex justify-between text-xs text-muted-foreground">
          <span>Confidence {routing.score.toFixed(2)}</span>
          <span>Auto-post threshold {routing.threshold.toFixed(2)}</span>
        </div>
        <div className="relative h-2 rounded-full bg-muted">
          <div
            className={cn('h-2 rounded-full', routing.outcome === 'auto_post' ? 'bg-emerald-500' : 'bg-amber-500')}
            style={{ width: `${pct}%` }}
          />
          <div
            className="absolute -top-1 h-4 w-0.5 bg-foreground/60"
            style={{ left: `${Math.round(routing.threshold * 100)}%` }}
            aria-hidden
          />
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          Weakest of extraction {routing.extraction_confidence.toFixed(2)} and coding{' '}
          {routing.coding_confidence.toFixed(2)}
          {Object.keys(routing.factors).length > 0 &&
            `, then × ${Object.entries(routing.factors)
              .map(([rule, factor]) => `${factor} (${ruleLabel(rule)})`)
              .join(' × ')}`}
        </p>
      </div>
      {routing.outcome === 'needs_review' && routing.score >= routing.threshold && (
        <p className="rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground">
          Confidence alone would allow posting automatically, but the rule below always requires a
          person.
        </p>
      )}
      {routing.hits.length > 0 && (
        <ul className="space-y-2">
          {routing.hits.map((hit, i) => (
            <li key={i} className="rounded-md border px-3 py-2">
              <div className="flex items-center gap-2 font-medium">
                <span
                  className={cn(
                    'rounded px-1.5 py-0.5 text-[11px] uppercase',
                    hit.severity === 'hard' ? 'bg-red-50 text-red-700' : 'bg-amber-50 text-amber-800',
                  )}
                >
                  {hit.severity === 'hard' ? 'Needs a person' : 'Lowers confidence'}
                </span>
                {ruleLabel(hit.rule)}
              </div>
              <div className="mt-0.5 text-muted-foreground">{hit.detail}</div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function AuditPanel({ task, names }: { task: TaskDetail; names: Record<string, string> }) {
  return (
    <div className="space-y-3">
      <p className={cn('text-xs', task.audit_intact ? 'text-emerald-700' : 'font-semibold text-red-700')}>
        {task.audit_intact
          ? 'Audit trail verified: every event is chained to the one before it.'
          : 'Audit trail does not verify: an event was altered after it was written.'}
      </p>
      <ol className="relative space-y-3 border-l pl-4">
        {task.audit.map((event) => (
          <li key={event.seq} className="text-sm">
            <span
              className={cn(
                'absolute -left-1.5 mt-1.5 size-3 rounded-full border-2 border-background',
                event.actor_kind === 'human' ? 'bg-violet-500' : 'bg-slate-400',
              )}
            />
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-medium">{event.action.replaceAll('_', ' ')}</span>
              {event.to_state && <span className="text-xs text-muted-foreground">→ {event.to_state.replace('_', ' ')}</span>}
              <span className="text-xs text-muted-foreground">
                {names[event.actor] ?? event.actor} · {formatDateTime(event.occurred_at)}
              </span>
            </div>
            <AuditDetails details={event.details} />
          </li>
        ))}
      </ol>
    </div>
  )
}

function AuditDetails({ details }: { details: Record<string, unknown> }) {
  const shown = Object.entries(details).filter(([, v]) => v !== null && v !== '' && !(Array.isArray(v) && v.length === 0))
  if (shown.length === 0) return null
  return (
    <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 text-xs text-muted-foreground">
      {shown.map(([key, value]) => (
        <div key={key} className="contents">
          <dt>{key.replaceAll('_', ' ')}</dt>
          <dd className="break-words font-mono text-[11px]">
            {typeof value === 'string' || typeof value === 'number' ? String(value) : JSON.stringify(value)}
          </dd>
        </div>
      ))}
    </dl>
  )
}

function LedgerPanel({ task }: { task: TaskDetail }) {
  if (task.entries.length === 0) return <p className="text-sm text-muted-foreground">Nothing posted.</p>
  return (
    <div className="space-y-4">
      {task.entries.map((entry) => (
        <div key={entry.id} className="rounded-md border">
          <div className="flex justify-between border-b px-3 py-2 text-xs text-muted-foreground">
            <span>
              {entry.entry_date} · {entry.memo}
            </span>
            {entry.reverses && <span className="font-medium text-orange-700">Reversal</span>}
          </div>
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-1 font-medium">Account</th>
                <th className="px-3 text-right font-medium">Debit</th>
                <th className="px-3 text-right font-medium">Credit</th>
              </tr>
            </thead>
            <tbody>
              {entry.lines.map((line, i) => (
                <tr key={i} className="border-t">
                  <td className="px-3 py-1.5">
                    {line.account_code} {line.account_name}
                  </td>
                  <td className="px-3 text-right tabular-nums">{line.debit !== '0.00' ? formatMoney(line.debit) : ''}</td>
                  <td className="px-3 text-right tabular-nums">{line.credit !== '0.00' ? formatMoney(line.credit) : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  )
}

function CallsPanel({ task }: { task: TaskDetail }) {
  return (
    <table className="w-full text-sm">
      <thead className="text-left text-xs text-muted-foreground">
        <tr>
          <th className="py-1 font-medium">Step</th>
          <th className="font-medium">Model</th>
          <th className="font-medium">Prompt</th>
          <th className="text-right font-medium">Latency</th>
          <th className="text-right font-medium">Cost</th>
        </tr>
      </thead>
      <tbody>
        {task.calls.map((call, i) => (
          <tr key={i} className="border-t">
            <td className="py-1.5">{call.purpose}</td>
            <td>{call.model}</td>
            <td className="text-muted-foreground">{call.prompt_version}</td>
            <td className="text-right tabular-nums">{call.replayed ? 'replayed' : `${call.latency_ms} ms`}</td>
            <td className="text-right tabular-nums">${call.cost_usd}</td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr className="border-t font-medium">
          <td colSpan={4} className="py-1.5">
            Total
          </td>
          <td className="text-right tabular-nums">${task.cost_usd}</td>
        </tr>
      </tfoot>
    </table>
  )
}

// ---------------------------------------------------------------------------- actions

const NOTE_ACTIONS: Record<SimpleAction, { title: string; description: string; required: boolean }> = {
  reject: { title: 'Reject document', description: 'It will not be posted. Say why.', required: true },
  block: { title: 'Block for information', description: 'What is missing, and from whom?', required: true },
  unblock: { title: 'Return to review', description: 'Optional note.', required: false },
  reopen: {
    title: 'Reopen posted document',
    description: 'A reversing journal entry is posted and the document returns to review. Say why.',
    required: true,
  },
  retry: { title: 'Retry processing', description: 'Queue the document for the pipeline again.', required: false },
}

function ActionBar({
  task,
  onApprove,
  approving,
}: {
  task: TaskDetail
  onApprove: (note: string) => void
  approving: boolean
}) {
  const { actorId } = useSession()
  const action = useAction(task.card.task_id)
  const assign = useAssign(task.card.task_id)
  const users = useUsers()
  const [dialog, setDialog] = useState<SimpleAction | 'approve' | null>(null)
  const [note, setNote] = useState('')
  const state = task.card.state
  const unsupported = task.card.doc_type === 'other'

  const run = () => {
    if (dialog === 'approve') {
      onApprove(note)
    } else if (dialog) {
      action.mutate(
        { action: dialog, reviewer_id: actorId, note },
        {
          onSuccess: (r) => toast.success(`Task is now ${r.state.replace('_', ' ')}`),
          onError: (error) => toast.error(error.message),
        },
      )
    }
    setDialog(null)
    setNote('')
  }

  const meta = dialog && dialog !== 'approve' ? NOTE_ACTIONS[dialog] : null

  return (
    <div className="flex flex-wrap items-center gap-2">
      {state === 'needs_review' && (
        <>
          <Button
            onClick={() => setDialog('approve')}
            disabled={approving || unsupported}
            title={unsupported ? 'Unsupported documents cannot be posted; reject instead' : 'Approve and post'}
          >
            Approve and post
          </Button>
          <Button variant="outline" onClick={() => setDialog('block')}>
            Block
          </Button>
          <Button variant="destructive" onClick={() => setDialog('reject')}>
            Reject
          </Button>
        </>
      )}
      {state === 'blocked' && (
        <Button variant="outline" onClick={() => setDialog('unblock')}>
          Return to review
        </Button>
      )}
      {state === 'posted' && task.has_file && (
        <Button variant="outline" onClick={() => setDialog('reopen')}>
          Reopen
        </Button>
      )}
      {state === 'failed' && <Button onClick={() => setDialog('retry')}>Retry</Button>}
      {['needs_review', 'blocked'].includes(state) && (
        <label className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground">
          Assigned to
          <select
            aria-label="Assignee"
            className="h-8 rounded-md border border-input bg-background px-2 text-sm"
            value={task.card.assignee_id ?? ''}
            onChange={(e) =>
              assign.mutate(
                { reviewer_id: actorId, assignee_id: e.target.value || null },
                { onError: (error) => toast.error(error.message) },
              )
            }
          >
            <option value="">Nobody</option>
            {users.data?.map((u) => (
              <option key={u.id} value={u.id}>
                {u.name}
              </option>
            ))}
          </select>
        </label>
      )}

      <Dialog open={dialog !== null} onOpenChange={(open) => !open && setDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{dialog === 'approve' ? 'Approve and post' : meta?.title}</DialogTitle>
            <DialogDescription>
              {dialog === 'approve'
                ? 'The document is re-checked, posted to the ledger, and its coding is added to the knowledge store.'
                : meta?.description}
            </DialogDescription>
          </DialogHeader>
          <Textarea
            aria-label="Note"
            placeholder={dialog === 'approve' ? 'Optional note' : 'Reason'}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDialog(null)}>
              Cancel
            </Button>
            <Button onClick={run} disabled={Boolean(meta?.required) && note.trim() === ''}>
              Confirm
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

// ---------------------------------------------------------------------------- page

export function TaskPage() {
  const { taskId = '' } = useParams()
  const task = useTask(taskId)
  if (task.isLoading) return <Skeleton className="h-[80vh]" />
  if (task.isError || !task.data) return <p className="text-sm text-red-700">Task not found.</p>
  const detail = task.data
  // Re-mount the view (and reset the review draft) whenever the task changes state or content.
  const version = `${detail.card.task_id}:${detail.card.state}:${detail.audit.length}`
  return <TaskView key={version} detail={detail} />
}

function TaskView({ detail }: { detail: TaskDetail }) {
  const { actorId } = useSession()
  const accounts = useAccounts(detail.client_id)
  const users = useUsers()
  const approve = useApprove(detail.card.task_id)
  const [draft, setDraft] = useState<ExtractedDocument | null>(detail.extracted)
  const [lineAccounts, setLineAccounts] = useState<string[]>(
    detail.coding?.accounts ?? detail.line_accounts ?? [],
  )

  const names = Object.fromEntries((users.data ?? []).map((u) => [u.id, u.name]))
  const editable = detail.card.state === 'needs_review'
  const onApprove = (note: string) => {
    if (!detail.extracted || !draft) return
    const changes = approveChanges(detail.extracted, draft, detail.coding?.accounts ?? [], lineAccounts)
    approve.mutate(
      { reviewer_id: actorId, note, ...changes },
      {
        onSuccess: (r) =>
          toast.success(`Posted. ${r.knowledge_entries} line${r.knowledge_entries === 1 ? '' : 's'} added to the knowledge store.`),
        onError: (error) => toast.error(error.message),
      },
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link to="/" className="mb-1 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
            <ArrowLeft className="size-3" /> Board
          </Link>
          <h1 className="text-xl font-semibold tracking-tight">
            {detail.card.vendor ?? detail.card.filename}
            <span className="ml-2 text-base font-normal text-muted-foreground">
              {docTypeLabel(detail.card.doc_type)}
              {detail.card.document_number && ` ${detail.card.document_number}`}
            </span>
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <StateBadge state={detail.card.state} />
            <span>{formatMoney(detail.card.total)}</span>
            <span>· in this stage {formatAge(detail.card.age_seconds)}</span>
            <span>· received {formatDateTime(detail.card.received_at)}</span>
          </div>
        </div>
        <ActionBar task={detail} onApprove={onApprove} approving={approve.isPending} />
      </div>

      {detail.last_error && (
        <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          Processing failed after {detail.attempts} attempt{detail.attempts === 1 ? '' : 's'}: {detail.last_error}
        </p>
      )}
      {approve.error && (
        <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">{approve.error.message}</p>
      )}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
        <DocumentViewer task={detail} />
        <div className="min-w-0 space-y-4">
          <AssistantCard
            task={detail}
            accounts={accounts.data ?? []}
            lineAccounts={lineAccounts}
            setLineAccounts={setLineAccounts}
            editable={editable}
          />
          <div className="rounded-xl border bg-background p-4">
            <Tabs defaultValue={detail.card.state === 'needs_review' ? 'review' : 'why'}>
              <TabsList>
                <TabsTrigger value="review">Document</TabsTrigger>
                <TabsTrigger value="why">Why</TabsTrigger>
                <TabsTrigger value="audit">Audit trail</TabsTrigger>
                <TabsTrigger value="ledger">Ledger</TabsTrigger>
                <TabsTrigger value="calls">Model calls</TabsTrigger>
              </TabsList>
              <TabsContent value="review" className="pt-4">
                {draft ? (
                  <ReviewPanel
                    task={detail}
                    accounts={accounts.data ?? []}
                    draft={draft}
                    setDraft={setDraft}
                    lineAccounts={lineAccounts}
                    setLineAccounts={setLineAccounts}
                    editable={editable}
                  />
                ) : (
                  <p className="text-sm text-muted-foreground">Not extracted yet.</p>
                )}
              </TabsContent>
              <TabsContent value="why" className="pt-4">
                <WhyPanel task={detail} />
              </TabsContent>
              <TabsContent value="audit" className="pt-4">
                <AuditPanel task={detail} names={names} />
              </TabsContent>
              <TabsContent value="ledger" className="pt-4">
                <LedgerPanel task={detail} />
              </TabsContent>
              <TabsContent value="calls" className="pt-4">
                <CallsPanel task={detail} />
              </TabsContent>
            </Tabs>
          </div>
        </div>
      </div>
    </div>
  )
}
