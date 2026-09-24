import { useEffect, type ReactNode } from 'react'
import { NavLink } from 'react-router-dom'
import { Clock, FastForward, Inbox, RotateCcw } from 'lucide-react'
import { toast } from 'sonner'
import { useClients, useClock, useDemo, useLiveEvents, useUsers, type Connection } from '@/api/hooks'
import { Button } from '@/components/ui/button'
import { formatDateTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useSession } from '@/state/session-context'

const NAV = [
  { to: '/', label: 'Board' },
  { to: '/ledger', label: 'Ledger' },
  { to: '/knowledge', label: 'Knowledge' },
]

const selectClass =
  'h-8 rounded-md border border-input bg-background px-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/50'

function LiveDot({ status }: { status: Connection }) {
  const tone = status === 'live' ? 'bg-emerald-500' : status === 'connecting' ? 'bg-amber-400' : 'bg-red-500'
  const label = status === 'live' ? 'Live' : status === 'connecting' ? 'Connecting' : 'Offline'
  return (
    <span className="flex items-center gap-1.5 text-xs text-muted-foreground" title={`Live updates: ${label}`}>
      <span className={cn('size-2 rounded-full', tone, status === 'live' && 'animate-pulse')} />
      {label}
    </span>
  )
}

function DemoControls({ clientId }: { clientId: string }) {
  const { feed, advance, reset } = useDemo(clientId)
  const onError = (error: Error) => toast.error(error.message)
  return (
    <div className="flex items-center gap-1">
      <Button
        size="sm"
        variant="outline"
        onClick={() =>
          feed.mutate(5, {
            onSuccess: (r) => toast.success(`Queued ${r.task_ids.length} documents`),
            onError,
          })
        }
        disabled={feed.isPending}
        title="Queue the next five generated documents"
      >
        <Inbox data-icon="inline-start" />
        Feed 5
      </Button>
      <Button
        size="sm"
        variant="outline"
        onClick={() => advance.mutate(24, { onError })}
        disabled={advance.isPending}
        title="Move the shared clock forward one day"
      >
        <FastForward data-icon="inline-start" />
        +1 day
      </Button>
      <Button size="sm" variant="ghost" onClick={() => reset.mutate(undefined, { onError })} title="Reset the clock">
        <RotateCcw />
      </Button>
    </div>
  )
}

export function AppShell({ children }: { children: ReactNode }) {
  const { clientId, actorId, setClientId, setActorId } = useSession()
  const clients = useClients()
  const users = useUsers()
  const clock = useClock()
  const status = useLiveEvents(clientId)

  useEffect(() => {
    const list = clients.data
    if (list && list.length > 0 && !list.some((c) => c.id === clientId)) {
      setClientId(list.find((c) => c.id === 'fernhill')?.id ?? list[0].id)
    }
  }, [clients.data, clientId, setClientId])

  const shifted = (clock.data?.offset_hours ?? 0) > 0

  return (
    <div className="min-h-screen bg-muted/30">
      <header className="sticky top-0 z-20 border-b bg-background/95 backdrop-blur">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-2.5">
          <div className="flex items-baseline gap-2">
            <span className="text-base font-semibold tracking-tight">AccrueBoard</span>
            <LiveDot status={status} />
          </div>
          <nav className="flex items-center gap-1">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === '/'}
                className={({ isActive }) =>
                  cn(
                    'rounded-md px-2.5 py-1 text-sm transition-colors',
                    isActive ? 'bg-muted font-medium text-foreground' : 'text-muted-foreground hover:text-foreground',
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              Client
              <select
                aria-label="Client"
                className={selectClass}
                value={clientId}
                onChange={(e) => setClientId(e.target.value)}
              >
                {clients.data?.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              Acting as
              <select
                aria-label="Acting as"
                className={selectClass}
                value={actorId}
                onChange={(e) => setActorId(e.target.value)}
              >
                {users.data?.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.name} ({u.role})
                  </option>
                ))}
              </select>
            </label>
            {clock.data && (
              <span
                className={cn(
                  'flex items-center gap-1 rounded-md px-2 py-1 text-xs tabular-nums',
                  shifted ? 'bg-violet-50 text-violet-800' : 'text-muted-foreground',
                )}
                title={shifted ? `Demo clock is ${clock.data.offset_hours.toFixed(0)}h ahead` : 'Current time'}
              >
                <Clock className="size-3.5" />
                {formatDateTime(clock.data.now)}
                {shifted && ` (+${clock.data.offset_hours.toFixed(0)}h)`}
              </span>
            )}
            {clock.data?.demo_mode && clientId && <DemoControls clientId={clientId} />}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-[1600px] px-4 py-5">{children}</main>
    </div>
  )
}
