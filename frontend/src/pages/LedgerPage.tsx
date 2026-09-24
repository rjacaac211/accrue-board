import { Link } from 'react-router-dom'
import { useLedger } from '@/api/hooks'
import { Skeleton } from '@/components/ui/skeleton'
import { formatMoney } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useSession } from '@/state/session-context'

export function LedgerPage() {
  const { clientId } = useSession()
  const { data, isLoading } = useLedger(clientId)
  if (isLoading || !data) return <Skeleton className="h-96" />

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      <section className="rounded-xl border bg-background p-4">
        <div className="mb-3 flex items-baseline justify-between">
          <h1 className="text-lg font-semibold">Trial balance</h1>
          <span className={cn('text-xs font-medium', data.balanced ? 'text-emerald-700' : 'text-red-700')}>
            {data.balanced ? 'Debits equal credits' : 'Out of balance'}
          </span>
        </div>
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-muted-foreground">
            <tr>
              <th className="py-1 font-medium">Account</th>
              <th className="text-right font-medium">Debit</th>
              <th className="text-right font-medium">Credit</th>
            </tr>
          </thead>
          <tbody>
            {data.trial_balance.map((row) => {
              const negative = row.balance.startsWith('-')
              return (
                <tr key={row.account_code} className="border-t">
                  <td className="py-1.5">
                    {row.account_code} {row.account_name}
                  </td>
                  <td className="text-right tabular-nums">{negative ? '' : formatMoney(row.balance)}</td>
                  <td className="text-right tabular-nums">{negative ? formatMoney(row.balance.slice(1)) : ''}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </section>

      <section className="rounded-xl border bg-background p-4">
        <h2 className="mb-3 text-lg font-semibold">Recent journal entries</h2>
        <div className="space-y-3">
          {data.entries.map((entry) => (
            <div key={entry.id} className="rounded-md border">
              <div className="flex flex-wrap justify-between gap-2 border-b px-3 py-1.5 text-xs text-muted-foreground">
                <span>
                  {entry.entry_date} · {entry.memo}
                </span>
                {entry.task_id && (
                  <Link className="hover:text-foreground hover:underline" to={`/tasks/${entry.task_id}`}>
                    {entry.reverses ? 'Reversal · ' : ''}open task
                  </Link>
                )}
              </div>
              <table className="w-full text-sm">
                <tbody>
                  {entry.lines.map((line, i) => (
                    <tr key={i}>
                      <td className="px-3 py-1">
                        {line.account_code} {line.account_name}
                      </td>
                      <td className="w-28 px-3 text-right tabular-nums">
                        {line.debit !== '0.00' ? formatMoney(line.debit) : ''}
                      </td>
                      <td className="w-28 px-3 text-right tabular-nums">
                        {line.credit !== '0.00' ? formatMoney(line.credit) : ''}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
