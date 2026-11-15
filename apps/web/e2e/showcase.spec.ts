import { mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { expect, test, type Route } from '@playwright/test'

test.use({ viewport: { width: 1440, height: 1080 }, deviceScaleFactor: 1, serviceWorkers: 'block' })

test('captures the synthetic README walkthrough', async ({ page }) => {
  test.skip(process.env.PICASSO_SCREENSHOTS !== '1', 'Opt in to regenerate published screenshots.')
  expect(process.env.PLAYWRIGHT_BASE_URL).toBeUndefined()
  const output = fileURLToPath(new URL('../../../docs/screenshots/', import.meta.url))
  await mkdir(output, { recursive: true })
  const unexpected: string[] = []
  page.on('pageerror', (error) => unexpected.push(error.message))
  // Block unmocked APIs and external requests before they can reach a service.
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url())
    if (url.origin === 'http://127.0.0.1:4173' && !url.pathname.startsWith('/api/')) {
      return route.continue()
    }
    unexpected.push(`${route.request().method()} ${url.pathname}`)
    return route.abort()
  })
  const json = (route: Route, body: object, status = 200) => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  })
  let completed = false
  const counts = { discovered: 12, predicted: 11, low_confidence: 3, rejected: 1 }
  const job = () => ({
    job_id: 'demo-001', state: completed ? 'COMPLETED' : 'INFERENCING',
    stage: completed ? 'COMPLETED' : 'INFERENCING',
    started_at: '2026-11-15T00:00:00Z',
    completed_at: completed ? '2026-11-15T00:01:00Z' : null,
    counts: completed ? counts : { ...counts, predicted: 5, low_confidence: 1 },
    failure: null,
    progress: {
      total: 12, processed: completed ? 12 : 6, percentage: completed ? 100 : 50,
      images_per_second: 1.5, eta_seconds: null,
      stage_eta_seconds: completed ? 0 : 4, job_eta_seconds: null,
    },
  })
  await page.route('**/api/v1/session/open', (route) =>
    json(route, { session_id: route.request().postDataJSON().session_id, job: null }))
  for (const action of ['close', 'heartbeat']) {
    await page.route(`**/api/v1/session/${action}`, (route) => json(route, { ok: true }))
  }
  await page.route('**/api/v1/devices', (route) => json(route, [{
    device_id: 'demo-device', filesystem: 'ext4', label: 'DEMO USB', size_bytes: 8 * 1024 ** 3,
  }]))
  await page.route('**/api/v1/devices/availability', (route) =>
    json(route, { unsupported_removable_media_detected: false }))
  await page.route('**/api/v1/runtime', (route) => json(route, {
    mode: 'inference_enabled', status: 'ready',
    source: 'platform', gpu_lock_available: true,
  }))
  await page.route('**/api/v1/devices/demo-device/tree?*', (route) => {
    const path = new URL(route.request().url()).searchParams.get('path')
    return json(route, path === 'demo-batch' ? [
      { name: 'images', relative_path: 'demo-batch/images', kind: 'directory', size_bytes: null },
      { name: 'metadata.csv', relative_path: 'demo-batch/metadata.csv', kind: 'file', size_bytes: 1024 },
    ] : [{ name: 'demo-batch', relative_path: 'demo-batch', kind: 'directory', size_bytes: null }])
  })
  await page.route('**/api/v1/devices/demo-device/preflight', (route) => {
    expect(route.request().postDataJSON()).toMatchObject({
      session_id: expect.any(String), source_relative_path: 'demo-batch',
    })
    return json(route, {
      preflight_id: 'demo-preflight', status: 'completed', stage: 'completed',
      processed: 13, total: 13, percentage: 100, eta_seconds: 0, error: null,
      summary: {
        file_count: 12, source_bytes: 6 * 1024 ** 2,
        required_free_bytes: 12 * 1024 ** 2, local_free_bytes: 20 * 1024 ** 3,
        has_enough_space: true,
      },
    })
  })
  await page.route('**/api/v1/jobs', (route) => {
    expect(route.request().postDataJSON()).toEqual({
      session_id: expect.any(String), device_id: 'demo-device',
      source_relative_path: 'demo-batch', metadata_relative_path: 'demo-batch/metadata.csv',
      preflight_id: 'demo-preflight',
    })
    return json(route, job(), 202)
  })
  await page.route('**/api/v1/jobs/demo-001', (route) => json(route, job()))
  await page.route('**/api/v1/jobs/demo-001/summary', (route) => json(route, {
    score_type: 'uncalibrated_softmax', counts,
    archive_path: '/demo/jobs/demo-001/archive/source_images.zip',
  }))

  await page.goto('/')
  // This capture-only label is deliberately outside the unchanged application UI.
  await page.evaluate(() => {
    const banner = document.createElement('aside')
    banner.textContent = 'SIMULATED UI · Synthetic data · No model or USB used'
    banner.style.cssText = 'padding:12px 24px;background:#173d37;color:#fff;text-align:center;font:600 16px system-ui;letter-spacing:.02em'
    document.body.prepend(banner)
  })
  await page.getByRole('button', { name: /DEMO USB/ }).click()
  await page.getByRole('button', { name: 'demo-batch' }).click()
  await page.getByLabel('Optional metadata CSV path').fill('demo-batch/metadata.csv')
  await expect(page.locator('.pathbar code')).toHaveText('/demo-batch')
  await expect(page.getByRole('button', { name: 'Run preflight' })).toBeEnabled()
  await expect(page.getByText('Ready for inference', { exact: true })).toBeVisible()
  const capture = async (name: string) => {
    await expect(page.getByRole('alert')).toHaveCount(0)
    expect(unexpected).toEqual([])
    await page.screenshot({ path: `${output}${name}.png`, fullPage: true, animations: 'disabled' })
  }
  await capture('01-select-source')

  await page.getByRole('button', { name: 'Run preflight' }).click()
  await expect(page.getByText('Storage check passed.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Confirm and start' })).toBeEnabled()
  await capture('02-review-preflight')

  await page.getByRole('button', { name: 'Confirm and start' }).click()
  await expect(page.locator('.job__state .status')).toHaveText('INFERENCING')
  await expect(page.getByText('50.0%', { exact: true })).toBeVisible()
  await capture('03-follow-progress')

  completed = true
  await expect(page.getByText('Export complete', { exact: true })).toBeVisible()
  await expect(page.getByText('11 predictions and 1 rejections exported.')).toBeVisible()
  await expect(page.getByText('Ready for inference', { exact: true })).toBeVisible()
  await capture('04-review-results')
  await page.getByRole('button', { name: 'Prepare another job' }).click()
  await expect(page.getByText('Choose a source folder to review its import requirements.')).toBeVisible()
  expect(unexpected).toEqual([])
})
