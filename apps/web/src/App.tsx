import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'
import logoPicasso from './assets/logo_picasso.svg'

type Device = {
  device_id: string
  filesystem: string
  label: string | null
  size_bytes: number
}

type DeviceAvailability = {
  unsupported_removable_media_detected: boolean
  missing_filesystem_uuid_detected?: boolean
}

type DirectoryEntry = {
  name: string
  relative_path: string
  kind: 'directory' | 'file'
  size_bytes: number | null
}

type Preflight = {
  file_count: number
  source_bytes: number
  required_free_bytes: number
  local_free_bytes: number
  has_enough_space: boolean
}

type PreflightRun = {
  preflight_id: string
  status: 'running' | 'completed' | 'cancelled' | 'failed'
  stage: string
  processed: number
  total: number | null
  percentage: number | null
  eta_seconds: number | null
  summary: Preflight | null
  error: string | null
}

type Counts = {
  discovered: number
  predicted: number
  low_confidence: number
  rejected: number
}

type Job = {
  job_id: string
  state: string
  stage: string
  started_at: string
  completed_at: string | null
  counts: Counts
  failure: {
    code: string
    message: string
    relative_paths: string[]
  } | null
  progress: {
    total: number
    processed: number
    percentage: number
    images_per_second: number
    eta_seconds: number | null
    stage_eta_seconds: number | null
    job_eta_seconds: number | null
    total_bytes?: number
    processed_bytes?: number
    throughput_bytes_per_second?: number
    progress_unit?: 'images' | 'bytes'
    updated_at?: string
  }
}

type Runtime = {
  mode: 'inference_enabled' | 'training'
  status: 'ready' | 'training' | 'inference_running' | 'admin_paused'
  source: 'platform' | 'training_entrypoint' | 'administrator'
  gpu_lock_available: boolean
}

type JobSummary = {
  score_type: string
  counts: Counts
  archive_path: string
}

const terminalStates = new Set(['COMPLETED', 'FAILED', 'SECURITY_BLOCKED', 'CANCELLED'])

class HttpError extends Error {
  readonly status: number
  readonly preflightId: string | null

  constructor(status: number, message: string, preflightId: string | null = null) {
    super(message)
    this.name = 'HttpError'
    this.status = status
    this.preflightId = preflightId
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: init?.body ? { 'Content-Type': 'application/json', ...init.headers } : init?.headers,
  })
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new HttpError(response.status, body?.detail ?? `Request failed (${response.status})`,
      response.headers?.get('X-PICASSO-Preflight-ID') ?? null)
  }
  return response.json() as Promise<T>
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes
  let unit = -1
  do {
    value /= 1024
    unit += 1
  } while (value >= 1024 && unit < units.length - 1)
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[unit]}`
}

function formatEta(seconds: number | null, state: string): string {
  if (state === 'WAITING_FOR_GPU' || state === 'WAITING_FOR_USB') return 'Paused'
  if (state === 'COMPLETED') return 'Complete'
  if (state === 'FAILED' || state === 'SECURITY_BLOCKED') return 'Stopped'
  if (state === 'CANCELLED') return 'Stopped'
  if (seconds === null) return 'Calculating…'
  if (seconds < 60) return 'Less than a minute'
  const minutes = Math.ceil(seconds / 60)
  if (minutes < 60) return `About ${minutes} min`
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  return remainingMinutes === 0
    ? `About ${hours} hr`
    : `About ${hours} hr ${remainingMinutes} min`
}

function parentPath(path: string): string {
  const parts = path.split('/').filter((part) => part && part !== '.')
  parts.pop()
  return parts.join('/') || '.'
}

function stateMessage(job: Job): string {
  if (job.state === 'WAITING_FOR_GPU') return 'Waiting for training to finish or for the GPU lock.'
  if (job.state === 'WAITING_FOR_USB') return 'Reconnect the same USB device to export and finish the job.'
  if (job.state === 'SECURITY_BLOCKED') return job.failure?.message ?? 'Import stopped by the USB security scan.'
  if (job.state === 'FAILED') return job.failure?.message ?? 'The job stopped. Review service logs before trying again.'
  if (job.state === 'CANCELLED') return 'The job was cancelled before completion.'
  if (job.state === 'COMPLETED') return 'Results were written to the USB device and returned to Ubuntu Files.'
  return 'The worker is processing this job.'
}

function runtimeStatus(runtime: Runtime): { label: string; detail: string } {
  if (runtime.status === 'training') {
    return { label: 'Training in progress', detail: 'Controlled by the training entrypoint.' }
  }
  if (runtime.status === 'inference_running') {
    return { label: 'Inference in progress', detail: 'The platform is using the GPU.' }
  }
  if (runtime.status === 'admin_paused') {
    return { label: 'Inference paused', detail: 'An administrator override is active.' }
  }
  return { label: 'Ready for inference', detail: 'The workstation is available.' }
}

function isStalePreflightError(reason: unknown): boolean {
  if (!(reason instanceof HttpError)) return false
  return reason.status === 404
    || reason.status === 410
    || (reason.status === 409 && reason.message === 'preflight is unavailable')
}

function App() {
  const [devices, setDevices] = useState<Device[]>([])
  const [unsupportedRemovableMediaDetected, setUnsupportedRemovableMediaDetected] = useState(false)
  const [missingFilesystemUuidDetected, setMissingFilesystemUuidDetected] = useState(false)
  const [deviceId, setDeviceId] = useState('')
  const [entries, setEntries] = useState<DirectoryEntry[]>([])
  const [currentPath, setCurrentPath] = useState('.')
  const [metadataPath, setMetadataPath] = useState('')
  const [preflight, setPreflight] = useState<Preflight | null>(null)
  const [preflightRun, setPreflightRun] = useState<PreflightRun | null>(null)
  const [preflightId, setPreflightId] = useState<string | null>(null)
  const [runtime, setRuntime] = useState<Runtime | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const [summary, setSummary] = useState<JobSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const cancellingPreflight = useRef<Promise<boolean> | null>(null)
  const sessionId = useRef('')
  const [sessionReady, setSessionReady] = useState(false)

  const openWindowSession = useCallback(async () => {
    const native = window as Window & {
      webkit?: { messageHandlers?: { picassoSession?: { postMessage: (id: string) => void } } }
    }
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const id = sessionId.current || crypto.randomUUID()
      sessionId.current = id
      native.webkit?.messageHandlers?.picassoSession?.postMessage(id)
      try {
        const restored = await requestJson<{ job: Job | null }>('/api/v1/session/open', {
          method: 'POST', body: JSON.stringify({ session_id: id }),
        })
        if (sessionId.current !== id) return
        setJob(restored.job)
        setPreflight(null)
        setPreflightRun(null)
        setPreflightId(null)
        setDeviceId('')
        setEntries([])
        setCurrentPath('.')
        if (restored.job) setError('')
        setSessionReady(true)
        return
      } catch (reason) {
        if (sessionId.current !== id) return
        setSessionReady(false)
        if (reason instanceof HttpError && reason.status === 409 && attempt === 0) {
          sessionId.current = ''
          continue
        }
        setError('Could not restore or release the previous session. Press Refresh to retry.')
        return
      }
    }
  }, [])

  const resetPreflightLifecycle = useCallback(() => {
    setPreflight(null)
    setPreflightRun(null)
    setPreflightId(null)
  }, [])

  const refreshDevices = useCallback(async (): Promise<Device[] | null> => {
    setError('')
    setUnsupportedRemovableMediaDetected(false)
    setMissingFilesystemUuidDetected(false)
    try {
      const [nextDevices, availability] = await Promise.all([
        requestJson<Device[]>('/api/v1/devices'),
        requestJson<DeviceAvailability>('/api/v1/devices/availability'),
      ])
      setDevices(nextDevices)
      setUnsupportedRemovableMediaDetected(availability.unsupported_removable_media_detected)
      setMissingFilesystemUuidDetected(availability.missing_filesystem_uuid_detected === true)
      return nextDevices
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not list USB devices.')
      return null
    }
  }, [])

  const refreshRuntime = useCallback(async () => {
    try {
      setRuntime(await requestJson<Runtime>('/api/v1/runtime'))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not read runtime mode.')
    }
  }, [])

  const refreshJob = useCallback(async (jobId: string): Promise<void> => {
    try {
      const nextJob = await requestJson<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}`)
      setJob(nextJob)
      if (nextJob.state === 'COMPLETED') {
        setSummary(
          await requestJson<JobSummary>(
            `/api/v1/jobs/${encodeURIComponent(jobId)}/summary`,
          ),
        )
      }
    } catch (reason) {
      if (reason instanceof HttpError && reason.status === 404) {
        setJob(null)
        setSummary(null)
        return
      }
      setError(reason instanceof Error ? reason.message : 'Could not read job status.')
    }
  }, [])

  useEffect(() => {
    try { localStorage.removeItem('plankton-active-job') } catch { /* Storage is optional. */ }
    void refreshDevices()
    void refreshRuntime()
    void openWindowSession()
    const close = () => {
      const id = sessionId.current
      sessionId.current = ''
      if (id) void fetch('/api/v1/session/close', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: id }), keepalive: true,
      }).catch(() => { /* The server heartbeat expiry retains cleanup responsibility. */ })
    }
    window.addEventListener('pagehide', close)
    return () => {
      window.removeEventListener('pagehide', close)
      close()
    }
  }, [refreshDevices, refreshRuntime, openWindowSession])

  useEffect(() => {
    if (!sessionReady) return
    const timer = window.setInterval(() => {
      const id = sessionId.current
      if (!id) return
      void requestJson('/api/v1/session/heartbeat', {
        method: 'POST', body: JSON.stringify({ session_id: id }),
      }).catch(() => {
        if (sessionId.current !== id) return
        setSessionReady(false)
        setError('Session connection lost. Press Refresh to recover before continuing.')
      })
    }, 5000)
    return () => window.clearInterval(timer)
  }, [sessionReady])

  useEffect(() => {
    const jobId = job?.job_id
    const jobState = job?.state
    if (!jobId || !jobState || terminalStates.has(jobState)) return
    const timer = window.setInterval(() => void refreshJob(jobId), 1000)
    return () => window.clearInterval(timer)
  }, [job?.job_id, job?.state, refreshJob])

  useEffect(() => {
    const run = preflightRun
    if (!run || run.status !== 'running' || run.preflight_id === 'pending') return
    let active = true
    const timer = window.setInterval(() => {
      void requestJson<PreflightRun>(`/api/v1/preflights/${encodeURIComponent(run.preflight_id)}`)
        .then((next) => {
          if (!active) return
          setPreflightRun(next)
          if (next.status === 'completed' && next.summary) {
            setPreflight(next.summary)
            setPreflightId(next.preflight_id)
          }
          if (next.status === 'failed') setError(next.error ?? 'Preflight failed.')
          if (next.status === 'cancelled') {
            resetPreflightLifecycle()
          }
        })
        .catch((reason) => {
          if (!active) return
          if (isStalePreflightError(reason)) {
            resetPreflightLifecycle()
            setError('Preflight expired after the USB service restarted. Choose the source folder again.')
            return
          }
          setError(reason instanceof Error ? reason.message : 'Could not read preflight status.')
        })
    }, 500)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [preflightRun, resetPreflightLifecycle])

  async function cancelActivePreflight(): Promise<boolean> {
    if (cancellingPreflight.current) return cancellingPreflight.current
    const run = preflightRun
    const activePreflightId = run && run.preflight_id !== 'pending' ? run.preflight_id : preflightId
    if (!activePreflightId) return true
    const cancellation = (async () => {
      try {
        await requestJson<PreflightRun>(
          `/api/v1/preflights/${encodeURIComponent(activePreflightId)}`,
          { method: 'DELETE' },
        )
        resetPreflightLifecycle()
        return true
      } catch (reason) {
        if (isStalePreflightError(reason)) {
          resetPreflightLifecycle()
          return true
        }
        setError('Could not release preflight. Try again.')
        return false
      } finally {
        cancellingPreflight.current = null
      }
    })()
    cancellingPreflight.current = cancellation
    return cancellation
  }

  async function openDirectory(nextDeviceId: string, path: string) {
    if (!sessionReady) return
    setBusy(true)
    setError('')
    try {
      if (!await cancelActivePreflight()) return
      setDeviceId(nextDeviceId)
      setCurrentPath(path)
      setEntries(
        await requestJson<DirectoryEntry[]>(
          `/api/v1/devices/${encodeURIComponent(nextDeviceId)}/tree?path=${encodeURIComponent(path)}`,
        ),
      )
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not open this folder.')
    } finally {
      setBusy(false)
    }
  }

  async function runPreflight() {
    if (!sessionReady || busy) return
    setBusy(true)
    setError('')
    setPreflight(null)
    setPreflightId(null)
    setPreflightRun({
      preflight_id: 'pending',
      status: 'running',
      stage: 'discovering',
      processed: 0,
      total: null,
      percentage: null,
      eta_seconds: null,
      summary: null,
      error: null,
    })
    try {
      const result = await requestJson<Preflight | PreflightRun>(
          `/api/v1/devices/${encodeURIComponent(deviceId)}/preflight`,
          {
            method: 'POST',
            body: JSON.stringify({
              session_id: sessionId.current,
              source_relative_path: currentPath,
              metadata_relative_path: metadataPath.trim() || null,
            }),
          },
        )
      if ('status' in result) {
        setPreflightRun(result)
        if (result.status === 'completed' && result.summary) {
          setPreflight(result.summary)
          setPreflightId(result.preflight_id)
        }
      } else {
        setPreflight(result)
        setPreflightRun(null)
        setPreflightId(null)
      }
    } catch (reason) {
      if (reason instanceof HttpError && reason.status === 503 && reason.message === 'native mount is busy') {
        // This initial claim was refused before an asynchronous run was accepted.
        setPreflightRun((run) => run?.preflight_id === 'pending' ? null : run)
        setError('This USB is being used by another program. Save and close documents on this USB, then retry Preflight. '
          + 'Also close Files windows showing this USB, whether you are using a local or remote desktop. '
          + 'Click Run preflight when ready to retry.')
      } else {
        if (reason instanceof HttpError && reason.preflightId) {
          // Keep the failed claim addressable: Refresh must release it before clearing selection.
          const failedId = reason.preflightId
          setPreflightId(failedId)
          setPreflightRun((run) => run?.preflight_id === 'pending' ? {
            ...run, preflight_id: failedId, status: 'failed', stage: 'failed', error: reason.message,
          } : run)
        }
        setError(reason instanceof Error ? reason.message : 'Preflight failed.')
      }
    } finally {
      setBusy(false)
    }
  }

  async function cancelPreflight() {
    await cancelActivePreflight()
  }

  async function refreshSource() {
    setBusy(true)
    setError('')
    try {
      if (!sessionReady) {
        await openWindowSession()
        return
      }
      const activeJob = Boolean(job && !terminalStates.has(job.state))
      if (!activeJob) {
        if (!await cancelActivePreflight()) return
        setPreflight(null)
      }
      const nextDevices = await refreshDevices()
      if (!activeJob && nextDevices) {
        if (deviceId && !nextDevices.some((device) => device.device_id === deviceId)) {
          setDeviceId('')
          setEntries([])
          setCurrentPath('.')
        }
      }
    } finally {
      setBusy(false)
    }
  }

  async function startJob() {
    if (!sessionReady || busy) return
    setBusy(true)
    setError('')
    try {
      const nextJob = await requestJson<Job>('/api/v1/jobs', {
        method: 'POST',
        body: JSON.stringify({
          session_id: sessionId.current,
          device_id: deviceId,
          source_relative_path: currentPath,
          metadata_relative_path: metadataPath.trim() || null,
          preflight_id: preflightId,
        }),
      })
      setJob(nextJob)
      setPreflight(null)
      setPreflightRun(null)
      setPreflightId(null)
      if (nextJob.state === 'COMPLETED') {
        setSummary(
          await requestJson<JobSummary>(
            `/api/v1/jobs/${encodeURIComponent(nextJob.job_id)}/summary`,
          ),
        )
      } else {
        setSummary(null)
      }
    } catch (reason) {
      if (isStalePreflightError(reason)) {
        resetPreflightLifecycle()
        setError('Preflight expired after the USB service restarted. Choose the source folder again.')
      } else {
        setError(reason instanceof Error ? reason.message : 'Could not start the job.')
        await openWindowSession()
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="app">
      <header className="hero">
        <div>
          <h1 className="brand-mark">
            <img src={logoPicasso} alt="PICASSO" />
          </h1>
          <p className="brand-subtitle">Plankton Image Classification &amp; Analysis System for Scientific Observation</p>
          <p className="lede">Select an image folder on USB, review preflight, then start one local job.</p>
        </div>
        {runtime && (
          <section className="runtime" aria-labelledby="runtime-title">
            <h2 id="runtime-title">Workstation status</h2>
            <strong>{runtimeStatus(runtime).label}</strong>
            <p>{runtimeStatus(runtime).detail}</p>
            <p className={runtime.gpu_lock_available ? 'good' : 'waiting'}>
              GPU lock {runtime.gpu_lock_available ? 'available' : 'in use'}
            </p>
          </section>
        )}
      </header>

      {error && <p className="alert" role="alert">{error}</p>}

      <div className="workspace">
        <section className="panel" aria-labelledby="source-title">
          <div className="panel__heading">
            <div>
              <p className="step">01</p>
              <h2 id="source-title">USB source</h2>
            </div>
            <button className="button button--quiet" type="button" onClick={() => void refreshSource()}>
              Refresh
            </button>
          </div>

          {devices.length === 0 ? (
            <p className="empty">No supported USB device detected.</p>
          ) : (
            <div className="device-list">
              {devices.map((device) => (
                <button
                  className={device.device_id === deviceId ? 'device device--selected' : 'device'}
                  type="button"
                  key={device.device_id}
                  disabled={!sessionReady}
                  onClick={() => void openDirectory(device.device_id, '.')}
                >
                  <strong>{device.label || 'Untitled USB'}</strong>
                  <span>{device.filesystem.toUpperCase()} · {formatBytes(device.size_bytes)}</span>
                </button>
              ))}
            </div>
          )}
          {unsupportedRemovableMediaDetected && (
            <p className="waiting" role="status">
              A USB device was detected, but its filesystem is unsupported. Supported formats: FAT16, FAT32, exFAT, NTFS, and ext4.
            </p>
          )}
          {missingFilesystemUuidDetected && (
            <p className="waiting" role="status">
              A USB device was detected, but its filesystem UUID is missing. Reconnect a device with a filesystem UUID before creating a job.
            </p>
          )}

          {deviceId && (
            <div className="browser">
              <div className="pathbar">
                <button
                  type="button"
                  className="icon-button"
                  disabled={currentPath === '.' || busy}
                  onClick={() => void openDirectory(deviceId, parentPath(currentPath))}
                  aria-label="Open parent folder"
                >
                  ↑
                </button>
                <code>/{currentPath === '.' ? '' : currentPath}</code>
              </div>
              <ul className="entries">
                {entries.map((entry) => (
                  <li key={entry.relative_path}>
                    {entry.kind === 'directory' ? (
                      <button type="button" onClick={() => void openDirectory(deviceId, entry.relative_path)}>
                        <span aria-hidden="true">▸</span> {entry.name}
                      </button>
                    ) : (
                      <span><span aria-hidden="true">·</span> {entry.name}</span>
                    )}
                  </li>
                ))}
              </ul>
              <p className="selected-folder">Selected folder: <strong>/{currentPath === '.' ? '' : currentPath}</strong></p>
              <label>
                <span>Optional metadata CSV path</span>
                <input
                  value={metadataPath}
                  onChange={(event) => {
                    setMetadataPath(event.target.value)
                    if (preflightRun || preflightId) {
                      setBusy(true)
                      void cancelActivePreflight().finally(() => setBusy(false))
                    } else {
                      setPreflight(null)
                    }
                  }}
                  placeholder="science/metadata.csv"
                />
              </label>
              <button
                className="button"
                type="button"
                disabled={!sessionReady || busy
                  || Boolean(job)
                  || Boolean(preflightId)
                  || Boolean(preflightRun && (preflightRun.status === 'running' || preflightRun.status === 'completed'))}
                onClick={() => void runPreflight()}
              >
                Run preflight
              </button>
              <p className="disclosure">During preflight and the job, PICASSO temporarily owns this USB exclusively. It is unavailable in Ubuntu Files, and access returns after completion or cancellation. On a local desktop, open Files windows may close automatically. If this USB is busy, save your work and close Files windows showing this USB yourself, then click Run preflight to retry. This applies to local and remote desktops.</p>
            </div>
          )}
        </section>

        <section className="panel" aria-labelledby="job-title">
          <div className="panel__heading">
            <div>
              <p className="step">02</p>
              <h2 id="job-title">Preflight & job</h2>
            </div>
          </div>

          {!preflight && !preflightRun && !job && <p className="empty">Choose a source folder to review its import requirements.</p>}

          {preflightRun && preflightRun.status === 'running' && !job && (
            <div className="preflight" aria-live="polite">
              <h3>{preflightRun.stage === 'measuring' ? 'Measuring source files…' : 'Scanning files…'}</h3>
              <progress
                max={preflightRun.total ?? undefined}
                value={preflightRun.total === null ? undefined : preflightRun.processed}
              />
              <div className="progress-copy">
                <strong>{preflightRun.percentage === null ? '—' : `${preflightRun.percentage.toFixed(1)}%`}</strong>
                <span>{preflightRun.processed.toLocaleString()} / {preflightRun.total === null ? '…' : preflightRun.total.toLocaleString()} files</span>
              </div>
              <p>Estimated completion: {formatEta(preflightRun.eta_seconds, '')}</p>
              <button className="button button--quiet" type="button" onClick={() => void cancelPreflight()}>
                Cancel preflight
              </button>
            </div>
          )}

          {preflight && !job && (
            <div className="preflight">
              <dl className="metrics">
                <div><dt>Images</dt><dd>{preflight.file_count.toLocaleString()}</dd></div>
                <div><dt>Source size</dt><dd>{formatBytes(preflight.source_bytes)}</dd></div>
                <div><dt>Required locally</dt><dd>{formatBytes(preflight.required_free_bytes)}</dd></div>
                <div><dt>Local free</dt><dd>{formatBytes(preflight.local_free_bytes)}</dd></div>
              </dl>
              <p className={preflight.has_enough_space ? 'good' : 'alert'}>
                {preflight.has_enough_space ? 'Storage check passed.' : 'Not enough local storage.'}
              </p>
              <button
                className="button button--primary"
                type="button"
                disabled={!sessionReady || busy || !preflight.has_enough_space || preflight.file_count === 0}
                onClick={() => void startJob()}
              >
                Confirm and start
              </button>
            </div>
          )}

          {job && (
            <div className="job" aria-live="polite">
              <div className="job__state">
                <span className={`status status--${job.state.toLowerCase()}`}>{job.state.replaceAll('_', ' ')}</span>
                <code>{job.job_id.slice(0, 8)}</code>
              </div>
              <p>{stateMessage(job)}</p>
              {job.failure && job.failure.relative_paths.length > 0 && (
                <div className="threats">
                  <strong>Affected paths</strong>
                  <ul>{job.failure.relative_paths.map((path) => <li key={path}><code>{path}</code></li>)}</ul>
                </div>
              )}
              <progress max="100" value={job.progress.percentage}>{job.progress.percentage}%</progress>
              <div className="progress-copy">
                <strong>{job.progress.percentage.toFixed(1)}%</strong>
                <span>{job.progress.progress_unit === 'bytes'
                  ? `${job.progress.processed.toLocaleString()} ${job.state === 'SCANNING' ? 'files' : 'images'} · ${(job.progress.processed_bytes ?? 0).toLocaleString()} / ${(job.progress.total_bytes ?? 0).toLocaleString()} bytes`
                  : `${job.progress.processed.toLocaleString()} / ${job.progress.total.toLocaleString()} images`}</span>
              </div>
              <dl className="metrics metrics--compact">
                <div><dt>Stage</dt><dd>{job.stage.replaceAll('_', ' ')}</dd></div>
                <div><dt>Speed</dt><dd>{job.progress.progress_unit === 'bytes'
                  ? `${(job.progress.throughput_bytes_per_second ?? 0).toFixed(1)} bytes/s`
                  : job.state === 'SCANNING' ? 'Calculating…'
                  : `${job.progress.images_per_second.toFixed(1)} images/s`}</dd></div>
                <div><dt>This stage</dt><dd>{formatEta(job.progress.stage_eta_seconds, job.state)}</dd></div>
                <div><dt>Estimated completion</dt><dd>{formatEta(job.progress.job_eta_seconds, job.state)}</dd></div>
                <div><dt>Rejected</dt><dd>{job.counts.rejected.toLocaleString()}</dd></div>
              </dl>
              {summary && (
                <div className="result">
                  <h3>Export complete</h3>
                  <p>{summary.counts.predicted.toLocaleString()} predictions and {summary.counts.rejected.toLocaleString()} rejections exported.</p>
                  <p className="archive-path">Local archive: <code>{summary.archive_path}</code></p>
                  <p className="note">USB is available in Ubuntu Files. Use Ubuntu's eject action before unplugging it.</p>
                  <p className="note">Scores are uncalibrated softmax model scores, not correctness probabilities.</p>
                </div>
              )}
              {terminalStates.has(job.state) && (
                <button className="button button--quiet" type="button" onClick={() => {
                  setJob(null)
                  setSummary(null)
                  setPreflight(null)
                  setPreflightRun(null)
                  setPreflightId(null)
                }}>
                  Prepare another job
                </button>
              )}
            </div>
          )}
        </section>
      </div>
    </main>
  )
}

export default App
