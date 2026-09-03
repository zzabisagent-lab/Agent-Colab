import { type FormEvent, useState } from 'react'
import { post } from '../../api/client'
import { codeOf, useResource } from '../../lib/useList'

interface ScheduleView {
  schedule_id: string
  name: string
  status: string
  current_version?: number
  next_run_at?: string | null
  version?: { cron_expression: string; timezone: string; channel_id: string; concurrency_policy: string; missed_run_policy: string }
}
interface PreviewItem { local: string; utc: string; reason?: string | null; occurrence_key?: string }
interface RunView {
  run_id: string
  run_kind: string
  status: string
  scheduled_for?: string | null
  started_at?: string | null
  finished_at?: string | null
  error_code?: string | null
  task_id?: string | null
  retry_of_run_id?: string | null
  links?: Record<string, string | null>
}
const base = '/api/v1/schedules'
const FIELDS = ['minute', 'hour', 'day of month', 'month', 'day of week'] as const

/** Schedule builder, preview, lifecycle, run-now and Run history (P5-08, development plan §10A.5). */
export function SchedulesPage() {
  const { data, error, reload, setError } = useResource<{ items: ScheduleView[] }>(base)
  const [notice, setNotice] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [fields, setFields] = useState(['0', '9', '*', '*', '1-5'])
  const [raw, setRaw] = useState('')
  const [timezone, setTimezone] = useState('UTC')
  const [channelId, setChannelId] = useState('')
  const [principal, setPrincipal] = useState('')
  const [agentMode, setAgentMode] = useState<'fixed' | 'capability'>('capability')
  const [agentValue, setAgentValue] = useState('')
  const [title, setTitle] = useState('Scheduled task')
  const [domain, setDomain] = useState('general')
  const [concurrency, setConcurrency] = useState('FORBID')
  const [missed, setMissed] = useState('RUN_ONCE')
  const [maxDuration, setMaxDuration] = useState('3600')
  const [dailyBudget, setDailyBudget] = useState('1000000')
  const [runBudget, setRunBudget] = useState('100000')
  const [preview, setPreview] = useState<PreviewItem[] | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [runs, setRuns] = useState<RunView[]>([])
  const cron = raw.trim() || fields.join(' ')
  const version = () => ({
    cron_expression: cron,
    timezone,
    channel_id: channelId,
    execution_principal_id: principal,
    agent_selection: agentMode === 'fixed'
      ? { mode: 'fixed', agent_id: agentValue }
      : { mode: 'capability', required_capabilities: [agentValue] },
    action_template: {
      schema_id: 'action-template.v1',
      action: 'task_create',
      input: { title, domain, risk: 'LOW' },
    },
    concurrency_policy: concurrency,
    missed_run_policy: missed,
    max_duration_seconds: Number(maxDuration),
    budget_policy: { per_run_cost_units: Number(runBudget), daily_cost_units: Number(dailyBudget) },
  })
  async function run(action: () => Promise<unknown>, ok: string) {
    setError(null); setNotice(null)
    try { await action(); setNotice(ok); await reload(); if (selected) await loadRuns(selected) } catch (e) { setError(codeOf(e)) }
  }
  async function doPreview() {
    setError(null)
    try {
      const body = { cron_expression: cron, timezone, count: 10 }
      setPreview((await post<{ items: PreviewItem[] }>(`${base}/preview`, body)).items)
    } catch (e) { setError(codeOf(e)) }
  }
  function create(e: FormEvent) {
    e.preventDefault()
    void run(() => post(base, { name, ...version() }), 'SCHEDULE_CREATED')
  }
  async function loadRuns(id: string) {
    setSelected(id)
    try { setRuns((await (await fetch(`${base}/${id}/runs?limit=50`, { credentials: 'same-origin' })).json()).items ?? []) } catch (e) { setError(codeOf(e)) }
  }
  return (
    <section>
      <h1>Schedules</h1>
      {error && <p role="alert" className="error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <form onSubmit={create} aria-labelledby="schedule-builder">
        <h2 id="schedule-builder">Schedule builder</h2>
        <label htmlFor="sch-name">Name</label>
        <input id="sch-name" value={name} onChange={(e) => setName(e.target.value)} required />
        <fieldset><legend>Cron (five numeric fields; day-of-month and day-of-week combine with OR)</legend>
          {FIELDS.map((f, i) => (
            <span key={f}>
              <label htmlFor={`cron-${i}`}>{f}</label>
              <input id={`cron-${i}`} value={fields[i]} onChange={(e) => setFields(fields.map((v, j) => (j === i ? e.target.value : v)))} />
            </span>
          ))}
          <label htmlFor="cron-raw">Raw cron expression (overrides the fields)</label>
          <input id="cron-raw" value={raw} onChange={(e) => setRaw(e.target.value)} placeholder="0 9 * * 1-5" />
          <label htmlFor="sch-tz">IANA timezone</label>
          <input id="sch-tz" value={timezone} onChange={(e) => setTimezone(e.target.value)} required />
          <button type="button" onClick={() => void doPreview()}>Preview next 10 runs</button>
        </fieldset>
        <fieldset><legend>Execution</legend>
          <label htmlFor="sch-channel">Channel id</label>
          <input id="sch-channel" value={channelId} onChange={(e) => setChannelId(e.target.value)} required />
          <label htmlFor="sch-principal">Execution principal (account id)</label>
          <input id="sch-principal" value={principal} onChange={(e) => setPrincipal(e.target.value)} required />
          <label htmlFor="sch-agent-mode">Agent selection</label>
          <select id="sch-agent-mode" value={agentMode} onChange={(e) => setAgentMode(e.target.value as 'fixed' | 'capability')}>
            <option value="capability">by capability</option><option value="fixed">fixed agent</option>
          </select>
          <label htmlFor="sch-agent-value">{agentMode === 'fixed' ? 'Agent id' : 'Capability id'}</label>
          <input id="sch-agent-value" value={agentValue} onChange={(e) => setAgentValue(e.target.value)} required />
          <label htmlFor="sch-title">Task title template</label>
          <input id="sch-title" value={title} onChange={(e) => setTitle(e.target.value)} required />
          <label htmlFor="sch-domain">Task domain</label>
          <input id="sch-domain" value={domain} onChange={(e) => setDomain(e.target.value)} required />
        </fieldset>
        <fieldset><legend>Policies</legend>
          <label htmlFor="sch-concurrency">Concurrency</label>
          <select id="sch-concurrency" value={concurrency} onChange={(e) => setConcurrency(e.target.value)}>
            <option>FORBID</option><option>ALLOW</option><option>REPLACE</option>
          </select>
          <label htmlFor="sch-missed">Missed runs</label>
          <select id="sch-missed" value={missed} onChange={(e) => setMissed(e.target.value)}>
            <option>SKIP</option><option>RUN_ONCE</option><option>BACKFILL_LIMITED</option>
          </select>
          <label htmlFor="sch-max">Max duration (seconds)</label>
          <input id="sch-max" inputMode="numeric" value={maxDuration} onChange={(e) => setMaxDuration(e.target.value)} />
          <label htmlFor="sch-run-budget">Per-Run budget (cost_units)</label>
          <input id="sch-run-budget" inputMode="numeric" value={runBudget} onChange={(e) => setRunBudget(e.target.value)} />
          <label htmlFor="sch-budget">Daily budget (cost_units)</label>
          <input id="sch-budget" inputMode="numeric" value={dailyBudget} onChange={(e) => setDailyBudget(e.target.value)} />
        </fieldset>
        <button type="submit">Create schedule (DRAFT)</button>
      </form>
      {preview && (
        <table>
          <caption>Next 10 occurrences</caption>
          <thead><tr><th>Local</th><th>UTC</th><th>Note</th></tr></thead>
          <tbody>{preview.map((p, i) => <tr key={i}><td>{p.local}</td><td>{p.utc}</td><td>{p.reason ?? '—'}</td></tr>)}</tbody>
        </table>
      )}
      <table>
        <caption>Schedules</caption>
        <thead><tr><th>Schedule</th><th>Name</th><th>Status</th><th>Cron</th><th>Next run</th><th>Actions</th></tr></thead>
        <tbody>
          {(data?.items ?? []).map((s) => (
            <tr key={s.schedule_id}>
              <td><code>{s.schedule_id}</code></td><td>{s.name}</td><td>{s.status}</td>
              <td>{s.version ? `${s.version.cron_expression} (${s.version.timezone})` : '—'}</td><td>{s.next_run_at ?? '—'}</td>
              <td>
                <button onClick={() => void run(() => post(`${base}/${s.schedule_id}/enable`, {}), 'SCHEDULE_ENABLED')}>Enable</button>
                <button onClick={() => void run(() => post(`${base}/${s.schedule_id}/pause`, {}), 'SCHEDULE_PAUSED')}>Pause</button>
                <button onClick={() => void run(() => post(`${base}/${s.schedule_id}/resume`, {}), 'SCHEDULE_RESUMED')}>Resume</button>
                <button onClick={() => { if (window.confirm(`Disable ${s.schedule_id}? Pending Runs are cancelled.`)) void run(() => post(`${base}/${s.schedule_id}/disable`, {}), 'SCHEDULE_DISABLED') }}>Disable</button>
                <button onClick={() => void run(() => post(`${base}/${s.schedule_id}/run-now`, {}), 'SCHEDULE_RUN_NOW')}>Run now</button>
                <button onClick={() => void loadRuns(s.schedule_id)}>History</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {selected && (
        <table>
          <caption>Runs of {selected}</caption>
          <thead><tr><th>Run</th><th>Kind</th><th>Status</th><th>Scheduled for</th><th>Started</th><th>Finished</th><th>Error</th><th>Links</th><th>Actions</th></tr></thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.run_id}>
                <td><code>{r.run_id}</code></td><td>{r.run_kind}{r.retry_of_run_id ? ` (retry of ${r.retry_of_run_id})` : ''}</td><td>{r.status}</td>
                <td>{r.scheduled_for ?? '—'}</td><td>{r.started_at ?? '—'}</td><td>{r.finished_at ?? '—'}</td><td>{r.error_code ?? '—'}</td>
                <td>{r.task_id ? `task ${r.task_id}` : '—'}{r.links ? Object.entries(r.links).filter(([, v]) => v).map(([k, v]) => ` · ${k} ${v}`).join('') : ''}</td>
                <td>
                  <button onClick={() => void run(() => post(`${base}/runs/${r.run_id}/cancel`, {}), 'RUN_CANCEL_REQUESTED')}>Cancel</button>
                  <button onClick={() => void run(() => post(`${base}/runs/${r.run_id}/retry`, {}), 'RUN_RETRIED')}>Retry</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
