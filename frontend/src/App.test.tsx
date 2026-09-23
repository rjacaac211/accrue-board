import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('App', () => {
  it('shows API and database status from the health endpoint', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: 'ok', version: '0.1.0', database: 'ok' })),
      ),
    )
    render(<App />)
    expect(await screen.findByText('v0.1.0')).toBeInTheDocument()
    expect(screen.getByText('ok')).toBeInTheDocument()
  })

  it('reports an unreachable API', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('network')))
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<App />)
    expect(await screen.findByRole('alert')).toHaveTextContent('API unreachable.')
  })
})
