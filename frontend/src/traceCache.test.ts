import { describe, expect, it } from 'vitest'
import { getCachedTrace, putCachedTrace } from './traceCache'
import type { TraceDetail } from './types'

function trace(traceId: number): TraceDetail {
  return {
    trace_id: traceId,
    started_at: '2026-01-01T00:00:00.000Z',
    updated_at: '2026-01-01T00:00:00.000Z',
    status: 'completed',
    agent_count: 0,
    span_count: 0,
    agents: [],
    events: [],
    spans: [],
  }
}

describe('trace page cache', () => {
  it('keeps the three most recently used trace pages', () => {
    const cache = new Map<number, TraceDetail>()
    putCachedTrace(cache, trace(1))
    putCachedTrace(cache, trace(2))
    putCachedTrace(cache, trace(3))
    expect(getCachedTrace(cache, 1)?.trace_id).toBe(1)

    expect(putCachedTrace(cache, trace(4))).toEqual([2])
    expect([...cache.keys()]).toEqual([3, 1, 4])
  })
})
