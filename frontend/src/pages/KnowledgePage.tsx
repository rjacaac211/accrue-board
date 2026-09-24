import { useDeferredValue, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAccounts, useKnowledge } from '@/api/hooks'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { formatDateTime, formatMoney } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useSession } from '@/state/session-context'

const SOURCE_TONES = {
  history: 'bg-slate-100 text-slate-600',
  confirmed: 'bg-emerald-50 text-emerald-700',
  corrected: 'bg-violet-50 text-violet-700',
} as const

export function KnowledgePage() {
  const { clientId } = useSession()
  const [vendor, setVendor] = useState('')
  const query = useDeferredValue(vendor)
  const { data, isLoading } = useKnowledge(clientId, query)
  const accounts = useAccounts(clientId)
  const names = Object.fromEntries((accounts.data ?? []).map((a) => [a.code, a.name]))

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Knowledge store</h1>
          <p className="max-w-2xl text-sm text-muted-foreground">
            Confirmed codings the pipeline retrieves as examples. Reviewer approvals add entries here, and
            corrections teach the next similar document. Newest first.
          </p>
        </div>
        <Input
          aria-label="Filter by vendor"
          placeholder="Filter by vendor"
          className="w-64"
          value={vendor}
          onChange={(e) => setVendor(e.target.value)}
        />
      </div>
      {isLoading ? (
        <Skeleton className="h-96" />
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-background">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Vendor</th>
                <th className="px-3 font-medium">Item</th>
                <th className="px-3 text-right font-medium">Amount</th>
                <th className="px-3 font-medium">Account</th>
                <th className="px-3 font-medium">Source</th>
                <th className="px-3 font-medium">Added</th>
              </tr>
            </thead>
            <tbody>
              {data?.map((item) => (
                <tr key={item.id} className="border-t">
                  <td className="px-3 py-1.5">{item.vendor_name}</td>
                  <td className="px-3">{item.description}</td>
                  <td className="px-3 text-right tabular-nums">{formatMoney(item.amount)}</td>
                  <td className="px-3">
                    {item.account} {names[item.account] ?? ''}
                  </td>
                  <td className="px-3">
                    <span className={cn('rounded px-1.5 py-0.5 text-xs', SOURCE_TONES[item.source])}>{item.source}</span>
                  </td>
                  <td className="px-3 text-xs text-muted-foreground">
                    {item.document_ref && item.source !== 'history' ? (
                      <Link to={`/tasks/${item.id.split(':')[0]}`} className="hover:underline">
                        {formatDateTime(item.created_at)}
                      </Link>
                    ) : (
                      formatDateTime(item.created_at)
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
