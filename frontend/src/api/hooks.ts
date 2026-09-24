import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getJson, postFile, postJson } from './client'
import type {
  AccountSummary,
  ActionResult,
  AssistantRun,
  Bottlenecks,
  Card,
  ClientSummary,
  ClockView,
  ExtractedDocument,
  KnowledgeItem,
  LedgerView,
  Stats,
  TaskDetail,
  UserSummary,
} from './types'

export const keys = {
  clients: ['clients'] as const,
  users: ['users'] as const,
  clock: ['clock'] as const,
  accounts: (clientId: string) => ['accounts', clientId] as const,
  board: (clientId: string) => ['board', clientId] as const,
  bottlenecks: (clientId: string) => ['bottlenecks', clientId] as const,
  stats: (clientId: string) => ['stats', clientId] as const,
  ledger: (clientId: string) => ['ledger', clientId] as const,
  knowledge: (clientId: string, vendor: string) => ['knowledge', clientId, vendor] as const,
  task: (taskId: string) => ['task', taskId] as const,
}

export const useClients = () =>
  useQuery({ queryKey: keys.clients, queryFn: ({ signal }) => getJson<ClientSummary[]>('/api/clients', signal) })

export const useUsers = () =>
  useQuery({ queryKey: keys.users, queryFn: ({ signal }) => getJson<UserSummary[]>('/api/users', signal) })

export const useClock = () =>
  useQuery({ queryKey: keys.clock, queryFn: ({ signal }) => getJson<ClockView>('/api/clock', signal) })

export const useAccounts = (clientId: string) =>
  useQuery({
    queryKey: keys.accounts(clientId),
    queryFn: ({ signal }) => getJson<AccountSummary[]>(`/api/clients/${clientId}/accounts`, signal),
    enabled: Boolean(clientId),
    staleTime: Infinity,
  })

export const useBoard = (clientId: string) =>
  useQuery({
    queryKey: keys.board(clientId),
    queryFn: ({ signal }) => getJson<Card[]>(`/api/clients/${clientId}/board`, signal),
    enabled: Boolean(clientId),
  })

export const useBottlenecks = (clientId: string) =>
  useQuery({
    queryKey: keys.bottlenecks(clientId),
    queryFn: ({ signal }) => getJson<Bottlenecks>(`/api/clients/${clientId}/bottlenecks`, signal),
    enabled: Boolean(clientId),
  })

export const useStats = (clientId: string) =>
  useQuery({
    queryKey: keys.stats(clientId),
    queryFn: ({ signal }) => getJson<Stats>(`/api/clients/${clientId}/stats`, signal),
    enabled: Boolean(clientId),
  })

export const useLedger = (clientId: string) =>
  useQuery({
    queryKey: keys.ledger(clientId),
    queryFn: ({ signal }) => getJson<LedgerView>(`/api/clients/${clientId}/ledger?limit=100`, signal),
    enabled: Boolean(clientId),
  })

export const useKnowledge = (clientId: string, vendor: string) =>
  useQuery({
    queryKey: keys.knowledge(clientId, vendor),
    queryFn: ({ signal }) =>
      getJson<KnowledgeItem[]>(
        `/api/clients/${clientId}/knowledge?limit=200${vendor ? `&vendor=${encodeURIComponent(vendor)}` : ''}`,
        signal,
      ),
    enabled: Boolean(clientId),
  })

export const useTask = (taskId: string) =>
  useQuery({
    queryKey: keys.task(taskId),
    queryFn: ({ signal }) => getJson<TaskDetail>(`/api/tasks/${taskId}`, signal),
    enabled: Boolean(taskId),
  })

export type SimpleAction = 'reject' | 'block' | 'unblock' | 'reopen' | 'retry'

export interface ApprovePayload {
  reviewer_id: string
  note?: string
  document?: ExtractedDocument
  accounts?: string[]
}

function useInvalidateAll() {
  const client = useQueryClient()
  return () =>
    Promise.all(
      ['board', 'task', 'bottlenecks', 'stats', 'ledger', 'knowledge'].map((key) =>
        client.invalidateQueries({ queryKey: [key] }),
      ),
    )
}

export function useApprove(taskId: string) {
  const invalidate = useInvalidateAll()
  return useMutation({
    mutationFn: (payload: ApprovePayload) =>
      postJson<ActionResult>(`/api/tasks/${taskId}/approve`, payload),
    onSuccess: invalidate,
  })
}

export function useAction(taskId: string) {
  const invalidate = useInvalidateAll()
  return useMutation({
    mutationFn: ({ action, reviewer_id, note }: { action: SimpleAction; reviewer_id: string; note: string }) =>
      postJson<ActionResult>(`/api/tasks/${taskId}/${action}`, { reviewer_id, note }),
    onSuccess: invalidate,
  })
}

export function useRunAssistant(taskId: string) {
  const invalidate = useInvalidateAll()
  return useMutation({
    mutationFn: () => postJson<AssistantRun>(`/api/tasks/${taskId}/assistant`, {}),
    onSuccess: invalidate,
  })
}

export function useAssign(taskId: string) {
  const invalidate = useInvalidateAll()
  return useMutation({
    mutationFn: ({ reviewer_id, assignee_id }: { reviewer_id: string; assignee_id: string | null }) =>
      postJson<ActionResult>(`/api/tasks/${taskId}/assign`, { reviewer_id, assignee_id }),
    onSuccess: invalidate,
  })
}

export function useUpload(clientId: string) {
  const invalidate = useInvalidateAll()
  return useMutation({
    mutationFn: (file: File) => postFile<{ task_id: string }>(`/api/clients/${clientId}/documents`, file),
    onSuccess: invalidate,
  })
}

export function useDemo(clientId: string) {
  const client = useQueryClient()
  const invalidate = useInvalidateAll()
  const feed = useMutation({
    mutationFn: (count: number) =>
      postJson<{ task_ids: string[] }>(`/api/demo/clients/${clientId}/feed`, { count }),
    onSuccess: invalidate,
  })
  const advance = useMutation({
    mutationFn: (hours: number) => postJson<ClockView>('/api/demo/clock/advance', { hours }),
    onSuccess: async (clock) => {
      client.setQueryData(keys.clock, clock)
      await invalidate()
    },
  })
  const reset = useMutation({
    mutationFn: () => postJson<ClockView>('/api/demo/clock/reset', {}),
    onSuccess: async (clock) => {
      client.setQueryData(keys.clock, clock)
      await invalidate()
    },
  })
  return { feed, advance, reset }
}

export type Connection = 'connecting' | 'live' | 'offline'

/** Subscribe to server-sent task events and keep the cached queries fresh. */
export function useLiveEvents(clientId: string): Connection {
  const client = useQueryClient()
  const [status, setStatus] = useState<Connection>('connecting')

  useEffect(() => {
    if (!clientId || typeof EventSource === 'undefined') return
    const source = new EventSource(`/api/events?client_id=${encodeURIComponent(clientId)}`)
    source.addEventListener('open', () => setStatus('live'))
    source.addEventListener('error', () => setStatus('offline'))
    source.addEventListener('hello', () => setStatus('live'))
    source.addEventListener('task', (event) => {
      const data = JSON.parse((event as MessageEvent<string>).data) as { task_id: string }
      void client.invalidateQueries({ queryKey: keys.board(clientId) })
      void client.invalidateQueries({ queryKey: keys.bottlenecks(clientId) })
      void client.invalidateQueries({ queryKey: keys.stats(clientId) })
      void client.invalidateQueries({ queryKey: keys.task(data.task_id) })
    })
    source.addEventListener('heartbeat', () => {
      void client.invalidateQueries({ queryKey: keys.clock })
      void client.invalidateQueries({ queryKey: keys.bottlenecks(clientId) })
      void client.invalidateQueries({ queryKey: keys.board(clientId) })
    })
    return () => source.close()
  }, [client, clientId])

  return status
}
