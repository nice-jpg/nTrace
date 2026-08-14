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
  openTraceStream: vi.fn(() => () => undefined),
}))

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', ResizeObserverMock)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('trace input details', () => {
  it('labels and formats message, reasoning, and function call inputs', () => {
    render(<UserInputsSection inputs={[
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
    vi.mocked(api.deleteTrace).mockResolvedValue()

    const { container } = render(<App />)
    expect(api.fetchSpan).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByTitle(/HOST/))

    await waitFor(() => expect(container.querySelector('.detail-drawer')).toBeInTheDocument())
    await waitFor(() => expect(api.fetchSpan).toHaveBeenCalledWith(1, 2))
    const workspace = container.querySelector('.workspace')
    const drawer = container.querySelector('.detail-drawer')
    expect(workspace).toHaveClass('detail-open')
    expect(drawer?.parentElement).toBe(workspace)
    expect(drawer?.previousElementSibling).toHaveClass('timeline-card')

    fireEvent.click(screen.getByRole('button', { name: 'Close details' }))
    expect(container.querySelector('.detail-drawer')).not.toBeInTheDocument()
    expect(workspace).not.toHaveClass('detail-open')

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
})
