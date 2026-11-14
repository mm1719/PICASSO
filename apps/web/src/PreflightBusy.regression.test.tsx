import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import App from './App'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test.each([
  ['native mount is busy', 503, true],
  ['USB host service request failed', 503, false],
  ['native USB view was not restored', 503, false],
  ['native mount is busy', 409, false],
  ['lost response', 0, false],
] as const)('initial refusal %s (%s) preserves uncertainty where required', async (detail, code, cleanBusy) => {
  localStorage.clear()
  const calls: string[] = []
  let documentClosed = false
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const path = new URL(String(input), 'http://localhost').pathname
    const route = `${init.method ?? 'GET'} ${path}`
    calls.push(route)
    let status = 200
    let body: unknown
    switch (route) {
      case 'POST /api/v1/session/open': body = { job: null }; break
      case 'POST /api/v1/session/heartbeat':
      case 'POST /api/v1/session/close': body = { ok: true }; break
      case 'GET /api/v1/devices':
        body = [{ device_id: 'device-1', filesystem: 'exfat', label: 'SCIENCE', size_bytes: 1000000000 }]
        break
      case 'GET /api/v1/devices/availability':
        body = { unsupported_removable_media_detected: false }; break
      case 'GET /api/v1/runtime':
        body = { mode: 'inference_enabled', status: 'ready', source: 'platform', gpu_lock_available: true }
        break
      case 'GET /api/v1/devices/device-1/tree': body = []; break
      case 'POST /api/v1/devices/device-1/preflight':
        if (code === 0) throw new TypeError(detail)
        status = documentClosed ? 200 : code
        body = documentClosed ? {
          preflight_id: 'accepted', status: 'completed', stage: 'completed',
          processed: 1, total: 1, percentage: 100, eta_seconds: 0, error: null,
          summary: { file_count: 1, source_bytes: 1, required_free_bytes: 1,
            local_free_bytes: 100, has_enough_space: true },
        } : { detail }
        break
      default: throw new Error(`Unexpected request: ${route}`)
    }
    return { ok: status < 400, status, json: async () => body } as Response
  }))
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Run preflight' }))
  await screen.findByRole('alert')
  expect(calls.filter(route => route === 'POST /api/v1/devices/device-1/preflight')).toHaveLength(1)
  expect(calls).not.toContain('POST /api/v1/jobs')
  expect(screen.queryByRole('button', { name: 'Confirm and start' })).toBeNull()
  if (!cleanBusy) {
    expect(screen.getByRole('alert').textContent).toBe(detail)
    expect(screen.getByRole('button', { name: 'Cancel preflight' })).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Run preflight' }) as HTMLButtonElement).disabled).toBe(true)
    return
  }
  await waitFor(() => expect(screen.queryByRole('heading', { name: 'Scanning files…' })).toBeNull())
  expect(screen.queryByRole('button', { name: 'Cancel preflight' })).toBeNull()
  expect(screen.getByRole('alert').textContent).toContain('Save and close documents on this USB, then retry Preflight.')
  documentClosed = true
  // Closing the document alone does not initiate another request.
  expect(calls.filter(route => route === 'POST /api/v1/devices/device-1/preflight')).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Run preflight' }))
  await screen.findByRole('button', { name: 'Confirm and start' })
  expect(calls.filter(route => route === 'POST /api/v1/devices/device-1/preflight')).toHaveLength(2)
  expect(calls).not.toContain('POST /api/v1/jobs')
})
