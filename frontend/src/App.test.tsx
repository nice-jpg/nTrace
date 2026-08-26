import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import App, { UserInputsSection } from './App'
import type { TraceDetail, TraceEvent, TraceSpan, TraceSummary } from './types'

vi.mock('./api', () => ({
  deleteTrace: vi.fn(),
  fetchSpan: vi.fn(),
  fetchTrace: vi.fn(),
  fetchTraces: vi.fn(),
  fetchUserInputs: vi.fn(),
  openTraceStream: vi.fn(() => () => undefined),
}))

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', ResizeObserverMock)

function timelineSpan({
  spanId, agentId, parentAgentId, parentSpanId, agentName, activationOrder, second,
}: {
  spanId: number
  agentId: number
  parentAgentId: number | null
  parentSpanId: number | null
  agentName: string
  activationOrder: number
  second: number
}): TraceSpan {
  const timestamp = `2026-01-01T00:00:${String(second).padStart(2, '0')}.000Z`
  const start: TraceEvent = {
    schema_version: 1,
    trace_id: 1,
    span_id: spanId,
    parent_span_id: parentSpanId,
    agent_id: agentId,
    parent_agent_id: parentAgentId,
    agent_name: agentName,
    activation_order: activationOrder,
    sender: 'host',
    type: 'start',
    timestamp,
    system_prompt: null,
    user_inputs: [],
    output: null,
    tools: [],
    tools_called: [],
    tool_call_results: [],
    token_usage: {},
    data: {},
  }
  return {
    ...start,
    type: 'span',
    started_at: timestamp,
    ended_at: timestamp,
    duration_ms: 0,
    running: false,
    start_event: start,
    end_event: null,
  }
}

function llmTimelineSpan(base: TraceSpan, tokenUsage: Record<string, unknown>): TraceSpan {
  return {
    ...base,
    sender: 'llm',
    token_usage: tokenUsage,
    start_event: base.start_event ? { ...base.start_event, sender: 'llm', token_usage: tokenUsage } : null,
  }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('trace input details', () => {
  it('labels and formats message, reasoning, and function call inputs', () => {
    render(<UserInputsSection defaultOpen inputs={[
      { type: 'message', role: 'human', content: 'hello\nworld' },
      { type: 'reasoning', summary: [{ type: 'summary_text', text: 'Inspect the page' }] },
      { type: 'function_call', name: 'search', arguments: { query: 'shop' } },
    ]} />)

    expect(screen.getByText('message · human')).toBeInTheDocument()
    expect(screen.getByText(/hello\s+world/)).toBeInTheDocument()
    expect(screen.getByText('reasoning')).toBeInTheDocument()
    expect(screen.getByText('Inspect the page')).toBeInTheDocument()
    expect(screen.getByText('function_call')).toBeInTheDocument()
    expect(screen.getByText('search')).toBeInTheDocument()
    expect(screen.getByText(/"query": "shop"/)).toBeInTheDocument()
  })

  it('places the expanded detail drawer after the timeline so it consumes workspace height', async () => {
    const startEvent: TraceEvent = {
      schema_version: 1,
      trace_id: 1,
      span_id: 2,
      parent_span_id: null,
      agent_id: 1,
      parent_agent_id: null,
      agent_name: 'main',
      activation_order: 1,
      sender: 'host',
      type: 'start',
      timestamp: '2026-01-01T00:00:00.000Z',
      system_prompt: 'system',
      user_inputs: ['hello'],
      output: null,
      tools: [],
      tools_called: [],
      tool_call_results: [],
      token_usage: {},
      data: {},
    }
    const span: TraceSpan = {
      ...startEvent,
      type: 'span',
      started_at: startEvent.timestamp,
      ended_at: null,
      duration_ms: null,
      running: true,
      start_event: startEvent,
      end_event: null,
    }
    const summary: TraceSummary = {
      trace_id: 1,
      started_at: startEvent.timestamp,
      updated_at: startEvent.timestamp,
      status: 'running',
      agent_count: 1,
      span_count: 1,
    }
    const detail: TraceDetail = {
      ...summary,
      agents: [{
        agent_id: 1,
        parent_agent_id: null,
        agent_name: 'main',
        activation_order: 1,
        first_seen_at: startEvent.timestamp,
      }],
      events: [startEvent],
      spans: [span],
    }
    vi.mocked(api.fetchTraces).mockResolvedValue([summary])
    vi.mocked(api.fetchTrace).mockResolvedValue(detail)
    vi.mocked(api.fetchSpan).mockResolvedValue(span)
    vi.mocked(api.fetchUserInputs)
      .mockResolvedValueOnce({
        items: Array.from({ length: 10 }, (_, index) => `input-${12 - index}`),
        offset: 0,
        limit: 10,
        total: 12,
        has_more: true,
      })
      .mockResolvedValueOnce({
        items: ['input-2', 'input-1'],
        offset: 10,
        limit: 10,
        total: 12,
        has_more: false,
      })
    vi.mocked(api.deleteTrace).mockResolvedValue()

    const { container } = render(<App />)
    expect(api.fetchSpan).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByTitle(/HOST/))

    await waitFor(() => expect(container.querySelector('.detail-drawer')).toBeInTheDocument())
    expect(api.fetchSpan).not.toHaveBeenCalled()
    expect(api.fetchUserInputs).not.toHaveBeenCalled()
    expect([...container.querySelectorAll('.drawer-grid details')].every(
      (section) => !(section as HTMLDetailsElement).open,
    )).toBe(true)

    fireEvent.click(screen.getByText('System prompt'))
    await waitFor(() => expect(api.fetchSpan).toHaveBeenCalledWith(1, 2))
    expect(api.fetchUserInputs).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText(/^User inputs/))
    await waitFor(() => expect(api.fetchUserInputs).toHaveBeenCalledWith(1, 2, 0, 10))
    expect(await screen.findByText('Input 12')).toBeInTheDocument()
    const inputList = container.querySelector('.user-input-list.lazy') as HTMLDivElement
    Object.defineProperties(inputList, {
      scrollHeight: { configurable: true, value: 500 },
      clientHeight: { configurable: true, value: 200 },
      scrollTop: { configurable: true, value: 270 },
    })
    fireEvent.scroll(inputList)
    await waitFor(() => expect(api.fetchUserInputs).toHaveBeenCalledWith(1, 2, 10, 10))
    expect(await screen.findByText('Input 01')).toBeInTheDocument()

    const workspace = container.querySelector('.workspace')
    const drawer = container.querySelector('.detail-drawer')
    expect(workspace).toHaveClass('detail-open')
    expect(drawer?.parentElement).toBe(workspace)
    expect(drawer?.previousElementSibling).toHaveClass('trace-main-row')
    expect(drawer?.previousElementSibling?.querySelector('.timeline-card')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Close details' }))
    expect(container.querySelector('.detail-drawer')).not.toBeInTheDocument()
    expect(workspace).not.toHaveClass('detail-open')

    fireEvent.click(screen.getByRole('button', { name: 'Collapse agent main' }))
    expect(container.querySelector('.agent-row')).toHaveClass('collapsed')
    expect(screen.getByRole('button', { name: 'Expand agent main' })).toBeInTheDocument()

    fireEvent.contextMenu(screen.getByRole('button', { name: /#1running/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete trace #1' }))
    await waitFor(() => expect(api.deleteTrace).toHaveBeenCalledWith(1))
  })

  it('reuses the three most recently visited trace pages and reloads an evicted page', async () => {
    const summaries: TraceSummary[] = [1, 2, 3, 4].map((traceId) => ({
      trace_id: traceId,
      started_at: `2026-01-01T00:00:0${traceId}.000Z`,
      updated_at: `2026-01-01T00:00:0${traceId}.000Z`,
      status: 'completed',
      agent_count: 0,
      span_count: 0,
    }))
    vi.mocked(api.fetchTraces).mockResolvedValue(summaries)
    vi.mocked(api.fetchTrace).mockImplementation(async (traceId) => ({
      ...summaries.find((trace) => trace.trace_id === traceId)!,
      agents: [],
      events: [],
      spans: [],
    }))

    render(<App />)
    await screen.findByRole('heading', { name: 'Trace 1' })

    const selectTrace = async (traceId: number) => {
      fireEvent.click(screen.getByRole('button', { name: new RegExp(`#${traceId}completed`, 'i') }))
      await screen.findByRole('heading', { name: `Trace ${traceId}` })
    }
    await selectTrace(2)
    await selectTrace(3)
    await selectTrace(1)
    await selectTrace(4)
    await selectTrace(1)

    expect(vi.mocked(api.fetchTrace).mock.calls.filter(([traceId]) => traceId === 1)).toHaveLength(1)

    await selectTrace(2)
    expect(vi.mocked(api.fetchTrace).mock.calls.filter(([traceId]) => traceId === 2)).toHaveLength(2)
  })

  it('keeps nested parent connectors visible when a derived agent is collapsed', async () => {
    const spans = [
      timelineSpan({ spanId: 10, agentId: 1, parentAgentId: null, parentSpanId: null, agentName: 'root', activationOrder: 1, second: 0 }),
      timelineSpan({ spanId: 20, agentId: 2, parentAgentId: 1, parentSpanId: 10, agentName: 'child', activationOrder: 2, second: 1 }),
      timelineSpan({ spanId: 30, agentId: 3, parentAgentId: 2, parentSpanId: 20, agentName: 'grandchild', activationOrder: 3, second: 2 }),
    ]
    const summary: TraceSummary = {
      trace_id: 1,
      started_at: spans[0].started_at,
      updated_at: spans[2].started_at,
      status: 'completed',
      agent_count: 3,
      span_count: 3,
    }
    vi.mocked(api.fetchTraces).mockResolvedValue([summary])
    vi.mocked(api.fetchTrace).mockResolvedValue({
      ...summary,
      agents: spans.map((span) => ({
        agent_id: span.agent_id,
        parent_agent_id: span.parent_agent_id,
        agent_name: span.agent_name,
        activation_order: span.activation_order,
        first_seen_at: span.started_at,
      })),
      events: spans.map((span) => span.start_event!),
      spans,
    })

    const { container } = render(<App />)
    await screen.findByRole('heading', { name: 'Trace 1' })
    expect(container.querySelectorAll('.connector-layer path')).toHaveLength(2)
    const before = [...container.querySelectorAll('.connector-layer path')].map((path) => path.getAttribute('d'))

    fireEvent.click(screen.getByRole('button', { name: 'Collapse agent child' }))

    expect(container.querySelectorAll('.connector-layer path')).toHaveLength(2)
    const after = [...container.querySelectorAll('.connector-layer path')].map((path) => path.getAttribute('d'))
    expect(after).not.toEqual(before)
  })

  it('opens agent token charts and jumps from chart points to span details', async () => {
    const rootHost = timelineSpan({ spanId: 10, agentId: 1, parentAgentId: null, parentSpanId: null, agentName: 'root', activationOrder: 1, second: 0 })
    const rootLlm = llmTimelineSpan(
      timelineSpan({ spanId: 11, agentId: 1, parentAgentId: null, parentSpanId: 10, agentName: 'root', activationOrder: 1, second: 1 }),
      { input_tokens: 10, output_tokens: 2 },
    )
    const childHost = timelineSpan({ spanId: 20, agentId: 2, parentAgentId: 1, parentSpanId: 10, agentName: 'collector', activationOrder: 2, second: 2 })
    const childLlm = llmTimelineSpan(
      timelineSpan({ spanId: 21, agentId: 2, parentAgentId: 1, parentSpanId: 20, agentName: 'collector', activationOrder: 2, second: 3 }),
      { input_tokens: 20, output_tokens: 3, input_token_details: { cached_tokens: 5 } },
    )
    const spans = [rootHost, rootLlm, childHost, childLlm]
    const summary: TraceSummary = {
      trace_id: 1,
      started_at: rootHost.started_at,
      updated_at: childLlm.started_at,
      status: 'completed',
      agent_count: 2,
      span_count: spans.length,
    }
    vi.mocked(api.fetchTraces).mockResolvedValue([summary])
    vi.mocked(api.fetchTrace).mockResolvedValue({
      ...summary,
      agents: [
        { agent_id: 1, parent_agent_id: null, agent_name: 'root', activation_order: 1, first_seen_at: rootHost.started_at },
        { agent_id: 2, parent_agent_id: 1, agent_name: 'collector', activation_order: 2, first_seen_at: childHost.started_at },
      ],
      events: spans.map((span) => span.start_event!),
      spans,
    })

    const { container } = render(<App />)
    await screen.findByRole('heading', { name: 'Trace 1' })
    fireEvent.click(screen.getByRole('button', { name: 'Show token statistics for agent root' }))

    const panel = screen.getByLabelText('Token statistics for agent root')
    expect(panel).toBeInTheDocument()
    expect(screen.getByText('LLM call token cost')).toBeInTheDocument()
    expect(screen.getByText('Subagent call token cost')).toBeInTheDocument()
    expect(panel.parentElement).toHaveClass('trace-main-row')

    const llmPoint = screen.getByRole('button', { name: /LLM call token cost point 1/ })
    fireEvent.mouseEnter(llmPoint)
    expect(screen.getByText('Weighted cost')).toBeInTheDocument()
    fireEvent.click(llmPoint)
    expect(container.querySelector('.detail-drawer')).toBeInTheDocument()
    expect(container.querySelector('[data-span-id="11"]')).toHaveClass('selected')

    const subagentPoint = screen.getByRole('button', { name: /Subagent call token cost point 1/ })
    fireEvent.click(subagentPoint)
    expect(container.querySelector('[data-span-id="20"]')).toHaveClass('selected')
    expect(container.querySelector('.detail-drawer h2')).toHaveTextContent('collector')
    expect(container.querySelector('.detail-drawer')?.previousElementSibling).toHaveClass('trace-main-row')
  })
})
