import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import App from './App'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test.each([false, true])('Refresh retains failed-claim identity until cleanup succeeds (busy=%s)', async (initiallyBusy) => {
  localStorage.clear()
  const calls: string[] = []
  let busy = initiallyBusy
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const path = new URL(String(input), 'http://localhost').pathname
    const route = `${init.method ?? 'GET'} ${path}`
    calls.push(route)
    let status = 200
    let body: unknown
    const headers = new Headers({ 'Content-Type': 'application/json' })
    switch (route) {
      case 'POST /api/v1/session/open': body = { job: null }; break
      case 'POST /api/v1/session/heartbeat':
      case 'POST /api/v1/session/close': body = { ok: true }; break
      case 'GET /api/v1/devices':
        body = [{ device_id: 'device-1', filesystem: 'exfat', label: 'SCIENCE', size_bytes: 1000 }]
        break
      case 'GET /api/v1/devices/availability':
        body = { unsupported_removable_media_detected: false }; break
      case 'GET /api/v1/runtime':
        body = { mode: 'inference_enabled', status: 'ready', source: 'platform', gpu_lock_available: true }
        break
      case 'GET /api/v1/devices/device-1/tree': body = []; break
      case 'POST /api/v1/devices/device-1/preflight':
        status = 503
        headers.set('X-PICASSO-Preflight-ID', 'failed-claim')
        body = { detail: 'USB cleanup is incomplete; retry Preflight or Refresh' }
        break
      case 'DELETE /api/v1/preflights/failed-claim':
        status = busy ? 503 : 200
        body = busy ? { detail: 'USB cleanup is incomplete; retry release' } : {
          preflight_id: 'failed-claim', status: 'cancelled', stage: 'cancelled',
          processed: 0, total: null, percentage: null, eta_seconds: null, summary: null, error: null,
        }
        break
      default: throw new Error(`Unexpected request: ${route}`)
    }
    return new Response(JSON.stringify(body), { status, headers })
  }))
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Run preflight' }))
  await screen.findByText('USB cleanup is incomplete; retry Preflight or Refresh')
  expect((screen.getByRole('button', { name: 'Run preflight' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.queryByRole('button', { name: 'Confirm and start' })).toBeNull()
  const discoveryCount = calls.filter(route => route === 'GET /api/v1/devices').length
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await waitFor(() => expect(calls).toContain('DELETE /api/v1/preflights/failed-claim'))
  if (initiallyBusy) {
    await screen.findByText('Could not release preflight. Try again.')
    expect(calls.filter(route => route === 'GET /api/v1/devices')).toHaveLength(discoveryCount)
    expect((screen.getByRole('button', { name: 'Run preflight' }) as HTMLButtonElement).disabled).toBe(true)
    busy = false
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  }
  await waitFor(() => expect((screen.getByRole('button', { name: 'Run preflight' }) as HTMLButtonElement).disabled).toBe(false))
  expect(calls.filter(route => route === 'DELETE /api/v1/preflights/failed-claim')).toHaveLength(initiallyBusy ? 2 : 1)
  expect(calls.filter(route => route === 'POST /api/v1/devices/device-1/preflight')).toHaveLength(1)
  expect(calls).not.toContain('POST /api/v1/jobs')
})
