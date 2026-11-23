import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import App from './App'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test('busy Preflight explains manual Files closure on any desktop and never retries automatically', async () => {
  localStorage.clear()
  const calls: string[] = []
  let filesClosed = false
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const route = `${init.method ?? 'GET'} ${new URL(String(input), 'http://localhost').pathname}`
    calls.push(route)
    let status = 200
    let body: unknown
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
        status = filesClosed ? 200 : 503
        body = filesClosed ? {
          preflight_id: 'accepted', status: 'completed', stage: 'completed',
          processed: 1, total: 1, percentage: 100, eta_seconds: 0, error: null,
          summary: { file_count: 1, source_bytes: 1, required_free_bytes: 1,
            local_free_bytes: 100, has_enough_space: true },
        } : { detail: 'native mount is busy' }
        break
      default: throw new Error(`Unexpected request: ${route}`)
    }
    return { ok: status < 400, status, json: async () => body } as Response
  }))

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  const disclosure = await screen.findByText(/If this USB is busy, save your work and close Files windows/)
  expect(disclosure.textContent).toContain('then click Run preflight to retry.')
  expect(disclosure.textContent).toContain('This applies to local and remote desktops.')
  fireEvent.click(await screen.findByRole('button', { name: 'Run preflight' }))
  const alert = await screen.findByRole('alert')
  expect(alert.textContent).toContain('Save and close documents on this USB, then retry Preflight.')
  expect(alert.textContent).toContain('Also close Files windows showing this USB, whether you are using a local or remote desktop.')
  expect(alert.textContent).toContain('Click Run preflight when ready to retry.')
  expect(calls.filter(route => route === 'POST /api/v1/devices/device-1/preflight')).toHaveLength(1)
  expect(calls).not.toContain('POST /api/v1/jobs')
  expect(screen.queryByRole('button', { name: 'Confirm and start' })).toBeNull()
  expect((screen.getByRole('button', { name: 'Run preflight' }) as HTMLButtonElement).disabled).toBe(false)

  filesClosed = true
  vi.useFakeTimers()
  try {
    await act(async () => {
      fireEvent.focus(window)
      await vi.advanceTimersByTimeAsync(5_000)
    })
  } finally {
    vi.useRealTimers()
  }
  expect(calls.filter(route => route === 'POST /api/v1/devices/device-1/preflight')).toHaveLength(1)
  expect(calls).not.toContain('POST /api/v1/jobs')
  fireEvent.click(screen.getByRole('button', { name: 'Run preflight' }))
  await screen.findByRole('button', { name: 'Confirm and start' })
  expect(calls.filter(route => route === 'POST /api/v1/devices/device-1/preflight')).toHaveLength(2)
  expect(calls).not.toContain('POST /api/v1/jobs')
})
