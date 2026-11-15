import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import App from './App'

type ApiResult = { body: unknown; status?: number }
type ApiHandler = (init: RequestInit) => ApiResult

const routes = new Map<string, ApiHandler>()
let fetchMock: ReturnType<typeof vi.fn>

function respond(method: string, path: string, body: unknown, status = 200) {
  routes.set(`${method} ${path}`, () => ({ body, status }))
}

function respondWith(method: string, path: string, handler: ApiHandler) {
  routes.set(`${method} ${path}`, handler)
}

function baseRoutes() {
  respond('POST', '/api/v1/session/open', { job: null })
  respond('POST', '/api/v1/session/heartbeat', { ok: true })
  respond('POST', '/api/v1/session/close', { ok: true })
  respond('GET', '/api/v1/devices', [
    { device_id: 'device-1', filesystem: 'ext4', label: 'SCIENCE', size_bytes: 2 * 1024 ** 3 },
    { device_id: 'device-2', filesystem: 'ntfs', label: null, size_bytes: 512 },
  ])
  respond('GET', '/api/v1/devices/availability', {
    unsupported_removable_media_detected: false,
  })
  respond('GET', '/api/v1/runtime', {
    mode: 'inference_enabled',
    status: 'ready',
    source: 'platform',
    gpu_lock_available: true,
  })
}

function job(
  state: string,
  failure: object | null = null,
  stageEtaSeconds: number | null = null,
  jobEtaSeconds: number | null = null,
) {
  return {
    job_id: 'job-12345678',
    state,
    stage: state,
    started_at: '2026-08-04T00:00:00Z',
    completed_at: state === 'COMPLETED' ? '2026-08-04T00:01:00Z' : null,
    counts: { discovered: 2, predicted: 1, low_confidence: 1, rejected: 1 },
    failure,
    progress: {
      total: 2,
      processed: state === 'CREATED' ? 0 : 2,
      percentage: state === 'CREATED' ? 0 : 100,
      images_per_second: 2.5,
      eta_seconds: null,
      stage_eta_seconds: stageEtaSeconds,
      job_eta_seconds: jobEtaSeconds,
    },
  }
}

beforeEach(() => {
  routes.clear()
  respond('POST', '/api/v1/session/open', { job: null })
  respond('POST', '/api/v1/session/heartbeat', { ok: true })
  respond('POST', '/api/v1/session/close', { ok: true })
  localStorage.clear()
  fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.toString(), 'http://localhost')
    const method = init.method ?? 'GET'
    const handler = routes.get(`${method} ${url.pathname}${url.search}`)
      ?? routes.get(`${method} ${url.pathname}`)
    if (!handler) throw new Error(`Unhandled request: ${method} ${url.pathname}${url.search}`)
    const result = handler(init)
    return {
      ok: (result.status ?? 200) < 400,
      status: result.status ?? 200,
      json: async () => result.body,
    } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test.each(['SCANNING', 'IMPORTING', 'INFERENCING', 'EXPORTING'])(
  'same-mode reopen restores %s from the server with empty browser storage', async (state) => {
    baseRoutes()
    respond('POST', '/api/v1/session/open', { job: job(state) })
    respond('GET', '/api/v1/jobs/job-12345678', job(state))
    render(<App />)
    await waitFor(() => expect(screen.getAllByText(state)).toHaveLength(2))
    expect(localStorage.length).toBe(0)
    await waitFor(() => expect(fetchMock.mock.calls.some(([path]) => path === '/api/v1/jobs/job-12345678'))
      .toBe(true), { timeout: 2000 })
  },
)

test('failed reopen cleanup blocks source actions and Refresh retries the same session', async () => {
  baseRoutes()
  let attempts = 0
  const ids: string[] = []
  respondWith('POST', '/api/v1/session/open', (init) => {
    ids.push(JSON.parse(String(init.body)).session_id)
    attempts += 1
    return attempts === 1 ? { body: { detail: 'cleanup pending' }, status: 503 } : { body: { job: null } }
  })
  render(<App />)
  await screen.findByText(/Could not restore or release/)
  const source = await screen.findByRole('button', { name: /SCIENCE/ }) as HTMLButtonElement
  expect(source.disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await waitFor(() => expect(source.disabled).toBe(false))
  expect(ids).toHaveLength(2)
  expect(ids[0]).toBe(ids[1])
})

test('normal window close sends the session identity even with a pending preflight', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [])
  respond('POST', '/api/v1/devices/device-1/preflight', {
    preflight_id: 'retained', status: 'running', stage: 'discovering', processed: 0,
    total: null, percentage: null, eta_seconds: null, summary: null, error: null,
  })
  const mounted = render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Run preflight' }))
  await waitFor(() => expect(fetchMock.mock.calls.some(([path]) => path.endsWith('/preflight'))).toBe(true))
  const opening = fetchMock.mock.calls.find(([path]) => path === '/api/v1/session/open')
  mounted.unmount()
  const closing = fetchMock.mock.calls.find(([path]) => path === '/api/v1/session/close')
  expect(closing?.[1]?.body).toBe(opening?.[1]?.body)
  expect(closing?.[1]?.keepalive).toBe(true)
})

test('lost successful job POST response recovers the durable job without another POST', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [])
  respond('POST', '/api/v1/devices/device-1/preflight', {
    preflight_id: 'retained', status: 'completed', stage: 'completed', processed: 1,
    total: 1, percentage: 100, eta_seconds: 0, error: null,
    summary: { file_count: 1, source_bytes: 1, required_free_bytes: 2, local_free_bytes: 3, has_enough_space: true },
  })
  respondWith('POST', '/api/v1/jobs', () => {
    respond('POST', '/api/v1/session/open', { job: job('SCANNING') })
    throw new Error('response lost after server commit')
  })
  respond('GET', '/api/v1/jobs/job-12345678', job('SCANNING'))
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Run preflight' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm and start' }))
  await waitFor(() => expect(screen.getAllByText('SCANNING')).toHaveLength(2))
  expect(screen.getByText('Speed').nextElementSibling?.textContent).toBe('Calculating…')
  expect(fetchMock.mock.calls.filter(([path, init]) => path === '/api/v1/jobs' && init?.method === 'POST'))
    .toHaveLength(1)
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false)
})

test('window heartbeat uses the approved five-second interval and stops after pagehide', async () => {
  baseRoutes()
  vi.useFakeTimers()
  try {
    await act(async () => { render(<App />) })
    const heartbeats = () => fetchMock.mock.calls.filter(([path]) => path === '/api/v1/session/heartbeat')
    await act(async () => { await vi.advanceTimersByTimeAsync(4999) })
    expect(heartbeats()).toHaveLength(0)
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(heartbeats()).toHaveLength(1)
    fireEvent(window, new Event('pagehide'))
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(heartbeats()).toHaveLength(1)
  } finally {
    cleanup()
    vi.useRealTimers()
  }
})

test('navigates folders and blocks a job when preflight storage is insufficient', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [
    { name: 'science', relative_path: 'science', kind: 'directory', size_bytes: null },
  ])
  respond('GET', '/api/v1/devices/device-1/tree?path=science', [
    { name: 'image.png', relative_path: 'science/image.png', kind: 'file', size_bytes: 128 },
  ])
  let preflightRequest: unknown
  respondWith('POST', '/api/v1/devices/device-1/preflight', (init) => {
    preflightRequest = JSON.parse(String(init.body))
    return {
      body: {
        file_count: 2,
        source_bytes: 2048,
        required_free_bytes: 4096,
        local_free_bytes: 1024,
        has_enough_space: false,
      },
    }
  })

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'science' }))
  await screen.findByText('image.png')
  fireEvent.change(screen.getByPlaceholderText('science/metadata.csv'), {
    target: { value: 'science/metadata.csv' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Run preflight' }))

  await screen.findByText('Not enough local storage.')
  expect(preflightRequest).toEqual({
    session_id: expect.any(String),
    source_relative_path: 'science',
    metadata_relative_path: 'science/metadata.csv',
  })
  expect((screen.getByRole('button', { name: 'Confirm and start' }) as HTMLButtonElement).disabled)
    .toBe(true)

  fireEvent.click(screen.getByRole('button', { name: 'Open parent folder' }))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    '/api/v1/devices/device-1/tree?path=.',
    expect.anything(),
  ))
})

test('does not offer or submit automatic eject for an ordinary job', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [
    { name: 'science', relative_path: 'science', kind: 'directory', size_bytes: null },
  ])
  respond('GET', '/api/v1/devices/device-1/tree?path=science', [
    { name: 'image.png', relative_path: 'science/image.png', kind: 'file', size_bytes: 128 },
  ])
  respond('POST', '/api/v1/devices/device-1/preflight', {
    file_count: 1,
    source_bytes: 2048,
    required_free_bytes: 4096,
    local_free_bytes: 8192,
    has_enough_space: true,
  })
  let jobRequest: unknown
  respondWith('POST', '/api/v1/jobs', (init) => {
    jobRequest = JSON.parse(String(init.body))
    return { body: job('COMPLETED') }
  })
  respond('GET', '/api/v1/jobs/job-12345678/summary', {
    score_type: 'uncalibrated_softmax',
    counts: { discovered: 2, predicted: 1, low_confidence: 1, rejected: 1 },
    archive_path: '/jobs/job-12345678/archive/source_images.zip',
    usb_ejected: false,
  })

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'science' }))
  await screen.findByText('image.png')
  fireEvent.click(screen.getByRole('button', { name: 'Run preflight' }))

  await screen.findByText('Storage check passed.')
  expect(screen.queryByRole('checkbox', { name: 'Safely eject USB after export' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and start' }))

  await screen.findByText('COMPLETED', { selector: '.status' })
  expect(jobRequest).not.toHaveProperty('safe_eject')
  expect(await screen.findByText(/USB is available in Ubuntu Files/)).toBeTruthy()
})

test('refresh clears the selected source after native desktop eject', async () => {
  let connected = true
  baseRoutes()
  respondWith('GET', '/api/v1/devices', () => ({
    body: connected
      ? [{ device_id: 'device-1', filesystem: 'ext4', label: 'SCIENCE', size_bytes: 2 * 1024 ** 3 }]
      : [],
  }))
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [])
  respond('POST', '/api/v1/devices/device-1/preflight', {
    file_count: 1,
    source_bytes: 128,
    required_free_bytes: 256,
    local_free_bytes: 1024,
    has_enough_space: true,
  })
  respond('POST', '/api/v1/jobs', job('COMPLETED'))
  respond('GET', '/api/v1/jobs/job-12345678/summary', {
    score_type: 'uncalibrated_softmax',
    counts: { discovered: 2, predicted: 1, low_confidence: 1, rejected: 1 },
    archive_path: 'jobs/job-12345678/archive/source_images.zip',
    usb_ejected: false,
  })

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  await screen.findByText('Selected folder:')
  fireEvent.click(screen.getByRole('button', { name: 'Run preflight' }))
  await screen.findByText('Storage check passed.')
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and start' }))
  await screen.findByText('COMPLETED', { selector: '.status' })

  connected = false
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByText('No supported USB device detected.')
  expect(screen.queryByText('Selected folder:')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Open selected folder in Files' })).toBeNull()
})

test('silently clears a restored job pointer absent from the current state root', async () => {
  baseRoutes()
  localStorage.setItem('plankton-active-job', 'job-from-previous-candidate')
  respond('GET', '/api/v1/jobs/job-from-previous-candidate', { detail: 'Not found' }, 404)

  render(<App />)

  await screen.findByText('Choose a source folder to review its import requirements.')
  await waitFor(() => expect(localStorage.getItem('plankton-active-job')).toBeNull())
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByText('job not found')).toBeNull()
})

test('shows observable async preflight progress, ETA, and cancellation', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [
    { name: 'image.png', relative_path: 'image.png', kind: 'file', size_bytes: 128 },
  ])
  respond('POST', '/api/v1/devices/device-1/preflight', {
    preflight_id: 'preflight-1',
    status: 'running',
    stage: 'discovering',
    processed: 0,
    total: null,
    percentage: null,
    eta_seconds: null,
    summary: null,
    error: null,
  }, 202)
  respond('GET', '/api/v1/preflights/preflight-1', {
    preflight_id: 'preflight-1',
    status: 'running',
    stage: 'measuring',
    processed: 500,
    total: 1000,
    percentage: 50,
    eta_seconds: 120,
    summary: null,
    error: null,
  })
  respond('DELETE', '/api/v1/preflights/preflight-1', {
    preflight_id: 'preflight-1',
    status: 'cancelled',
    stage: 'cancelled',
    processed: 500,
    total: 1000,
    percentage: 50,
    eta_seconds: null,
    summary: null,
    error: null,
  })

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Run preflight' }))

  await screen.findByText('Scanning files…')
  await screen.findByText('Measuring source files…')
  expect(screen.getByText('50.0%')).toBeTruthy()
  expect(screen.getByText('Estimated completion: About 2 min')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel preflight' }))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    '/api/v1/preflights/preflight-1',
    expect.objectContaining({ method: 'DELETE' }),
  ))
})

test('cancels a running preflight before navigating to another folder', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [
    { name: 'science', relative_path: 'science', kind: 'directory', size_bytes: null },
  ])
  respond('GET', '/api/v1/devices/device-1/tree?path=science', [])
  respond('POST', '/api/v1/devices/device-1/preflight', {
    preflight_id: 'preflight-1',
    status: 'running',
    stage: 'discovering',
    processed: 500,
    total: null,
    percentage: null,
    eta_seconds: null,
    summary: null,
    error: null,
  }, 202)
  respond('GET', '/api/v1/preflights/preflight-1', {
    preflight_id: 'preflight-1',
    status: 'running',
    stage: 'discovering',
    processed: 500,
    total: null,
    percentage: null,
    eta_seconds: null,
    summary: null,
    error: null,
  })
  respond('DELETE', '/api/v1/preflights/preflight-1', {
    preflight_id: 'preflight-1',
    status: 'cancelled',
    stage: 'cancelled',
    processed: 500,
    total: null,
    percentage: null,
    eta_seconds: null,
    summary: null,
    error: null,
  })

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Run preflight' }))
  await screen.findByText('Scanning files…')
  fireEvent.click(screen.getByRole('button', { name: 'science' }))

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    '/api/v1/preflights/preflight-1',
    expect.objectContaining({ method: 'DELETE' }),
  ))
})

test('cancels a running preflight when metadata selection changes', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [])
  respond('POST', '/api/v1/devices/device-1/preflight', {
    preflight_id: 'preflight-1',
    status: 'running',
    stage: 'discovering',
    processed: 500,
    total: null,
    percentage: null,
    eta_seconds: null,
    summary: null,
    error: null,
  }, 202)
  respond('GET', '/api/v1/preflights/preflight-1', {
    preflight_id: 'preflight-1',
    status: 'running',
    stage: 'discovering',
    processed: 500,
    total: null,
    percentage: null,
    eta_seconds: null,
    summary: null,
    error: null,
  })
  respond('DELETE', '/api/v1/preflights/preflight-1', {
    preflight_id: 'preflight-1',
    status: 'cancelled',
    stage: 'cancelled',
    processed: 500,
    total: null,
    percentage: null,
    eta_seconds: null,
    summary: null,
    error: null,
  })

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Run preflight' }))
  await screen.findByText('Scanning files…')
  fireEvent.change(screen.getByLabelText('Optional metadata CSV path'), {
    target: { value: 'science/metadata.csv' },
  })
  fireEvent.change(screen.getByLabelText('Optional metadata CSV path'), {
    target: { value: 'science/revised.csv' },
  })

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    '/api/v1/preflights/preflight-1',
    expect.objectContaining({ method: 'DELETE' }),
  ))
  expect(fetchMock.mock.calls.filter(([url, init]) => (
    url === '/api/v1/preflights/preflight-1' && init?.method === 'DELETE'
  ))).toHaveLength(1)
})

test('refresh cancels a running preflight and returns to editable source selection', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [])
  respond('POST', '/api/v1/devices/device-1/preflight', {
    preflight_id: 'preflight-1',
    status: 'running',
    stage: 'discovering',
    processed: 1,
    total: null,
    percentage: null,
    eta_seconds: null,
    summary: null,
    error: null,
  }, 202)
  respond('GET', '/api/v1/preflights/preflight-1', {
    preflight_id: 'preflight-1',
    status: 'running',
    stage: 'discovering',
    processed: 1,
    total: null,
    percentage: null,
    eta_seconds: null,
    summary: null,
    error: null,
  })
  respond('DELETE', '/api/v1/preflights/preflight-1', {
    preflight_id: 'preflight-1',
    status: 'cancelled',
    stage: 'cancelled',
    processed: 1,
    total: null,
    percentage: null,
    eta_seconds: null,
    summary: null,
    error: null,
  })

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Run preflight' }))
  await screen.findByText('Scanning files…')
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    '/api/v1/preflights/preflight-1',
    expect.objectContaining({ method: 'DELETE' }),
  ))
  expect(fetchMock.mock.calls.filter(([url, init]) => (
    url === '/api/v1/preflights/preflight-1' && init?.method === 'DELETE'
  ))).toHaveLength(1)
  expect(screen.queryByText('Scanning files…')).toBeNull()
  expect(screen.getByRole('button', { name: 'Run preflight' })).toBeTruthy()
})

test('refresh releases a completed zero-image preflight without creating a job', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [])
  respond('POST', '/api/v1/devices/device-1/preflight', {
    preflight_id: 'preflight-1',
    status: 'completed',
    stage: 'completed',
    processed: 0,
    total: 0,
    percentage: null,
    eta_seconds: 0,
    summary: {
      file_count: 0,
      source_bytes: 0,
      required_free_bytes: 256,
      local_free_bytes: 1024,
      has_enough_space: true,
    },
    error: null,
  })
  respond('DELETE', '/api/v1/preflights/preflight-1', {
    preflight_id: 'preflight-1',
    status: 'cancelled',
    stage: 'cancelled',
    processed: 0,
    total: 0,
    percentage: null,
    eta_seconds: null,
    summary: null,
    error: null,
  })

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Run preflight' }))
  await screen.findByText('Storage check passed.')
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

  await waitFor(() => expect(screen.queryByText('Storage check passed.')).toBeNull())
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    '/api/v1/preflights/preflight-1',
    expect.objectContaining({ method: 'DELETE' }),
  ))
  expect(screen.queryByRole('button', { name: 'Confirm and start' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Run preflight' })).toBeTruthy()
})

test('refresh resets a completed preflight already removed after USB service restart', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [])
  respond('POST', '/api/v1/devices/device-1/preflight', {
    preflight_id: 'preflight-stale',
    status: 'completed',
    stage: 'completed',
    processed: 1000,
    total: 1000,
    percentage: 100,
    eta_seconds: 0,
    summary: {
      file_count: 1000,
      source_bytes: 2048,
      required_free_bytes: 4096,
      local_free_bytes: 8192,
      has_enough_space: true,
    },
    error: null,
  })
  respond('DELETE', '/api/v1/preflights/preflight-stale', {
    detail: 'preflight not found',
  }, 404)

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Run preflight' }))
  await screen.findByText('Storage check passed.')
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

  await waitFor(() => expect(screen.queryByText('Storage check passed.')).toBeNull())
  expect(screen.queryByRole('button', { name: 'Confirm and start' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Run preflight' })).toBeTruthy()
  expect(fetchMock.mock.calls.filter(([url, init]) => (
    url === '/api/v1/preflights/preflight-stale' && init?.method === 'DELETE'
  ))).toHaveLength(1)
})

test('confirm resets a preflight when the restarted API reports it unavailable', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [])
  respond('POST', '/api/v1/devices/device-1/preflight', {
    preflight_id: 'preflight-stale',
    status: 'completed',
    stage: 'completed',
    processed: 1,
    total: 1,
    percentage: 100,
    eta_seconds: 0,
    summary: {
      file_count: 1,
      source_bytes: 128,
      required_free_bytes: 256,
      local_free_bytes: 1024,
      has_enough_space: true,
    },
    error: null,
  })
  respond('POST', '/api/v1/jobs', { detail: 'preflight is unavailable' }, 409)

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Run preflight' }))
  await screen.findByText('Storage check passed.')
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and start' }))

  await waitFor(() => expect(screen.queryByText('Storage check passed.')).toBeNull())
  expect(screen.queryByRole('button', { name: 'Confirm and start' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Run preflight' })).toBeTruthy()
  expect(await screen.findByText(/Preflight expired after the USB service restarted/)).toBeTruthy()
})

test('retains one completed preflight and blocks a duplicate claim until release', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [])
  let preflightPosts = 0
  respondWith('POST', '/api/v1/devices/device-1/preflight', () => {
    preflightPosts += 1
    return {
      body: {
        preflight_id: `preflight-${preflightPosts}`,
        status: 'completed',
        stage: 'completed',
        processed: 0,
        total: 0,
        percentage: null,
        eta_seconds: 0,
        summary: {
          file_count: 0,
          source_bytes: 0,
          required_free_bytes: 256,
          local_free_bytes: 1024,
          has_enough_space: true,
        },
        error: null,
      },
    }
  })

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  await screen.findByText('Selected folder:')
  fireEvent.click(screen.getByRole('button', { name: 'Run preflight' }))
  await screen.findByText('Storage check passed.')

  const runPreflight = screen.getByRole('button', { name: 'Run preflight' }) as HTMLButtonElement
  expect(runPreflight.disabled).toBe(true)
  fireEvent.click(runPreflight)
  expect(preflightPosts).toBe(1)
})

test('blocks source claims for a terminal job until Prepare another job', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [])
  let preflightPosts = 0
  respondWith('POST', '/api/v1/devices/device-1/preflight', () => {
    preflightPosts += 1
    return {
      body: {
        preflight_id: `preflight-${preflightPosts}`,
        status: 'completed',
        stage: 'completed',
        processed: 1,
        total: 1,
        percentage: 100,
        eta_seconds: 0,
        summary: {
          file_count: 1,
          source_bytes: 128,
          required_free_bytes: 256,
          local_free_bytes: 1024,
          has_enough_space: true,
        },
        error: null,
      },
    }
  })
  respond('POST', '/api/v1/jobs', job('SECURITY_BLOCKED', {
    code: 'malware_detected',
    message: 'Import stopped by the USB security scan.',
    relative_paths: ['eicar.com.txt'],
  }))

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  await screen.findByText('Selected folder:')
  fireEvent.click(screen.getByRole('button', { name: 'Run preflight' }))
  await screen.findByText('Storage check passed.')
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and start' }))
  await screen.findByText('SECURITY BLOCKED', { selector: '.status' })

  const runPreflight = screen.getByRole('button', { name: 'Run preflight' }) as HTMLButtonElement
  expect(runPreflight.disabled).toBe(true)
  expect(preflightPosts).toBe(1)

  fireEvent.click(screen.getByRole('button', { name: 'Prepare another job' }))
  expect((screen.getByRole('button', { name: 'Run preflight' }) as HTMLButtonElement).disabled)
    .toBe(false)
})

test('keeps a completed preflight blocked and retries the same release after failure', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [])
  let preflightPosts = 0
  let releaseAttempts = 0
  let releaseFails = true
  respondWith('POST', '/api/v1/devices/device-1/preflight', () => {
    preflightPosts += 1
    return {
      body: {
        preflight_id: 'preflight-retained',
        status: 'completed',
        stage: 'completed',
        processed: 0,
        total: 0,
        percentage: null,
        eta_seconds: 0,
        summary: {
          file_count: 0,
          source_bytes: 0,
          required_free_bytes: 256,
          local_free_bytes: 1024,
          has_enough_space: true,
        },
        error: null,
      },
    }
  })
  respondWith('DELETE', '/api/v1/preflights/preflight-retained', () => {
    releaseAttempts += 1
    return releaseFails
      ? { body: { detail: 'USB release is incomplete.' }, status: 503 }
      : {
          body: {
            preflight_id: 'preflight-retained',
            status: 'cancelled',
            stage: 'cancelled',
            processed: 0,
            total: 0,
            percentage: null,
            eta_seconds: null,
            summary: null,
            error: null,
          },
        }
  })

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  await screen.findByText('Selected folder:')
  fireEvent.click(screen.getByRole('button', { name: 'Run preflight' }))
  await screen.findByText('Storage check passed.')
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

  await screen.findByText('Could not release preflight. Try again.')
  expect(screen.getByText('Storage check passed.')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Run preflight' }) as HTMLButtonElement).disabled)
    .toBe(true)
  expect(preflightPosts).toBe(1)
  expect(releaseAttempts).toBe(1)

  releaseFails = false
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await waitFor(() => expect(screen.queryByText('Storage check passed.')).toBeNull())
  expect(releaseAttempts).toBe(2)
  expect(preflightPosts).toBe(1)
  expect((screen.getByRole('button', { name: 'Run preflight' }) as HTMLButtonElement).disabled)
    .toBe(false)
})

test('refresh keeps an existing non-terminal job attached and observable', async () => {
  baseRoutes()
  respond('POST', '/api/v1/session/open', { job: job('INFERENCING') })
  localStorage.setItem('plankton-active-job', 'job-12345678')
  respond('GET', '/api/v1/jobs/job-12345678', job('INFERENCING'))

  render(<App />)
  await waitFor(() => expect(screen.getAllByText('INFERENCING')).toHaveLength(2))
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

  await waitFor(() => expect(screen.getAllByText('INFERENCING')).toHaveLength(2))
  expect(localStorage.getItem('plankton-active-job')).toBeNull()
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false)
})

test('refresh removes an absent selected USB and shows it again after reconnection', async () => {
  baseRoutes()
  respond('GET', '/api/v1/devices/device-1/tree?path=.', [])

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /SCIENCE/ }))
  await screen.findByText('Selected folder:')

  respond('GET', '/api/v1/devices', [])
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByText('No supported USB device detected.')
  expect(screen.queryByText('Selected folder:')).toBeNull()

  respond('GET', '/api/v1/devices', [
    { device_id: 'device-1', filesystem: 'ext4', label: 'SCIENCE', size_bytes: 2 * 1024 ** 3 },
  ])
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  expect(await screen.findByRole('button', { name: /SCIENCE/ })).toBeTruthy()
})

test('brands the platform as PICASSO with its full name', () => {
  baseRoutes()
  render(<App />)

  expect(screen.getByRole('heading', { name: 'PICASSO' })).toBeTruthy()
  expect(screen.getByText('Plankton Image Classification & Analysis System for Scientific Observation')).toBeTruthy()
})

test('displays read-only workstation status from the supported authority', async () => {
  baseRoutes()
  respond('GET', '/api/v1/runtime', {
    mode: 'training',
    status: 'training',
    source: 'training_entrypoint',
    gpu_lock_available: false,
  })

  render(<App />)

  await screen.findByText('Training in progress')
  expect(screen.getByText('Controlled by the training entrypoint.')).toBeTruthy()
  await screen.findByText('GPU lock in use')
  expect(screen.queryByRole('combobox')).toBeNull()
  expect(fetchMock).not.toHaveBeenCalledWith(
    '/api/v1/runtime/mode',
    expect.objectContaining({ method: 'PUT' }),
  )
})

test('shows an API detail and can recover by refreshing devices', async () => {
  respond('GET', '/api/v1/devices', { detail: 'USB agent is unavailable.' }, 503)
  respond('GET', '/api/v1/devices/availability', {
    unsupported_removable_media_detected: false,
  })
  respond('GET', '/api/v1/runtime', {
    mode: 'inference_enabled',
    status: 'ready',
    source: 'platform',
    gpu_lock_available: true,
  })

  render(<App />)
  await screen.findByText('USB agent is unavailable.')

  respond('GET', '/api/v1/devices', [
    { device_id: 'device-1', filesystem: 'ext4', label: 'SCIENCE', size_bytes: 512 },
  ])
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

  await screen.findByRole('button', { name: /SCIENCE/ })
  expect(screen.queryByRole('alert')).toBeNull()
})

test('explains an unsupported USB filesystem without identifying the device', async () => {
  respond('GET', '/api/v1/devices', [])
  respond('GET', '/api/v1/devices/availability', {
    unsupported_removable_media_detected: true,
  })
  respond('GET', '/api/v1/runtime', {
    mode: 'inference_enabled',
    status: 'ready',
    source: 'platform',
    gpu_lock_available: true,
  })

  render(<App />)

  await screen.findByText(/filesystem is unsupported/)
  expect(screen.getByText(/Supported formats: FAT16, FAT32, exFAT, NTFS, and ext4/)).toBeTruthy()
  expect(screen.queryByText(/\/dev\//)).toBeNull()
})

test('explains a missing filesystem UUID with an actionable message', async () => {
  respond('GET', '/api/v1/devices', [])
  respond('GET', '/api/v1/devices/availability', {
    unsupported_removable_media_detected: false,
    missing_filesystem_uuid_detected: true,
  })
  respond('GET', '/api/v1/runtime', {
    mode: 'inference_enabled',
    status: 'ready',
    source: 'platform',
    gpu_lock_available: true,
  })

  render(<App />)

  await screen.findByText(/filesystem UUID is missing/)
  expect(screen.getByText(/Reconnect a device with a filesystem UUID/)).toBeTruthy()
})

test.each(['COMPLETED', 'FAILED', 'SECURITY_BLOCKED', 'CANCELLED'])(
  'clears restored terminal job %s from the UI session without deleting persisted results',
  async (state) => {
    baseRoutes()
    localStorage.setItem('plankton-active-job', 'job-12345678')
    respond('GET', '/api/v1/jobs/job-12345678', job(state))

    render(<App />)

    await waitFor(() => expect(localStorage.getItem('plankton-active-job')).toBeNull())
    expect(screen.queryByText(state.replaceAll('_', ' '))).toBeNull()
    expect(await screen.findByText('Choose a source folder to review its import requirements.'))
      .toBeTruthy()
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false)
  },
)

test('keeps scan rate unknown through heartbeats, then renders each polled stage in its own units', async () => {
  baseRoutes()
  const initial = job('CREATED')
  let active = {
    ...initial,
    progress: {
      ...initial.progress, total: 0, processed: 0, percentage: 0,
      images_per_second: 0, progress_unit: 'images' as string | undefined,
      total_bytes: 0, processed_bytes: 0, throughput_bytes_per_second: 0,
      updated_at: '2026-10-28T00:00:00Z',
    },
  }
  respond('POST', '/api/v1/session/open', { job: active })
  respondWith('GET', '/api/v1/jobs/job-12345678', () => ({ body: active }))
  const speed = () => screen.getByText('Speed').nextElementSibling?.textContent
  const eta = (label: string) => screen.getByText(label).nextElementSibling?.textContent
  vi.useFakeTimers()
  try {
    await act(async () => { render(<App />) })
    active = { ...active, state: 'SCANNING', stage: 'SCANNING' }
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(speed()).toBe('Calculating…')
    expect(screen.queryByText(/bytes\/s$/)).toBeNull()
    expect(eta('This stage')).toBe('Calculating…')
    expect(eta('Estimated completion')).toBe('Calculating…')

    // A heartbeat only advances its timestamp; it must not invent work, rate or ETA.
    const beforeHeartbeat = document.querySelector('.progress-copy')?.textContent
    active = { ...active, progress: { ...active.progress, updated_at: '2026-10-28T00:00:05Z' } }
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(document.querySelector('.progress-copy')?.textContent).toBe(beforeHeartbeat)
    expect(speed()).toBe('Calculating…')
    expect(eta('This stage')).toBe('Calculating…')
    expect(eta('Estimated completion')).toBe('Calculating…')
    expect(fetchMock.mock.calls.filter(([path]) => path === '/api/v1/session/heartbeat')).toHaveLength(1)

    active = { ...active, progress: { ...active.progress, total: 4, processed: 1,
      percentage: 25, progress_unit: 'bytes', total_bytes: 4096, processed_bytes: 1024,
      throughput_bytes_per_second: 512, stage_eta_seconds: 6, job_eta_seconds: 120 } }
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(speed()).toBe('512.0 bytes/s')
    expect(screen.getByText('1 files · 1,024 / 4,096 bytes')).toBeTruthy()
    expect(eta('Estimated completion')).toBe('About 2 min')

    active = { ...active, state: 'IMPORTING', stage: 'IMPORTING' }
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(speed()).toBe('512.0 bytes/s')
    expect(screen.getByText('1 images · 1,024 / 4,096 bytes')).toBeTruthy()

    for (const state of ['VALIDATING', 'INFERENCING']) {
      active = { ...active, state, stage: state, progress: { ...active.progress,
        progress_unit: 'images', images_per_second: 2.5 } }
      await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
      expect(speed()).toBe('2.5 images/s')
      expect(screen.getByText('1 / 4 images')).toBeTruthy()
    }

    // A restored scan may retain a legacy/missing unit and an old image rate.
    active = { ...active, state: 'SCANNING', stage: 'SCANNING', progress: { ...active.progress,
      progress_unit: undefined, stage_eta_seconds: null, job_eta_seconds: null } }
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(speed()).toBe('Calculating…')
    expect(screen.queryByText(/images\/s$/)).toBeNull()
    expect(eta('This stage')).toBe('Calculating…')
    expect(eta('Estimated completion')).toBe('Calculating…')
  } finally {
    cleanup()
    vi.useRealTimers()
  }
})

test('presents exact ETA fields as readable customer durations', async () => {
  baseRoutes()
  localStorage.setItem('plankton-active-job', 'job-12345678')
  const active = job('INFERENCING', null, 125, 4210)
  respond('POST', '/api/v1/session/open', { job: active })
  respond('GET', '/api/v1/jobs/job-12345678', active)

  render(<App />)

  await screen.findByText('About 3 min')
  expect(screen.getByText('About 1 hr 11 min')).toBeTruthy()
  expect(screen.getByText('This stage')).toBeTruthy()
  expect(screen.getByText('Estimated completion')).toBeTruthy()
  expect(screen.queryByText('stage_eta_seconds')).toBeNull()
  expect(screen.queryByText('125s')).toBeNull()
})

test.each([
  ['IMPORTING', 'Calculating…'],
  ['WAITING_FOR_GPU', 'Paused'],
])('renders readable ETA state for %s', async (state, expected) => {
  baseRoutes()
  respond('POST', '/api/v1/session/open', { job: job(state) })
  localStorage.setItem('plankton-active-job', 'job-12345678')
  respond('GET', '/api/v1/jobs/job-12345678', job(state))
  if (state === 'COMPLETED') {
    respond('GET', '/api/v1/jobs/job-12345678/summary', {
      score_type: 'uncalibrated_softmax',
      counts: { discovered: 2, predicted: 1, low_confidence: 1, rejected: 1 },
      archive_path: '/jobs/job-12345678/archive/source_images.zip',
    })
  }

  render(<App />)

  expect((await screen.findAllByText(expected)).length).toBe(2)
})

describe.each([
  ['WAITING_FOR_GPU', null, 'Waiting for training to finish or for the GPU lock.'],
  ['WAITING_FOR_USB', null, 'Reconnect the same USB device to export and finish the job.'],
])('restored job state %s', (state, failure, expectedMessage) => {
  test('shows the operator-safe recovery message', async () => {
    baseRoutes()
    respond('POST', '/api/v1/session/open', { job: job(state, failure) })
    localStorage.setItem('plankton-active-job', 'job-12345678')
    respond('GET', '/api/v1/jobs/job-12345678', job(state, failure))

    render(<App />)

    await screen.findByText(expectedMessage)
  })
})
