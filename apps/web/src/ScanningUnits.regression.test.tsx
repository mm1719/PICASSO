import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import App from './App'

afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear() })

test.each([
  ['SCANNING', 'images', 0, null],
  ['SCANNING', 'bytes', 1024, '1024.0 bytes/s'],
  ['INFERENCING', 'images', 0, '2.0 images/s'],
] as const)('truthful speed during %s with %s progress and %i bytes', async (state, unit, bytes, expected) => {
  const active = {
    job_id: 'job-unit-probe', state, stage: state, failure: null,
    counts: { discovered: 0, predicted: 0, low_confidence: 0, rejected: 0 },
    progress: {
      total: 0, processed: 0, percentage: 0, images_per_second: state === 'INFERENCING' ? 2 : 0,
      progress_unit: unit, total_bytes: bytes, processed_bytes: 0,
      throughput_bytes_per_second: bytes, eta_seconds: null, stage_eta_seconds: null, job_eta_seconds: null,
    },
  }
  localStorage.setItem('plankton-active-job', active.job_id)
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const route = `${init.method ?? 'GET'} ${new URL(String(input), 'http://localhost').pathname}`
    let body: unknown
    switch (route) {
      case 'POST /api/v1/session/open': body = { job: active }; break
      case 'POST /api/v1/session/heartbeat':
      case 'POST /api/v1/session/close': body = { ok: true }; break
      case 'GET /api/v1/jobs/job-unit-probe': body = active; break
      case 'GET /api/v1/devices': body = []; break
      case 'GET /api/v1/devices/availability': body = { unsupported_removable_media_detected: false }; break
      case 'GET /api/v1/runtime':
        body = { mode: 'inference_enabled', status: 'ready', source: 'platform', gpu_lock_available: true }; break
      default: throw new Error(`Unexpected request: ${route}`)
    }
    return { ok: true, status: 200, json: async () => body } as Response
  }))
  render(<App />)
  await screen.findAllByText(state)
  if (expected) expect(screen.getByText(expected)).toBeTruthy()
  // FR-06: scanning must never be presented as image inference throughput,
  // including the real initial heartbeat before trustworthy byte totals exist.
  if (state === 'SCANNING') expect(screen.queryByText(/images\/s$/)).toBeNull()
})
