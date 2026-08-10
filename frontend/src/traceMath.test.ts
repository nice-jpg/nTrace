import { describe, expect, it } from 'vitest'
import { assembleSpans, layoutSpan, tokenColor, upsertEvent } from './traceMath'
import type { TraceEvent } from './types'

function event(type: 'start' | 'end', overrides: Partial<TraceEvent> = {}): TraceEvent {
  return {
    schema_version: 1,
    trace_id: 1,
    span_id: 2,
    parent_span_id: null,
    agent_id: 1,
    parent_agent_id: null,
    agent_name: 'main',
    activation_order: 1,
    sender: 'host',
    type,
    timestamp: type === 'start' ? '2026-01-01T00:00:00.000Z' : '2026-01-01T00:00:10.000Z',
    system_prompt: 'system',
    user_inputs: ['hello'],
    output: null,
    tools: [],
    tools_called: [],
    tool_call_results: [],
    token_usage: {},
    data: {},
    ...overrides,
  }
}

describe('trace timeline math', () => {
  it('assembles start and end events into a duration span', () => {
    const spans = assembleSpans([event('end', { output: 'done' }), event('start')])
    expect(spans).toHaveLength(1)
    expect(spans[0].duration_ms).toBe(10_000)
    expect(spans[0].output).toBe('done')
    expect(spans[0].running).toBe(false)
  })

  it('keeps a start-only span running', () => {
    expect(assembleSpans([event('start')])[0].running).toBe(true)
  })

  it('caps blocks at one third unless a child is attached', () => {
    const span = assembleSpans([event('start'), event('end')])[0]
    const clipped = layoutSpan(span, Date.parse(span.started_at), Date.now(), 1, 900, false)
    const parent = layoutSpan(span, Date.parse(span.started_at), Date.now(), 1, 900, true)
    expect(clipped.width).toBe(300)
    expect(clipped.clipped).toBe(true)
    expect(parent.width).toBe(10_000)
    expect(parent.clipped).toBe(false)
  })

  it('darkens the token color as usage grows', () => {
    expect(tokenColor(10_000, 10_000)).not.toBe(tokenColor(1, 10_000))
    expect(tokenColor(null, 10_000)).toContain('215')
  })

  it('deduplicates live start or end events', () => {
    const updated = upsertEvent([event('start')], event('start', { user_inputs: ['updated'] }))
    expect(updated).toHaveLength(1)
    expect(updated[0].user_inputs).toEqual(['updated'])
  })
})
