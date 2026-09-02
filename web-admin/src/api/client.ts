// Minimal API client: cookie session, Idempotency-Key on writes, Problem Details errors.

export interface Problem {
  type: string
  title: string
  status: number
  detail: string
  code: string
  [key: string]: unknown
}

export class ApiError extends Error {
  readonly problem: Problem
  constructor(problem: Problem) {
    super(`${problem.code}: ${problem.detail}`)
    this.problem = problem
  }
}

function idempotencyKey(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body !== undefined) headers.set('Content-Type', 'application/json')
  const method = (init.method ?? 'GET').toUpperCase()
  if (method !== 'GET' && !headers.has('Idempotency-Key')) headers.set('Idempotency-Key', idempotencyKey())
  const response = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  if (response.status === 204) return undefined as T
  const text = await response.text()
  const body = text ? JSON.parse(text) : null
  if (!response.ok) {
    const problem: Problem = body && body.code ? body : {
      type: 'about:blank', title: response.statusText, status: response.status,
      detail: text || response.statusText, code: `HTTP_${response.status}`,
    }
    throw new ApiError(problem)
  }
  return body as T
}

export const get = <T>(path: string) => api<T>(path)
export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
export const put = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) })
export const patch = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: 'PATCH', body: JSON.stringify(body ?? {}) })
export const del = <T>(path: string) => api<T>(path, { method: 'DELETE' })
