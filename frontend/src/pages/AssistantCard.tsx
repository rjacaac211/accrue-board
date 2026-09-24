import { Bot, RotateCw } from 'lucide-react'
import { toast } from 'sonner'
import { useRunAssistant } from '@/api/hooks'
import type { AccountSummary, AssistantRun, TaskDetail } from '@/api/types'
import { Button } from '@/components/ui/button'
import { formatDateTime, ruleLabel } from '@/lib/format'
import { cn } from '@/lib/utils'
import {
  ACTION_LABELS,
  ACTION_TONES,
  VERDICT_LABELS,
  accountChanges,
  applySuggestion,
  describeInput,
  toolLabel,
} from './assistant'

interface AssistantCardProps {
  task: TaskDetail
  accounts: AccountSummary[]
  lineAccounts: string[]
  setLineAccounts: (accounts: string[]) => void
  editable: boolean
}

export function AssistantCard({ task, accounts, lineAccounts, setLineAccounts, editable }: AssistantCardProps) {
  const runAssistant = useRunAssistant(task.card.task_id)
  const reviewable = ['needs_review', 'blocked'].includes(task.card.state)
  const run = task.assistant
  if (!run && !reviewable) return null

  const ask = () =>
    runAssistant.mutate(undefined, {
      onSuccess: (r) =>
        r.status === 'done' ? toast.success('The assistant has a recommendation') : toast.error(r.error ?? 'No recommendation'),
      onError: (error) => toast.error(error.message),
    })

  return (
    <section aria-label="Review assistant" className="rounded-xl border border-sky-200 bg-sky-50/40 p-4">
      <header className="flex flex-wrap items-center gap-2">
        <Bot className="size-4 text-sky-700" />
        <h2 className="text-sm font-semibold">Review assistant</h2>
        <span className="text-xs text-muted-foreground">suggests only; you decide</span>
        {reviewable && (
          <Button size="sm" variant="ghost" className="ml-auto h-7" onClick={ask} disabled={runAssistant.isPending}>
            <RotateCw className={cn('size-3.5', runAssistant.isPending && 'animate-spin')} />
            {runAssistant.isPending ? 'Investigating…' : run ? 'Run again' : 'Investigate'}
          </Button>
        )}
      </header>

      {!run && !runAssistant.isPending && (
        <p className="mt-2 text-sm text-muted-foreground">Not investigated yet.</p>
      )}
      {run && (
        <RunView
          run={run}
          accounts={accounts}
          lineAccounts={lineAccounts}
          setLineAccounts={setLineAccounts}
          editable={editable}
        />
      )}
    </section>
  )
}

function RunView({
  run,
  accounts,
  lineAccounts,
  setLineAccounts,
  editable,
}: {
  run: AssistantRun
  accounts: AccountSummary[]
  lineAccounts: string[]
  setLineAccounts: (accounts: string[]) => void
  editable: boolean
}) {
  const names = Object.fromEntries(accounts.map((a) => [a.code, a.name]))
  const suggestion = run.suggestion
  const changes = accountChanges(run, lineAccounts)
  const footer = `${run.model} · ${run.model_turns} turn${run.model_turns === 1 ? '' : 's'} · ${
    run.replayed ? 'replayed' : `$${run.cost_usd}`
  } · ${formatDateTime(run.ran_at)}`

  if (!suggestion) {
    return (
      <div className="mt-2 space-y-1 text-sm">
        <p className="text-red-800">The assistant could not reach a recommendation: {run.error}</p>
        <p className="text-xs text-muted-foreground">{footer}</p>
      </div>
    )
  }

  return (
    <div className="mt-3 space-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">Recommends</span>
        <span
          className={cn(
            'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold ring-1 ring-inset',
            ACTION_TONES[suggestion.action],
          )}
        >
          {ACTION_LABELS[suggestion.action]}
        </span>
      </div>
      <p>{suggestion.summary}</p>
      {suggestion.question && (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-amber-900">
          <span className="font-medium">Ask: </span>
          {suggestion.question}
        </p>
      )}

      {suggestion.rule_assessments.length > 0 && (
        <ul className="space-y-1.5">
          {suggestion.rule_assessments.map((a, i) => (
            <li key={i} className="text-xs">
              <span className="font-medium">{ruleLabel(a.rule)}</span>
              <span
                className={cn(
                  'ml-2 rounded px-1.5 py-0.5 text-[11px]',
                  a.verdict === 'false_positive'
                    ? 'bg-emerald-100 text-emerald-800'
                    : a.verdict === 'confirmed'
                      ? 'bg-slate-200 text-slate-800'
                      : 'bg-amber-100 text-amber-800',
                )}
              >
                {VERDICT_LABELS[a.verdict]}
              </span>
              <div className="text-muted-foreground">{a.reason}</div>
            </li>
          ))}
        </ul>
      )}

      {changes.length > 0 && (
        <div className="rounded-md border bg-background px-3 py-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs font-medium">
              Suggests a different account on {changes.length} line{changes.length === 1 ? '' : 's'}
            </span>
            {editable && (
              <Button size="sm" variant="outline" className="h-7" onClick={() => setLineAccounts(applySuggestion(run, lineAccounts))}>
                Use suggested accounts
              </Button>
            )}
          </div>
          <ul className="mt-1 space-y-1 text-xs">
            {changes.map((c) => (
              <li key={c.line}>
                Line {c.line + 1}: {c.current ?? '—'} {c.current ? names[c.current] : ''} →{' '}
                <span className="font-medium">
                  {c.suggested} {names[c.suggested] ?? ''}
                </span>
                <span className="text-muted-foreground"> · {c.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {suggestion.evidence.length > 0 && (
        <div>
          <h3 className="text-xs font-medium text-muted-foreground">Evidence</h3>
          <ul className="mt-1 list-disc space-y-1 pl-4 text-xs">
            {suggestion.evidence.map((e, i) => (
              <li key={i}>
                <span className="text-muted-foreground">{toolLabel(e.source)}: </span>
                {e.detail}
                {e.document_id && <span className="ml-1 font-mono text-[11px] text-muted-foreground">({e.document_id})</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <details className="text-xs">
        <summary className="cursor-pointer text-muted-foreground">
          Investigation: {run.steps.length} tool call{run.steps.length === 1 ? '' : 's'}
        </summary>
        <ol className="mt-1 space-y-0.5 pl-4">
          {run.steps.map((step, i) => (
            <li key={i} className={cn(step.is_error && 'text-red-700')}>
              {step.turn}. {toolLabel(step.tool)}
              {step.tool !== 'submit_review' && describeInput(step.input) && (
                <span className="text-muted-foreground"> · {describeInput(step.input)}</span>
              )}
              {step.is_error && <span> · {step.output.slice(0, 160)}</span>}
            </li>
          ))}
        </ol>
      </details>
      <p className="text-[11px] text-muted-foreground">{footer}</p>
    </div>
  )
}
