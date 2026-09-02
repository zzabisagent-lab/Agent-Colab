import { type FormEvent, useCallback, useEffect, useState } from 'react'
import { ApiError, get, patch, post } from '../../api/client'

export interface AgentView {
  agent_id: string
  display_name: string
  adapter_type: string
  status: string
  online: boolean
  capacity: number
  limits: Record<string, number>
  roles?: string[]
  channels?: string[]
}

const ADAPTER_TYPES = ['mcp', 'webhook', 'mattermost_bot']
const base = '/api/v1/agents'

export function AgentsPage() {
  const [agents, setAgents] = useState<AgentView[]>([])
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [agentId, setAgentId] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [adapterType, setAdapterType] = useState('mcp')
  const [endpointUrl, setEndpointUrl] = useState('')
  const [credentialRef, setCredentialRef] = useState('')
  const [roles, setRoles] = useState('')
  const [concurrent, setConcurrent] = useState('1')
  const [rate, setRate] = useState('60')
  const [dailyCost, setDailyCost] = useState('1000000')

  const load = useCallback(async () => {
    try {
      const data = await get<{ items: AgentView[] }>(base)
      setAgents(data.items)
    } catch (e) {
      setError(e instanceof ApiError ? e.problem.code : String(e))
    }
  }, [])
  useEffect(() => { void load() }, [load])

  async function run(action: () => Promise<unknown>, ok: string) {
    setError(null)
    setNotice(null)
    try {
      await action()
      setNotice(ok)
      await load()
    } catch (e) {
      setError(e instanceof ApiError ? e.problem.code : String(e))
    }
  }
  function create(e: FormEvent) {
    e.preventDefault()
    void run(
      () => post(base, {
        agent_id: agentId,
        display_name: displayName,
        adapter_type: adapterType,
        endpoint: endpointUrl ? { url: endpointUrl } : {},
        credential_ref: credentialRef || null,
        roles: roles.split(',').map((r) => r.trim()).filter(Boolean),
        limits: {
          concurrent_tasks: Number(concurrent),
          requests_per_minute: Number(rate),
          daily_cost_units: Number(dailyCost),
        },
      }),
      'AGENT_REGISTERED',
    )
  }
  function editLimits(a: AgentView) {
    const value = window.prompt('Concurrent Task limit', String(a.limits?.concurrent_tasks ?? 1))
    if (value === null) return
    void run(
      () => patch(`${base}/${a.agent_id}`, { limits: { ...a.limits, concurrent_tasks: Number(value) } }),
      'AGENT_UPDATED',
    )
  }
  return (
    <section>
      <h1>Agents</h1>
      {error && <p role="alert" className="error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <form onSubmit={create} aria-labelledby="add-agent">
        <h2 id="add-agent">Add Agent</h2>
        <label htmlFor="agent-id">Agent id</label>
        <input id="agent-id" value={agentId} onChange={(e) => setAgentId(e.target.value)} required />
        <label htmlFor="agent-name">Display name</label>
        <input id="agent-name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} required />
        <label htmlFor="adapter-type">Adapter type</label>
        <select id="adapter-type" value={adapterType} onChange={(e) => setAdapterType(e.target.value)}>
          {ADAPTER_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <label htmlFor="endpoint-url">Endpoint URL (webhook / bot), optional</label>
        <input id="endpoint-url" value={endpointUrl} onChange={(e) => setEndpointUrl(e.target.value)} />
        <label htmlFor="credential-ref">Credential reference (Secret Broker), optional</label>
        <input id="credential-ref" value={credentialRef} onChange={(e) => setCredentialRef(e.target.value)} />
        <label htmlFor="agent-roles">Roles (comma separated)</label>
        <input id="agent-roles" value={roles} onChange={(e) => setRoles(e.target.value)} />
        <label htmlFor="limit-concurrent">Concurrent Task limit</label>
        <input id="limit-concurrent" inputMode="numeric" value={concurrent} onChange={(e) => setConcurrent(e.target.value)} />
        <label htmlFor="limit-rate">Requests per minute</label>
        <input id="limit-rate" inputMode="numeric" value={rate} onChange={(e) => setRate(e.target.value)} />
        <label htmlFor="limit-cost">Daily cost_units</label>
        <input id="limit-cost" inputMode="numeric" value={dailyCost} onChange={(e) => setDailyCost(e.target.value)} />
        <button type="submit">Register</button>
      </form>
      <table>
        <caption>Registered Agents</caption>
        <thead>
          <tr><th>Agent</th><th>Name</th><th>Adapter</th><th>Status</th><th>Online</th><th>Limits</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {agents.map((a) => (
            <tr key={a.agent_id}>
              <td><code>{a.agent_id}</code></td><td>{a.display_name}</td><td>{a.adapter_type}</td>
              <td>{a.status}</td><td>{a.online ? 'online' : 'offline'}</td>
              <td>{a.limits?.concurrent_tasks ?? '—'} tasks · {a.limits?.requests_per_minute ?? '—'}/min</td>
              <td>
                <button onClick={() => void run(() => post(`${base}/${a.agent_id}/test-connection`), 'AGENT_CONNECTION_OK')}>Test connection</button>
                <button onClick={() => void run(() => post(`${base}/${a.agent_id}/activate`), 'AGENT_ACTIVATED')}>Activate</button>
                <button onClick={() => void run(() => post(`${base}/${a.agent_id}/suspend`), 'AGENT_SUSPENDED')}>Suspend</button>
                <button onClick={() => void run(() => post(`${base}/${a.agent_id}/revoke`), 'AGENT_REVOKED')}>Revoke</button>
                <button onClick={() => editLimits(a)}>Edit limits</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
