import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from '@/components/AppShell'
import { Toaster } from '@/components/ui/sonner'
import { BoardPage } from '@/pages/BoardPage'
import { KnowledgePage } from '@/pages/KnowledgePage'
import { LedgerPage } from '@/pages/LedgerPage'
import { TaskPage } from '@/pages/TaskPage'
import { makeQueryClient } from '@/lib/query'
import { SessionProvider } from '@/state/session'

export default function App({ client = makeQueryClient() }: { client?: QueryClient }) {
  return (
    <QueryClientProvider client={client}>
      <SessionProvider>
        <BrowserRouter>
          <AppShell>
            <Routes>
              <Route path="/" element={<BoardPage />} />
              <Route path="/tasks/:taskId" element={<TaskPage />} />
              <Route path="/ledger" element={<LedgerPage />} />
              <Route path="/knowledge" element={<KnowledgePage />} />
              <Route path="*" element={<p className="text-sm text-muted-foreground">Page not found.</p>} />
            </Routes>
          </AppShell>
        </BrowserRouter>
        <Toaster richColors position="bottom-right" />
      </SessionProvider>
    </QueryClientProvider>
  )
}
