import { useCallback, useEffect, useState } from 'react'
import { ApiError, get } from '../api/client'

/** Load a JSON resource once and on demand; errors surface as stable codes for the page alert. */
export function useResource<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const reload = useCallback(async () => {
    if (!path) return
    try {
      const next = await get<T>(path)
      setData(() => next)
      setError(() => null)
    } catch (e) {
      setError(() => (e instanceof ApiError ? e.problem.code : String(e)))
    }
  }, [path])
  useEffect(() => {
    let active = true
    if (!path) return
    get<T>(path)
      .then((next) => { if (active) { setData(next); setError(null) } })
      .catch((e) => { if (active) setError(e instanceof ApiError ? e.problem.code : String(e)) })
    return () => { active = false }
  }, [path])
  return { data, error, reload, setError }
}

export function codeOf(e: unknown): string {
  return e instanceof ApiError ? e.problem.code : String(e)
}
