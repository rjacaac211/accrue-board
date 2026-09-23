export type Health = {
  status: 'ok'
  version: string
  database: 'ok' | 'unavailable'
}

export async function fetchHealth(signal?: AbortSignal): Promise<Health> {
  const response = await fetch('/api/health', { signal })
  if (!response.ok) {
    throw new Error(`Health check failed: HTTP ${response.status}`)
  }
  return (await response.json()) as Health
}
