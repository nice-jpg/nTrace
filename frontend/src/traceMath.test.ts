import { describe, expect, it } from 'vitest'
import {
  assembleSpans,
  centeredZoomScrollLeft,
  childConnectorSpans,
  clampDrawerHeight,
  decodeEscapedText,
  formatTickLabel,
  latestEventTime,
  layoutSpan,
  tokenColor,
  tokenCostBreakdown,
  upsertEvent,
} from './traceMath'
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

  it('freezes the timeline at the latest event until another event arrives', () => {
    const start = event('start')
    expect(latestEventTime([start], Date.parse(start.timestamp))).toBe(Date.parse(start.timestamp))

    const later = event('start', { span_id: 3, timestamp: '2026-01-01T00:00:25.000Z' })
    expect(latestEventTime([start, later], Date.parse(start.timestamp))).toBe(Date.parse(later.timestamp))
  })

  it('uses the next event time to reveal idle spacing for a running span', () => {
    const span = assembleSpans([event('start')])[0]
    const first = layoutSpan(span, Date.parse(span.started_at), Date.parse(span.started_at), 1, 1_000, false)
    const afterEvent = layoutSpan(span, Date.parse(span.started_at), Date.parse(span.started_at) + 400, 1, 1_000, false)
    expect(first.naturalWidth).toBe(18)
    expect(afterEvent.naturalWidth).toBe(400)
  })

  it('keeps the same timeline point under the viewport center while zooming', () => {
    expect(centeredZoomScrollLeft({
      scrollLeft: 300,
      clientWidth: 800,
      labelWidth: 188,
      oldPixelsPerMs: 1,
      newPixelsPerMs: 2,
      durationMs: 2_000,
      newStageWidth: 4_188,
    })).toBe(812)
  })

  it('clamps the detail drawer between its minimum and the visible viewport', () => {
    expect(clampDrawerHeight(80, 800)).toBe(190)
    expect(clampDrawerHeight(900, 800)).toBe(728)
    expect(clampDrawerHeight(420, 800)).toBe(420)
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
    expect(tokenColor(1_000)).not.toBe(tokenColor(100_000))
    expect(tokenColor(100_000)).not.toBe(tokenColor(1_000_000))
    expect(tokenColor(null)).toContain('215')
  })

  it('spends most of the token color range below 300k weighted cost', () => {
    expect(tokenColor(30_000)).not.toBe(tokenColor(100_000))
    expect(tokenColor(100_000)).not.toBe(tokenColor(300_000))
    expect(tokenColor(300_000)).not.toBe(tokenColor(1_000_000))
  })

  it('calculates weighted token cost from output, cached, and uncached input tokens', () => {
    expect(tokenCostBreakdown({
      input_tokens: 2651,
      output_tokens: 178,
      details: { input_token_details: { cached_tokens: 1000 } },
    })).toEqual({
      inputTokens: 2651,
      uncachedInputTokens: 1651,
      outputTokens: 178,
      cachedTokens: 1000,
      weightedCost: 101350,
    })
  })

  it('draws only the first main-agent connector for each child agent', () => {
    const events = [
      event('start', { span_id: 10 }),
      event('end', { span_id: 10 }),
      event('start', {
        span_id: 20,
        agent_id: 2,
        parent_agent_id: 1,
        parent_span_id: 10,
        timestamp: '2026-01-01T00:00:01.000Z',
      }),
      event('end', {
        span_id: 20,
        agent_id: 2,
        parent_agent_id: 1,
        parent_span_id: 10,
        timestamp: '2026-01-01T00:00:02.000Z',
      }),
      event('start', {
        span_id: 21,
        agent_id: 2,
        parent_agent_id: 1,
        parent_span_id: 10,
        timestamp: '2026-01-01T00:00:03.000Z',
      }),
      event('start', {
        span_id: 30,
        agent_id: 3,
        parent_agent_id: 2,
        parent_span_id: 20,
        timestamp: '2026-01-01T00:00:04.000Z',
      }),
    ]
    expect(childConnectorSpans(assembleSpans(events)).map((span) => span.span_id)).toEqual([20])
  })

  it('renders escaped user input text as readable multiline content', () => {
    expect(decodeEscapedText('first\\nsecond\\tvalue')).toBe('first\nsecond\tvalue')
    expect(decodeEscapedText('unicode: \\u4e2d\\u6587')).toBe('unicode: 中文')
  })

  it('uses sub-second precision for short timeline tick labels', () => {
    expect(formatTickLabel(123.456, 800)).toBe('123.5ms')
    expect(formatTickLabel(1234, 8_000)).toBe('1.234s')
    expect(formatTickLabel(62_345, 120_000)).toBe('1:02.345')
    expect(formatTickLabel(65_400, 600_000)).toBe('1:05.400')
  })

  it('deduplicates live start or end events', () => {
    const updated = upsertEvent([event('start')], event('start', { user_inputs: ['updated'] }))
    expect(updated).toHaveLength(1)
    expect(updated[0].user_inputs).toEqual(['updated'])
  })
})
