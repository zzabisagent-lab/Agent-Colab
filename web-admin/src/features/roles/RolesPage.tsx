import { type FormEvent, useCallback, useEffect, useState } from 'react'
import { ApiError, get, post } from '../../api/client'

interface RoleVersion { version: number; permissions: string[]; deny: string[] }
interface RoleView { role_id: string; display_name: string; current_version: number; status: string; versions?: RoleVersion[] }
interface EffectiveRole { role_id: string; version: number; permissions: string[]; deny: string[] }
interface Effective {
  account_id: string
  roles: EffectiveRole[]
  effective_permissions?: string[]
  effective_deny?: string[]
  allow?: string[]
  deny?: string[]
}
const latest = (r: RoleView): RoleVersion | undefined =>
  (r.versions ?? []).find((v) => v.version === r.current_version) ?? (r.versions ?? []).at(-1)
const base = '/api/v1/roles'

export function RolesPage() {
  const [roles, setRoles] = useState<RoleView[]>([])
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [roleId, setRoleId] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [permissions, setPermissions] = useState('')
  const [deny, setDeny] = useState('')
  const [assignAccount, setAssignAccount] = useState('')
  const [assignRole, setAssignRole] = useState('')
  const [preview, setPreview] = useState<Effective | null>(null)

  const load = useCallback(async () => {
    try {
      setRoles((await get<{ items: RoleView[] }>(base)).items)
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
  const split = (s: string) => s.split(',').map((x) => x.trim()).filter(Boolean)
  function create(e: FormEvent) {
    e.preventDefault()
    void run(
      () => post(base, { role_id: roleId, display_name: displayName, permissions: split(permissions), deny: split(deny) }),
      'ROLE_COMMITTED',
    )
  }
  function assign(e: FormEvent) {
    e.preventDefault()
    void run(() => post(`${base}/${assignRole}/assign`, { account_id: assignAccount }), 'ROLE_ASSIGNED')
  }
  async function showEffective(account: string) {
    setError(null)
    try {
      setPreview(await get<Effective>(`${base}/effective?account_id=${encodeURIComponent(account)}`))
    } catch (e) {
      setError(e instanceof ApiError ? e.problem.code : String(e))
    }
  }
  return (
    <section>
      <h1>Roles</h1>
      {error && <p role="alert" className="error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <form onSubmit={create} aria-labelledby="create-role">
        <h2 id="create-role">Create or version a Role</h2>
        <label htmlFor="role-id">Role id</label>
        <input id="role-id" value={roleId} onChange={(e) => setRoleId(e.target.value)} required />
        <label htmlFor="role-name">Display name</label>
        <input id="role-name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} required />
        <label htmlFor="role-permissions">Permissions (comma separated)</label>
        <input id="role-permissions" value={permissions} onChange={(e) => setPermissions(e.target.value)} />
        <label htmlFor="role-deny">Explicit deny (comma separated)</label>
        <input id="role-deny" value={deny} onChange={(e) => setDeny(e.target.value)} />
        <button type="submit">Commit version</button>
      </form>
      <form onSubmit={assign} aria-labelledby="assign-role">
        <h2 id="assign-role">Assign a Role</h2>
        <label htmlFor="assign-account">Account id</label>
        <input id="assign-account" value={assignAccount} onChange={(e) => setAssignAccount(e.target.value)} required />
        <label htmlFor="assign-role-id">Role id to assign</label>
        <input id="assign-role-id" value={assignRole} onChange={(e) => setAssignRole(e.target.value)} required />
        <button type="submit">Assign</button>
        <button type="button" onClick={() => void showEffective(assignAccount)}>Preview effective permissions</button>
      </form>
      {preview && (
        <section aria-label="Effective permissions">
          <h2>Effective permissions of <code>{preview.account_id}</code></h2>
          <p>Roles: {(preview.roles ?? []).map((r) => `${r.role_id}@${r.version}`).join(', ') || '—'}</p>
          <p>Allow: {(preview.effective_permissions ?? preview.allow ?? (preview.roles ?? []).flatMap((r) => r.permissions)).join(', ') || '—'}</p>
          <p>Deny: {(preview.effective_deny ?? preview.deny ?? (preview.roles ?? []).flatMap((r) => r.deny)).join(', ') || '—'}</p>
        </section>
      )}
      <table>
        <caption>Roles</caption>
        <thead><tr><th>Role</th><th>Name</th><th>Version</th><th>Permissions</th><th>Deny</th></tr></thead>
        <tbody>
          {roles.map((r) => (
            <tr key={r.role_id}>
              <td><code>{r.role_id}</code></td><td>{r.display_name}</td><td>{r.current_version}</td>
              <td>{(latest(r)?.permissions ?? []).join(', ')}</td><td>{(latest(r)?.deny ?? []).join(', ') || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
