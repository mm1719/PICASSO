import { expect, test, type Page, type Route } from '@playwright/test'

const counts = { discovered: 2, predicted: 2, low_confidence: 1, rejected: 0 }

function job(state: string, failure: object | null = null) {
  return {
    job_id: 'job-12345678',
    state,
    stage: state,
    started_at: '2026-08-04T00:00:00Z',
    completed_at: state === 'COMPLETED' ? '2026-08-04T00:01:00Z' : null,
    counts,
    failure,
    progress: {
      total: 2,
      processed: state === 'CREATED' ? 0 : 2,
      percentage: state === 'CREATED' ? 0 : 100,
      images_per_second: 2.5,
      eta_seconds: null,
      stage_eta_seconds: null,
      job_eta_seconds: null,
    },
  }
}

async function json(route: Route, body: object, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

async function mockSession(page: Page, active: ReturnType<typeof job> | null = null) {
  await page.route('**/api/v1/session/open', (route) => {
    const { session_id } = route.request().postDataJSON()
    expect(session_id).toEqual(expect.any(String))
    return json(route, { session_id, job: active })
  })
}

test.beforeEach(async ({ page }) => {
  await mockSession(page)
  for (const action of ['close', 'heartbeat']) {
    await page.route(`**/api/v1/session/${action}`, (route) => json(route, { ok: true }))
  }
})

async function mockCommon(page: Page) {
  await page.route('**/api/v1/devices', (route) =>
    json(route, [
      {
        device_id: 'device-1',
        filesystem: 'ext4',
        label: 'SCIENCE',
        size_bytes: 1024 ** 3,
      },
    ]),
  )
  await page.route('**/api/v1/devices/availability', (route) =>
    json(route, { unsupported_removable_media_detected: false }),
  )
  await page.route('**/api/v1/runtime', (route) =>
    json(route, {
      mode: 'inference_enabled',
      status: 'ready',
      source: 'platform',
      gpu_lock_available: true,
    }),
  )
}

test('completes the visible preflight and job workflow', async ({ page }) => {
  await mockCommon(page)
  await page.route('**/api/v1/devices/device-1/tree?*', (route) =>
    json(route, [
      { name: 'image.png', relative_path: 'image.png', kind: 'file', size_bytes: 128 },
    ]),
  )
  await page.route('**/api/v1/devices/device-1/preflight', (route) =>
    json(route, {
      file_count: 2,
      source_bytes: 2048,
      required_free_bytes: 4096,
      local_free_bytes: 8192,
      has_enough_space: true,
    }),
  )
  await page.route('**/api/v1/jobs', async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      session_id: expect.any(String),
      device_id: 'device-1',
      source_relative_path: '.',
      metadata_relative_path: null,
      preflight_id: null,
    })
    await json(route, job('CREATED'), 202)
  })
  await page.route('**/api/v1/jobs/job-12345678/summary', (route) =>
    json(route, {
      score_type: 'uncalibrated_softmax',
      counts,
      archive_path: '/var/lib/plankton-inference/jobs/job-12345678/archive/source_images.zip',
    }),
  )
  await page.route('**/api/v1/jobs/job-12345678', (route) =>
    json(route, job('COMPLETED')),
  )

  await page.goto('/')
  await page.getByRole('button', { name: /SCIENCE/ }).click()
  await page.getByRole('button', { name: 'Run preflight' }).click()
  await expect(page.getByText('Storage check passed.')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm and start' }).click()

  await expect(page.getByText('Export complete')).toBeVisible()
  await expect(page.getByText(/uncalibrated softmax model scores/)).toBeVisible()
  await expect(page.getByText(/2 predictions and 0 rejections exported/)).toBeVisible()
  await expect(page.locator('.job__state code')).toHaveText('job-1234')
  await page.getByRole('button', { name: 'Prepare another job' }).click()
  expect(await page.evaluate(() => localStorage.getItem('plankton-active-job'))).toBeNull()
  await expect(page.getByText('Choose a source folder to review its import requirements.')).toBeVisible()
})

test('requires a new session before a terminal security job can claim again', async ({ page }) => {
  await mockCommon(page)
  await page.route('**/api/v1/devices/device-1/tree?*', (route) => json(route, []))
  let preflightPosts = 0
  await page.route('**/api/v1/devices/device-1/preflight', (route) => {
    preflightPosts += 1
    return json(route, {
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
    })
  })
  await page.route('**/api/v1/jobs', (route) => json(route, job('SECURITY_BLOCKED', {
    code: 'malware_detected',
    message: 'Import stopped by the USB security scan.',
    relative_paths: ['eicar.com.txt'],
  }), 202))

  await page.goto('/')
  await page.getByRole('button', { name: /SCIENCE/ }).click()
  await page.getByRole('button', { name: 'Run preflight' }).click()
  await expect(page.getByText('Storage check passed.')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm and start' }).click()
  await expect(page.locator('.job__state .status')).toHaveText('SECURITY BLOCKED')

  await expect(page.getByRole('button', { name: 'Run preflight' })).toBeDisabled()
  expect(preflightPosts).toBe(1)

  await page.getByRole('button', { name: 'Prepare another job' }).click()
  await expect(page.getByRole('button', { name: 'Run preflight' })).toBeEnabled()
})

test('navigates a nested source and blocks start when storage is insufficient', async ({ page }) => {
  await mockCommon(page)
  await page.route('**/api/v1/devices/device-1/tree?*', (route) => {
    const path = new URL(route.request().url()).searchParams.get('path')
    return json(route, path === 'science'
      ? [{ name: 'image.png', relative_path: 'science/image.png', kind: 'file', size_bytes: 128 }]
      : [{ name: 'science', relative_path: 'science', kind: 'directory', size_bytes: null }])
  })
  await page.route('**/api/v1/devices/device-1/preflight', async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      session_id: expect.any(String),
      source_relative_path: 'science',
      metadata_relative_path: 'science/metadata.csv',
    })
    await json(route, {
      file_count: 2,
      source_bytes: 2048,
      required_free_bytes: 4096,
      local_free_bytes: 1024,
      has_enough_space: false,
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: /SCIENCE/ }).click()
  const nestedTree = page.waitForRequest((request) =>
    new URL(request.url()).searchParams.get('path') === 'science',
  )
  await page.locator('.entries').getByRole('button', { name: 'science', exact: true }).click()
  await nestedTree
  await expect(page.locator('.pathbar code')).toHaveText('/science')
  await expect(page.getByText('image.png')).toBeVisible()
  await page.getByPlaceholder('science/metadata.csv').fill('science/metadata.csv')
  await page.getByRole('button', { name: 'Run preflight' }).click()

  await expect(page.getByText('Not enough local storage.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Confirm and start' })).toBeDisabled()
})

test('reconciles USB removal on Refresh', async ({ page }) => {
  let connected = true
  await page.route('**/api/v1/devices', (route) => json(route, connected ? [{
    device_id: 'device-1',
    filesystem: 'ext4',
    label: 'SCIENCE',
    size_bytes: 1024 ** 3,
  }] : []))
  await page.route('**/api/v1/devices/availability', (route) =>
    json(route, { unsupported_removable_media_detected: false }),
  )
  await page.route('**/api/v1/runtime', (route) => json(route, {
    mode: 'inference_enabled',
    status: 'ready',
    source: 'platform',
    gpu_lock_available: true,
  }))
  await page.route('**/api/v1/devices/device-1/tree?*', (route) => {
    const path = new URL(route.request().url()).searchParams.get('path')
    return json(route, path === 'science' ? [] : [
      { name: 'science', relative_path: 'science', kind: 'directory', size_bytes: null },
    ])
  })
  await page.goto('/')
  await page.getByRole('button', { name: /SCIENCE/ }).click()
  await page.locator('.entries').getByRole('button', { name: 'science', exact: true }).click()

  connected = false
  await page.getByRole('button', { name: 'Refresh' }).click()
  await expect(page.getByText('No supported USB device detected.')).toBeVisible()
  await expect(page.getByText('Selected folder:')).toHaveCount(0)

  connected = true
  await page.getByRole('button', { name: 'Refresh' }).click()
  await expect(page.getByRole('button', { name: /SCIENCE/ })).toBeVisible()
})

test('Refresh cancels a running preflight without creating a job', async ({ page }) => {
  const createdJobs: string[] = []
  const cancellations: string[] = []
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/v1/jobs') {
      createdJobs.push(request.url())
    }
    if (request.method() === 'DELETE') cancellations.push(new URL(request.url()).pathname)
  })
  await mockCommon(page)
  await page.route('**/api/v1/devices/device-1/tree?*', (route) => json(route, []))
  await page.route('**/api/v1/devices/device-1/preflight', (route) => json(route, {
    preflight_id: 'preflight-1',
    status: 'running',
    stage: 'discovering',
    processed: 0,
    total: null,
    percentage: null,
    eta_seconds: null,
    summary: null,
    error: null,
  }, 202))
  await page.route('**/api/v1/preflights/preflight-1', (route) =>
    route.request().method() === 'DELETE'
      ? json(route, {
        preflight_id: 'preflight-1', status: 'cancelled', stage: 'cancelled',
        processed: 0, total: null, percentage: null, eta_seconds: null,
        summary: null, error: null,
      })
      : json(route, {
        preflight_id: 'preflight-1', status: 'running', stage: 'discovering',
        processed: 0, total: null, percentage: null, eta_seconds: null,
        summary: null, error: null,
      }),
  )

  await page.goto('/')
  await page.getByRole('button', { name: /SCIENCE/ }).click()
  await page.getByRole('button', { name: 'Run preflight' }).click()
  await expect(page.getByText('Scanning files…')).toBeVisible()

  const cancelled = page.waitForRequest((request) =>
    request.method() === 'DELETE' && request.url().endsWith('/api/v1/preflights/preflight-1'),
  )
  await page.getByRole('button', { name: 'Refresh' }).click()
  await cancelled
  await expect(page.getByText('Scanning files…')).toHaveCount(0)
  expect(createdJobs).toEqual([])
  expect(cancellations).toEqual(['/api/v1/preflights/preflight-1'])
})

test('recovers a retained preflight invalidated by an API restart', async ({ page }) => {
  await mockCommon(page)
  await page.route('**/api/v1/devices/device-1/tree?*', (route) => json(route, []))
  await page.route('**/api/v1/devices/device-1/preflight', (route) => json(route, {
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
  }))
  await page.route('**/api/v1/preflights/preflight-stale', (route) =>
    json(route, { detail: 'preflight not found' }, 404))

  await page.goto('/')
  await page.getByRole('button', { name: /SCIENCE/ }).click()
  await page.getByRole('button', { name: 'Run preflight' }).click()
  await expect(page.getByText('Storage check passed.')).toBeVisible()

  await page.getByRole('button', { name: 'Refresh' }).click()
  await expect(page.getByText('Storage check passed.')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Confirm and start' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Run preflight' })).toBeEnabled()
})

test('Refresh releases a completed zero-image preflight without creating a job', async ({ page }) => {
  const mutations: string[] = []
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().endsWith('/api/v1/jobs')) {
      mutations.push(`${request.method()} ${new URL(request.url()).pathname}`)
    }
    if (request.method() === 'DELETE') {
      mutations.push(`${request.method()} ${new URL(request.url()).pathname}`)
    }
  })
  await mockCommon(page)
  await page.route('**/api/v1/devices/device-1/tree?*', (route) => json(route, []))
  await page.route('**/api/v1/devices/device-1/preflight', (route) => json(route, {
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
  }))
  await page.route('**/api/v1/preflights/preflight-1', (route) => json(route, {
    preflight_id: 'preflight-1',
    status: 'cancelled',
    stage: 'cancelled',
    processed: 0,
    total: 0,
    percentage: null,
    eta_seconds: null,
    summary: null,
    error: null,
  }))

  await page.goto('/')
  await page.getByRole('button', { name: /SCIENCE/ }).click()
  await page.getByRole('button', { name: 'Run preflight' }).click()
  await expect(page.getByText('Storage check passed.')).toBeVisible()

  await page.getByRole('button', { name: 'Refresh' }).click()
  await expect(page.getByText('Storage check passed.')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Confirm and start' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Run preflight' })).toBeVisible()
  expect(mutations).toEqual(['DELETE /api/v1/preflights/preflight-1'])
})

test('Refresh keeps an existing non-terminal job attached without cancellation', async ({ page }) => {
  const mutations: string[] = []
  page.on('request', (request) => {
    if (request.method() === 'DELETE') mutations.push(`${request.method()} ${request.url()}`)
  })
  await mockCommon(page)
  await mockSession(page, job('INFERENCING'))
  await page.route('**/api/v1/jobs/job-12345678', (route) => json(route, job('INFERENCING')))

  await page.goto('/')
  await expect(page.locator('.job__state .status')).toHaveText('INFERENCING')
  await page.getByRole('button', { name: 'Refresh' }).click()

  await expect(page.locator('.job__state .status')).toHaveText('INFERENCING')
  await expect(page.locator('.job__state code')).toHaveText('job-1234')
  expect(await page.evaluate(() => localStorage.length)).toBe(0)
  expect(mutations).toEqual([])
})

test('Refresh clears stale source after native desktop eject without deleting the job', async ({ page }) => {
  let connected = true
  const deletions: string[] = []
  page.on('request', (request) => {
    if (request.method() === 'DELETE') deletions.push(request.url())
  })
  await page.route('**/api/v1/devices', (route) => json(route, connected ? [{
    device_id: 'device-1', filesystem: 'ext4', label: 'SCIENCE', size_bytes: 1024 ** 3,
  }] : []))
  await page.route('**/api/v1/devices/availability', (route) =>
    json(route, { unsupported_removable_media_detected: false }))
  await page.route('**/api/v1/runtime', (route) => json(route, {
    mode: 'inference_enabled', status: 'ready', source: 'platform', gpu_lock_available: true,
  }))
  await page.route('**/api/v1/devices/device-1/tree?*', (route) => json(route, []))
  await page.route('**/api/v1/devices/device-1/preflight', (route) => json(route, {
    file_count: 2,
    source_bytes: 2048,
    required_free_bytes: 4096,
    local_free_bytes: 8192,
    has_enough_space: true,
  }))
  await page.route('**/api/v1/jobs', async (route) => {
    await json(route, job('CREATED'), 202)
  })
  await page.route('**/api/v1/jobs/job-12345678', (route) =>
    json(route, job('COMPLETED')))
  await page.route('**/api/v1/jobs/job-12345678/summary', (route) => json(route, {
    score_type: 'uncalibrated_softmax',
    counts,
    archive_path: 'jobs/job-12345678/archive/source_images.zip',
    usb_ejected: false,
  }))

  await page.goto('/')
  await page.getByRole('button', { name: /SCIENCE/ }).click()
  await expect(page.getByText('Selected folder:')).toBeVisible()
  await page.getByRole('button', { name: 'Run preflight' }).click()
  await expect(page.getByText('Storage check passed.')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm and start' }).click()
  await expect(page.locator('.job__state .status')).toHaveText('COMPLETED')

  connected = false
  await page.getByRole('button', { name: 'Refresh' }).click()
  await expect(page.getByText('No supported USB device detected.')).toBeVisible()
  await expect(page.getByText('Selected folder:')).toHaveCount(0)
  await expect(page.locator('.job__state .status')).toHaveText('COMPLETED')
  await expect(page.locator('.job__state code')).toHaveText('job-1234')
  expect(deletions).toEqual([])
})

test('shows read-only training status and GPU lock contention', async ({ page }) => {
  await mockCommon(page)
  await page.route('**/api/v1/runtime', (route) => json(route, {
    mode: 'training',
    status: 'training',
    source: 'training_entrypoint',
    gpu_lock_available: false,
  }))

  await page.goto('/')

  await expect(page.getByText('Training in progress')).toBeVisible()
  await expect(page.getByText('Controlled by the training entrypoint.')).toBeVisible()
  await expect(page.getByText('GPU lock in use')).toBeVisible()
  await expect(page.getByRole('combobox')).toHaveCount(0)
})

test('explains unsupported removable media without exposing a selectable device', async ({ page }) => {
  await page.route('**/api/v1/devices', (route) => json(route, []))
  await page.route('**/api/v1/devices/availability', (route) =>
    json(route, { unsupported_removable_media_detected: true }),
  )
  await page.route('**/api/v1/runtime', (route) =>
    json(route, {
      mode: 'inference_enabled',
      status: 'ready',
      source: 'platform',
      gpu_lock_available: true,
    }),
  )

  await page.goto('/')

  await expect(page.getByText('No supported USB device detected.')).toBeVisible()
  await expect(page.getByText(/filesystem is unsupported/)).toBeVisible()
  await expect(page.getByText(/Supported formats: FAT16, FAT32, exFAT, NTFS, and ext4/)).toBeVisible()
  await expect(page.locator('.device-list .device')).toHaveCount(0)
})

test('shows exact ETA values as readable approximate durations', async ({ page }) => {
  await mockCommon(page)
  const active = job('INFERENCING')
  active.progress.stage_eta_seconds = 125
  active.progress.job_eta_seconds = 4210
  await mockSession(page, active)
  await page.route('**/api/v1/jobs/job-12345678', (route) => json(route, active))

  await page.goto('/')

  await expect(page.getByText('This stage')).toBeVisible()
  await expect(page.getByText('About 3 min')).toBeVisible()
  await expect(page.getByText('Estimated completion')).toBeVisible()
  await expect(page.getByText('About 1 hr 11 min')).toBeVisible()
  await expect(page.getByText('stage_eta_seconds')).toHaveCount(0)
  await expect(page.getByText('125s')).toHaveCount(0)
})

test('shows an API error without leaking a traceback', async ({ page }) => {
  await page.route('**/api/v1/devices', (route) =>
    json(route, { detail: 'USB agent is unavailable.' }, 503),
  )
  await page.route('**/api/v1/devices/availability', (route) =>
    json(route, { unsupported_removable_media_detected: false }),
  )
  await page.route('**/api/v1/runtime', (route) =>
    json(route, {
      mode: 'inference_enabled',
      status: 'ready',
      source: 'platform',
      gpu_lock_available: true,
    }),
  )

  await page.goto('/')

  await expect(page.getByRole('alert')).toHaveText('USB agent is unavailable.')
  await expect(page.locator('body')).not.toContainText('Traceback')
})

for (const state of ['COMPLETED', 'FAILED', 'SECURITY_BLOCKED', 'CANCELLED'] as const) {
  test(`clears a restored terminal job ${state} without deleting persisted data`, async ({ page }) => {
    const mutations: string[] = []
    const jobReads: string[] = []
    page.on('request', (request) => {
      if ((request.method() === 'POST' || request.method() === 'DELETE')
        && !/\/api\/v1\/session\/(open|close|heartbeat)$/.test(request.url())) {
        mutations.push(`${request.method()} ${request.url()}`)
      }
      if (request.method() === 'GET' && request.url().endsWith('/api/v1/jobs/job-12345678')) {
        jobReads.push(request.url())
      }
    })
    await mockCommon(page)
    await page.route('**/api/v1/jobs/job-12345678', (route) =>
      json(
        route,
        job(state, state === 'SECURITY_BLOCKED' ? {
          code: 'malware_detected',
          message: 'Import stopped because the security scan found a threat.',
          relative_paths: ['science/eicar.jpg'],
        } : null),
      ),
    )

    await page.goto('/')
    await page.evaluate(() => localStorage.setItem('plankton-active-job', 'job-12345678'))
    await page.reload()

    await expect(page.getByText('Choose a source folder to review its import requirements.')).toBeVisible()
    await expect(page.locator('.job__state')).toHaveCount(0)
    expect(await page.evaluate(() => localStorage.getItem('plankton-active-job'))).toBeNull()
    const readsAfterReset = jobReads.length
    // The server now discovers the active job; a stale local pointer is not authoritative.
    expect(readsAfterReset).toBe(0)

    await page.reload()
    await expect(page.getByText('Choose a source folder to review its import requirements.')).toBeVisible()
    await expect(page.locator('.job__state')).toHaveCount(0)
    expect(jobReads).toHaveLength(readsAfterReset)
    expect(mutations).toEqual([])
  })
}

for (const state of ['SCANNING', 'IMPORTING', 'INFERENCING', 'EXPORTING', 'WAITING_FOR_GPU', 'WAITING_FOR_USB'] as const) {
  test(`keeps restored non-terminal job ${state} attached without mutation`, async ({ page }) => {
    const mutations: string[] = []
    page.on('request', (request) => {
      if ((request.method() === 'POST' || request.method() === 'DELETE')
        && !/\/api\/v1\/session\/(open|close|heartbeat)$/.test(request.url())) {
        mutations.push(`${request.method()} ${request.url()}`)
      }
    })
    await mockCommon(page)
    await mockSession(page, job(state))
    await page.route('**/api/v1/jobs/job-12345678', (route) => json(route, job(state)))

    await page.goto('/')
    await page.evaluate(() => localStorage.clear())
    await page.reload()

    await expect(page.locator('.job__state .status')).toHaveText(state.replaceAll('_', ' '))
    await expect(page.locator('.job__state code')).toHaveText('job-1234')
    expect(await page.evaluate(() => localStorage.length)).toBe(0)

    await page.reload()
    await expect(page.locator('.job__state .status')).toHaveText(state.replaceAll('_', ' '))
    await expect(page.locator('.job__state code')).toHaveText('job-1234')
    expect(await page.evaluate(() => localStorage.length)).toBe(0)
    expect(mutations).toEqual([])
  })
}

for (const [state, message] of [
  ['WAITING_FOR_GPU', 'Waiting for training to finish or for the GPU lock.'],
  ['WAITING_FOR_USB', 'Reconnect the same USB device to export and finish the job.'],
] as const) {
  test(`restores and explains ${state}`, async ({ page }) => {
    await mockCommon(page)
    await mockSession(page, job(state))
    await page.route('**/api/v1/jobs/job-12345678', (route) => json(route, job(state)))
    await page.route('**/api/v1/devices/device-1/tree?*', (route) => json(route, []))

    await page.goto('/')

    await expect(page.locator('.job__state .status')).toHaveText(state.replaceAll('_', ' '))
    await expect(page.getByText(message)).toBeVisible()
    await expect(page.getByText('Paused')).toHaveCount(2)
    await page.getByRole('button', { name: /SCIENCE/ }).click()
    await expect(page.getByRole('button', { name: 'Run preflight' })).toBeDisabled()
  })
}
