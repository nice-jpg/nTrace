import type { TraceDetail } from './types'

export const TRACE_CACHE_LIMIT = 3

export function getCachedTrace(cache: Map<number, TraceDetail>, traceId: number): TraceDetail | null {
  const trace = cache.get(traceId)
  if (!trace) return null
  cache.delete(traceId)
  cache.set(traceId, trace)
  return trace
}

export function putCachedTrace(
  cache: Map<number, TraceDetail>,
  trace: TraceDetail,
  limit = TRACE_CACHE_LIMIT,
): number[] {
  cache.delete(trace.trace_id)
  cache.set(trace.trace_id, trace)
  const evicted: number[] = []
  while (cache.size > limit) {
    const oldest = cache.keys().next().value
    if (oldest === undefined) break
    cache.delete(oldest)
    evicted.push(oldest)
  }
  return evicted
}
