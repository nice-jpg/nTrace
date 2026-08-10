import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { fetchTrace, fetchTraces, openTraceStream } from './api'
import {
  AGENT_HEIGHT,
  LABEL_WIDTH,
  RULER_HEIGHT,
  assembleSpans,
  formatDuration,
  layoutSpan,
  tokenColor,
  tokenTotal,
  upsertEvent,
} from './traceMath'
import type { AgentSummary, TraceDetail, TraceEvent, TraceSpan, TraceSummary } from './types'

const Icon = ({ name }: { name: 'trace' | 'activity' | 'chevron' | 'copy' | 'close' }) => {
  const paths = {
    trace: <><path d="M4 18V9m5 9V5m5 13v-7m5 7V3" /><path d="M2 21h20" /></>,
    activity: <path d="M3 12h4l2.2-6 4.2 12 2.1-6H21" />,
    chevron: <path d="m9 18 6-6-6-6" />,
    copy: <><rect x="8" y="8" width="11" height="11" rx="2" /><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3" /></>,
    close: <path d="m6 6 12 12M18 6 6 18" />,
  }
  return <svg viewBox="0 0 24 24" aria-hidden="true">{paths[name]}</svg>
}

export default function App() {
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detail, setDetail] = useState<TraceDetail | null>(null)
  const [selectedSpan, setSelectedSpan] = useState<TraceSpan | null>(null)
  const [historyOpen, setHistoryOpen] = useState(true)
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState('')
  const selectedIdRef = useRef<number | null>(null)

  useEffect(() => { selectedIdRef.current = selectedId }, [selectedId])

  const refreshTraces = useCallback(async () => {
    try {
      const next = await fetchTraces()
      setTraces(next)
      setSelectedId((current) => current ?? next[0]?.trace_id ?? null)
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }, [])

  const refreshSelected = useCallback(async (traceId: number | null = selectedIdRef.current) => {
    if (traceId === null) return
    try {
      setDetail(await fetchTrace(traceId))
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }, [])

  useEffect(() => { void refreshTraces() }, [refreshTraces])
  useEffect(() => { void refreshSelected(selectedId) }, [selectedId, refreshSelected])

  useEffect(() => openTraceStream(
    (event) => {
      setConnected(true)
      setTraces((current) => updateTraceSummaries(current, event))
      if (selectedIdRef.current === null) setSelectedId(event.trace_id)
      setDetail((current) => {
        if (!current || current.trace_id !== event.trace_id) return current
        return mergeLiveEvent(current, event)
      })
    },
    () => {
      setConnected(true)
      void refreshTraces()
      void refreshSelected()
    },
  ), [refreshSelected, refreshTraces])

  return (
    <div className="app-shell">
      <nav className="app-nav">
        <div className="brand-mark"><Icon name="activity" /></div>
        <button className="nav-item active" aria-label="Trace">
          <Icon name="trace" />
          <span>Trace</span>
        </button>
        <div className="nav-spacer" />
        <div className={`connection-dot ${connected ? 'online' : ''}`} title={connected ? 'Live stream connected' : 'Reconnecting'} />
      </nav>

      <HistoryPanel
        open={historyOpen}
        traces={traces}
        selectedId={selectedId}
        onToggle={() => setHistoryOpen((value) => !value)}
        onSelect={(id) => { setSelectedId(id); setSelectedSpan(null) }}
      />

      <main className="workspace">
        <header className="topbar">
          <div>
            <span className="eyebrow">AGENT SMART TRACE</span>
            <h1>{detail ? `Trace ${shortId(detail.trace_id)}` : 'Trace timeline'}</h1>
          </div>
          {detail && (
            <div className="trace-meta">
              <Status status={detail.status} />
              <span>{detail.agents.length} agents</span>
              <span>{detail.spans.length} spans</span>
            </div>
          )}
        </header>

        {error && <div className="error-banner">{error}</div>}
        {!detail && !error && <EmptyState />}
        {detail && (
          <Timeline
            trace={detail}
            selectedSpanId={selectedSpan?.span_id ?? null}
            onSelectSpan={setSelectedSpan}
          />
        )}
      </main>

      {selectedSpan && <DetailDrawer span={selectedSpan} onClose={() => setSelectedSpan(null)} />}
    </div>
  )
}

function HistoryPanel({
  open, traces, selectedId, onToggle, onSelect,
}: {
  open: boolean
  traces: TraceSummary[]
  selectedId: number | null
  onToggle: () => void
  onSelect: (traceId: number) => void
}) {
  return (
    <aside className={`history-panel ${open ? 'open' : 'closed'}`}>
      <div className="history-heading">
        {open && <><span>Trace history</span><strong>{traces.length}</strong></>}
        <button onClick={onToggle} aria-label={open ? 'Collapse history' : 'Expand history'}>
          <Icon name="chevron" />
        </button>
      </div>
      {open && <div className="history-list">
        {traces.map((trace) => (
          <button
            key={trace.trace_id}
            className={`history-card ${selectedId === trace.trace_id ? 'selected' : ''}`}
            onClick={() => onSelect(trace.trace_id)}
          >
            <div className="history-card-top">
              <code>#{shortId(trace.trace_id)}</code>
              <Status status={trace.status} compact />
            </div>
            <time>{new Date(trace.started_at).toLocaleString()}</time>
            <div className="history-counts">
              <span>{trace.agent_count} agents</span>
              <span>{trace.span_count} spans</span>
            </div>
          </button>
        ))}
        {traces.length === 0 && <div className="history-empty">Waiting for the first trace…</div>}
      </div>}
    </aside>
  )
}

function Timeline({ trace, selectedSpanId, onSelectSpan }: {
  trace: TraceDetail
  selectedSpanId: number | null
  onSelectSpan: (span: TraceSpan) => void
}) {
  const viewportRef = useRef<HTMLDivElement>(null)
  const [viewportWidth, setViewportWidth] = useState(900)
  const [zoom, setZoom] = useState(1)
  const [follow, setFollow] = useState(true)
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    const element = viewportRef.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => setViewportWidth(entry.contentRect.width))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 500)
    return () => window.clearInterval(timer)
  }, [])

  const agents = useMemo(() => [...trace.agents].sort((a, b) => a.activation_order - b.activation_order), [trace.agents])
  const spans = trace.spans
  const startMs = Math.min(...spans.map((span) => Date.parse(span.started_at)), Date.parse(trace.started_at))
  const latestMs = Math.max(nowIfRunning(trace, now), ...spans.map((span) => span.ended_at ? Date.parse(span.ended_at) : now))
  const durationMs = Math.max(1_000, latestMs - startMs)
  const contentViewport = Math.max(320, viewportWidth - LABEL_WIDTH)
  const fitPixelsPerMs = contentViewport / durationMs
  const pixelsPerMs = Math.max(0.002, fitPixelsPerMs * zoom)
  const canvasWidth = Math.max(contentViewport, durationMs * pixelsPerMs + 80)
  const childParentIds = useMemo(() => new Set(
    spans.filter((span) => span.sender === 'host' && span.parent_span_id !== null).map((span) => span.parent_span_id!),
  ), [spans])
  const maxTokens = Math.max(0, ...spans.filter((span) => span.sender === 'llm').map((span) => tokenTotal(span) ?? 0))

  useEffect(() => {
    if (follow && viewportRef.current) viewportRef.current.scrollLeft = viewportRef.current.scrollWidth
  }, [follow, spans.length, now])

  const ticks = Array.from({ length: 9 }, (_, index) => ({
    left: (canvasWidth - 80) * index / 8,
    elapsed: durationMs * index / 8,
  }))

  return (
    <section className="timeline-card">
      <div className="timeline-toolbar">
        <div className="legend">
          <span><i className="legend-host" />Host runtime</span>
          <span><i className="legend-llm" />LLM call · depth = tokens</span>
        </div>
        <div className="timeline-actions">
          <button className={follow ? 'active' : ''} onClick={() => setFollow((value) => !value)}>Live follow</button>
          <button onClick={() => setZoom((value) => Math.max(0.5, value / 1.35))}>−</button>
          <span>{Math.round(zoom * 100)}%</span>
          <button onClick={() => setZoom((value) => Math.min(24, value * 1.35))}>+</button>
        </div>
      </div>
      <div
        className="timeline-viewport"
        ref={viewportRef}
        onScroll={() => {
          const element = viewportRef.current
          if (element && element.scrollWidth - element.scrollLeft - element.clientWidth > 48) setFollow(false)
        }}
      >
        <div className="timeline-stage" style={{ width: canvasWidth + LABEL_WIDTH, height: RULER_HEIGHT + agents.length * AGENT_HEIGHT }}>
          <div className="ruler-corner">AGENT / SOURCE</div>
          <div className="time-ruler" style={{ left: LABEL_WIDTH, width: canvasWidth }}>
            {ticks.map((tick) => <div className="tick" key={tick.left} style={{ left: tick.left }}><span>{formatTick(tick.elapsed)}</span></div>)}
          </div>
          <ConnectorLayer agents={agents} spans={spans} startMs={startMs} pixelsPerMs={pixelsPerMs} width={canvasWidth} />
          {agents.map((agent, agentIndex) => (
            <AgentRows
              key={agent.agent_id}
              agent={agent}
              index={agentIndex}
              spans={spans.filter((span) => span.agent_id === agent.agent_id)}
              startMs={startMs}
              now={now}
              pixelsPerMs={pixelsPerMs}
              canvasWidth={canvasWidth}
              viewportWidth={contentViewport}
              maxTokens={maxTokens}
              childParentIds={childParentIds}
              selectedSpanId={selectedSpanId}
              onSelectSpan={onSelectSpan}
            />
          ))}
        </div>
      </div>
    </section>
  )
}

function AgentRows({ agent, index, spans, startMs, now, pixelsPerMs, canvasWidth, viewportWidth, maxTokens, childParentIds, selectedSpanId, onSelectSpan }: {
  agent: AgentSummary
  index: number
  spans: TraceSpan[]
  startMs: number
  now: number
  pixelsPerMs: number
  canvasWidth: number
  viewportWidth: number
  maxTokens: number
  childParentIds: Set<number>
  selectedSpanId: number | null
  onSelectSpan: (span: TraceSpan) => void
}) {
  return (
    <div className="agent-row" style={{ top: RULER_HEIGHT + index * AGENT_HEIGHT }}>
      <div className="agent-label">
        <span className="agent-index">{String(agent.activation_order).padStart(2, '0')}</span>
        <div><strong>{agent.agent_name}</strong><small>{agent.parent_agent_id ? `child of ${agent.parent_agent_id}` : 'main agent'}</small></div>
      </div>
      {(['host', 'llm'] as const).map((sender) => (
        <div key={sender} className={`lane lane-${sender}`} style={{ left: LABEL_WIDTH, width: canvasWidth }}>
          <span className="lane-name">{sender}</span>
          {spans.filter((span) => span.sender === sender).map((span, spanIndex) => {
            const hasChild = childParentIds.has(span.span_id)
            const layout = layoutSpan(span, startMs, now, pixelsPerMs, viewportWidth, hasChild)
            const total = tokenTotal(span)
            return (
              <button
                key={span.span_id}
                className={`trace-block ${sender} ${span.running ? 'running' : ''} ${selectedSpanId === span.span_id ? 'selected' : ''}`}
                style={{
                  left: layout.left,
                  width: layout.width,
                  background: sender === 'llm' ? tokenColor(total, maxTokens) : undefined,
                  zIndex: spanIndex + 2,
                }}
                onClick={() => onSelectSpan(span)}
                title={`${sender.toUpperCase()} · ${formatDuration(span.duration_ms)}`}
              >
                <span className="block-title">{sender === 'llm' ? llmLabel(span) : agent.agent_name}</span>
                <span className="block-duration">{formatDuration(span.duration_ms)}</span>
                {layout.clipped && <b className="clip-mark">//</b>}
              </button>
            )
          })}
        </div>
      ))}
    </div>
  )
}

function ConnectorLayer({ agents, spans, startMs, pixelsPerMs, width }: {
  agents: AgentSummary[]
  spans: TraceSpan[]
  startMs: number
  pixelsPerMs: number
  width: number
}) {
  const agentIndex = new Map(agents.map((agent, index) => [agent.agent_id, index]))
  const connectors = spans.filter((span) => span.sender === 'host' && span.parent_span_id !== null)
  return (
    <svg className="connector-layer" style={{ left: LABEL_WIDTH, top: RULER_HEIGHT, width, height: agents.length * AGENT_HEIGHT }}>
      {connectors.map((span) => {
        const parent = spans.find((candidate) => candidate.span_id === span.parent_span_id)
        const fromIndex = parent ? agentIndex.get(parent.agent_id) : undefined
        const toIndex = agentIndex.get(span.agent_id)
        if (fromIndex === undefined || toIndex === undefined) return null
        const x = Math.max(4, (Date.parse(span.started_at) - startMs) * pixelsPerMs)
        const fromY = fromIndex * AGENT_HEIGHT + 27
        const toY = toIndex * AGENT_HEIGHT + 27
        return <path key={span.span_id} d={`M ${x} ${fromY} h 14 V ${toY} h 9`} />
      })}
    </svg>
  )
}

function DetailDrawer({ span, onClose }: { span: TraceSpan; onClose: () => void }) {
  const [copied, setCopied] = useState(false)
  const raw = JSON.stringify(span, null, 2)
  const copy = async () => {
    await navigator.clipboard.writeText(raw)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1_200)
  }
  return (
    <aside className="detail-drawer">
      <div className="drawer-handle" />
      <header>
        <div>
          <span className={`sender-chip ${span.sender}`}>{span.sender}</span>
          <h2>{span.agent_name} <small>span #{shortId(span.span_id)}</small></h2>
        </div>
        <div className="drawer-actions">
          <button onClick={() => void copy()}><Icon name="copy" />{copied ? 'Copied' : 'Copy JSON'}</button>
          <button className="icon-button" onClick={onClose} aria-label="Close details"><Icon name="close" /></button>
        </div>
      </header>
      <div className="drawer-summary">
        <Metric label="Duration" value={formatDuration(span.duration_ms)} />
        <Metric label="Started" value={new Date(span.started_at).toLocaleTimeString()} />
        <Metric label="Status" value={span.running ? 'Running' : 'Complete'} />
        {span.sender === 'llm' && <Metric label="Tokens" value={String(tokenTotal(span) ?? '—')} />}
      </div>
      <div className="drawer-grid">
        <JsonSection title="System prompt" value={span.system_prompt} wide />
        <JsonSection title="User inputs" value={span.user_inputs} />
        <JsonSection title="Output" value={span.output} />
        <JsonSection title="Tools" value={span.tools} />
        <JsonSection title="Tools called" value={span.tools_called} />
        <JsonSection title="Tool call results" value={span.tool_call_results} />
        <JsonSection title="Token usage" value={span.token_usage} />
        <JsonSection title="Additional data" value={span.data} wide />
      </div>
    </aside>
  )
}

function JsonSection({ title, value, wide = false }: { title: string; value: unknown; wide?: boolean }) {
  return <details className={wide ? 'wide' : ''} open={title === 'System prompt' || title === 'Additional data'}><summary>{title}</summary><pre>{formatValue(value)}</pre></details>
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="metric"><span>{label}</span><strong>{value}</strong></div>
}

function Status({ status, compact = false }: { status: string; compact?: boolean }) {
  return <span className={`status ${status} ${compact ? 'compact' : ''}`}><i />{compact ? status : status === 'running' ? 'Live recording' : 'Completed'}</span>
}

function EmptyState() {
  return <div className="empty-state"><div className="empty-pulse"><Icon name="activity" /></div><h2>Waiting for agent activity</h2><p>Start a traced Bines task. Host and LLM spans will appear here in real time.</p><code>python -m nTrace.server</code></div>
}

function mergeLiveEvent(detail: TraceDetail, event: TraceEvent): TraceDetail {
  const events = upsertEvent(detail.events, event)
  const agents = detail.agents.some((agent) => agent.agent_id === event.agent_id)
    ? detail.agents
    : [...detail.agents, {
      agent_id: event.agent_id,
      parent_agent_id: event.parent_agent_id,
      agent_name: event.agent_name,
      activation_order: event.activation_order,
      first_seen_at: event.timestamp,
    }]
  return {
    ...detail,
    events,
    agents,
    spans: assembleSpans(events),
    updated_at: event.timestamp,
    status: event.sender === 'host' && event.type === 'end' && event.agent_id === 1 ? 'completed' : detail.status,
  }
}

function updateTraceSummaries(current: TraceSummary[], event: TraceEvent): TraceSummary[] {
  const existing = current.find((trace) => trace.trace_id === event.trace_id)
  const next: TraceSummary = existing ? {
    ...existing,
    updated_at: event.timestamp,
    status: event.sender === 'host' && event.type === 'end' && event.agent_id === 1 ? 'completed' : existing.status,
    agent_count: Math.max(existing.agent_count, event.agent_id),
    span_count: existing.span_count + (event.type === 'start' ? 1 : 0),
  } : {
    trace_id: event.trace_id,
    started_at: event.timestamp,
    updated_at: event.timestamp,
    status: event.sender === 'host' && event.type === 'end' && event.agent_id === 1 ? 'completed' : 'running',
    agent_count: event.agent_id,
    span_count: 1,
  }
  return [next, ...current.filter((trace) => trace.trace_id !== event.trace_id)]
}

function nowIfRunning(trace: TraceDetail, now: number): number {
  return trace.status === 'running' ? now : Date.parse(trace.updated_at)
}

function llmLabel(span: TraceSpan): string {
  const calls = span.tools_called
  if (Array.isArray(calls) && calls.length) return `LLM · ${calls.length} tool${calls.length === 1 ? '' : 's'}`
  return 'LLM response'
}

function shortId(value: number): string {
  const text = String(value)
  return text.length > 8 ? text.slice(-8) : text
}

function formatTick(milliseconds: number): string {
  if (milliseconds < 1_000) return `${Math.round(milliseconds)}ms`
  if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(1)}s`
  return `${(milliseconds / 60_000).toFixed(1)}m`
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}
