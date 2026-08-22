import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import { deleteTrace as deleteTraceRequest, fetchSpan, fetchTrace, fetchTraces, fetchUserInputs, openTraceStream } from './api'
import {
  LABEL_WIDTH,
  RULER_HEIGHT,
  assembleSpans,
  centeredZoomScrollLeft,
  childConnectorSpans,
  clampDrawerHeight,
  decodeEscapedText,
  formatDuration,
  formatTickLabel,
  latestEventTime,
  layoutAgentRows,
  layoutSpan,
  tokenColor,
  tokenCost,
  tokenCostBreakdown,
  upsertEvent,
} from './traceMath'
import type { AgentSummary, TraceDetail, TraceEvent, TraceSpan, TraceSummary } from './types'
import type { AgentRowLayout } from './traceMath'
import { getCachedTrace, putCachedTrace } from './traceCache'

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

interface SpanSelection {
  timeline: TraceSpan
  detail: TraceSpan | null
  loading: boolean
  error: string
}

export default function App() {
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detail, setDetail] = useState<TraceDetail | null>(null)
  const [selectedSpan, setSelectedSpan] = useState<SpanSelection | null>(null)
  const [historyOpen, setHistoryOpen] = useState(true)
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState('')
  const selectedIdRef = useRef<number | null>(null)
  const traceCacheRef = useRef(new Map<number, TraceDetail>())
  const spanCacheRef = useRef(new Map<number, Map<number, TraceSpan>>())
  const spanRequestRef = useRef(0)

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

  const cacheTrace = useCallback((trace: TraceDetail) => {
    const evicted = putCachedTrace(traceCacheRef.current, trace)
    evicted.forEach((traceId) => spanCacheRef.current.delete(traceId))
  }, [])

  const refreshSelected = useCallback(async (
    traceId: number | null = selectedIdRef.current,
    force = false,
  ) => {
    if (traceId === null) return
    if (!force) {
      const cached = getCachedTrace(traceCacheRef.current, traceId)
      if (cached) {
        setDetail(cached)
        setError('')
        return
      }
    }
    setDetail(null)
    try {
      const next = await fetchTrace(traceId)
      cacheTrace(next)
      if (selectedIdRef.current !== traceId) return
      setDetail(next)
      setError('')
    } catch (reason) {
      if (selectedIdRef.current !== traceId) return
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }, [cacheTrace])

  const removeTraceLocally = useCallback((traceId: number) => {
    traceCacheRef.current.delete(traceId)
    spanCacheRef.current.delete(traceId)
    setTraces((current) => current.filter((trace) => trace.trace_id !== traceId))
    if (selectedIdRef.current === traceId) {
      setSelectedSpan(null)
      setDetail(null)
      setSelectedId(null)
      void refreshTraces()
    }
  }, [refreshTraces])

  const deleteTrace = useCallback(async (traceId: number) => {
    try {
      await deleteTraceRequest(traceId)
      removeTraceLocally(traceId)
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }, [removeTraceLocally])

  const selectSpan = useCallback((span: TraceSpan) => {
    spanRequestRef.current += 1
    setSelectedSpan({ timeline: span, detail: null, loading: false, error: '' })
  }, [])

  const loadSpanDetails = useCallback(async (span: TraceSpan) => {
    const cached = spanCacheRef.current.get(span.trace_id)?.get(span.span_id)
    if (cached) {
      setSelectedSpan((current) => current?.timeline.span_id === span.span_id
        ? { ...current, detail: cached, loading: false, error: '' }
        : current)
      return
    }
    const requestId = ++spanRequestRef.current
    setSelectedSpan((current) => current?.timeline.span_id === span.span_id
      ? { ...current, loading: true, error: '' }
      : current)
    try {
      const next = await fetchSpan(span.trace_id, span.span_id)
      let traceSpans = spanCacheRef.current.get(span.trace_id)
      if (!traceSpans) {
        traceSpans = new Map<number, TraceSpan>()
        spanCacheRef.current.set(span.trace_id, traceSpans)
      }
      traceSpans.set(span.span_id, next)
      if (requestId !== spanRequestRef.current) return
      setSelectedSpan({ timeline: span, detail: next, loading: false, error: '' })
    } catch (reason) {
      if (requestId !== spanRequestRef.current) return
      setSelectedSpan({
        timeline: span,
        detail: null,
        loading: false,
        error: reason instanceof Error ? reason.message : String(reason),
      })
    }
  }, [])

  const clearSelectedSpan = useCallback(() => {
    spanRequestRef.current += 1
    setSelectedSpan(null)
  }, [])

  useEffect(() => { void refreshTraces() }, [refreshTraces])
  useEffect(() => { void refreshSelected(selectedId) }, [selectedId, refreshSelected])

  useEffect(() => openTraceStream(
    (event) => {
      setConnected(true)
      setTraces((current) => updateTraceSummaries(current, event))
      if (selectedIdRef.current === null) setSelectedId(event.trace_id)
      spanCacheRef.current.get(event.trace_id)?.delete(event.span_id)
      const cached = traceCacheRef.current.get(event.trace_id)
      const cachedUpdate = cached ? mergeLiveEvent(cached, event) : null
      if (cachedUpdate) traceCacheRef.current.set(event.trace_id, cachedUpdate)
      setDetail((current) => {
        if (!current || current.trace_id !== event.trace_id) return current
        return cachedUpdate ?? mergeLiveEvent(current, event)
      })
    },
    () => {
      setConnected(true)
      void refreshTraces()
      void refreshSelected(undefined, true)
    },
    removeTraceLocally,
  ), [refreshSelected, refreshTraces, removeTraceLocally])

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
        onSelect={(id) => { setSelectedId(id); clearSelectedSpan() }}
        onDelete={(id) => void deleteTrace(id)}
      />

      <main className={`workspace ${selectedSpan ? 'detail-open' : ''}`}>
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
            selectedSpanId={selectedSpan?.timeline.span_id ?? null}
            onSelectSpan={selectSpan}
          />
        )}
        {selectedSpan && (
          <DetailDrawer
            key={`${selectedSpan.timeline.trace_id}:${selectedSpan.timeline.span_id}`}
            span={selectedSpan.detail ?? selectedSpan.timeline}
            loading={selectedSpan.loading}
            error={selectedSpan.error}
            onLoadDetails={() => void loadSpanDetails(selectedSpan.timeline)}
            onClose={clearSelectedSpan}
          />
        )}
      </main>
    </div>
  )
}

function HistoryPanel({
  open, traces, selectedId, onToggle, onSelect, onDelete,
}: {
  open: boolean
  traces: TraceSummary[]
  selectedId: number | null
  onToggle: () => void
  onSelect: (traceId: number) => void
  onDelete: (traceId: number) => void
}) {
  const [contextMenu, setContextMenu] = useState<{ traceId: number; x: number; y: number } | null>(null)

  useEffect(() => {
    if (!contextMenu) return
    const close = () => setContextMenu(null)
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close()
    }
    window.addEventListener('click', close)
    window.addEventListener('blur', close)
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('click', close)
      window.removeEventListener('blur', close)
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [contextMenu])

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
            onContextMenu={(event) => {
              event.preventDefault()
              setContextMenu({ traceId: trace.trace_id, x: event.clientX, y: event.clientY })
            }}
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
      {contextMenu && (
        <div
          className="trace-context-menu"
          role="menu"
          style={{
            left: Math.min(contextMenu.x, window.innerWidth - 156),
            top: Math.min(contextMenu.y, window.innerHeight - 52),
          }}
          onClick={(event) => event.stopPropagation()}
        >
          <button
            role="menuitem"
            onClick={() => {
              onDelete(contextMenu.traceId)
              setContextMenu(null)
            }}
          >Delete trace #{shortId(contextMenu.traceId)}</button>
        </div>
      )}
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
  const [collapsedAgentIds, setCollapsedAgentIds] = useState<Set<number>>(() => new Set())

  useEffect(() => {
    const element = viewportRef.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => setViewportWidth(entry.contentRect.width))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const agents = useMemo(() => [...trace.agents].sort((a, b) => a.activation_order - b.activation_order), [trace.agents])
  const rowLayouts = useMemo(
    () => layoutAgentRows(agents.map((agent) => agent.agent_id), collapsedAgentIds),
    [agents, collapsedAgentIds],
  )
  const rowsHeight = rowLayouts.reduce((height, row) => height + row.height, 0)
  const spans = trace.spans
  const startMs = Math.min(...spans.map((span) => Date.parse(span.started_at)), Date.parse(trace.started_at))
  const latestMs = latestEventTime(trace.events, startMs)
  const durationMs = Math.max(1_000, latestMs - startMs)
  const contentViewport = Math.max(320, viewportWidth - LABEL_WIDTH)
  const fitPixelsPerMs = contentViewport / durationMs
  const basePixelsPerMs = Math.max(0.01, fitPixelsPerMs)
  const pixelsPerMs = basePixelsPerMs * zoom
  const canvasWidth = Math.max(contentViewport, durationMs * pixelsPerMs + 80)
  const connectorSpans = useMemo(() => childConnectorSpans(spans), [spans])
  const childParentIds = useMemo(() => new Set(
    connectorSpans.map((span) => span.parent_span_id!),
  ), [connectorSpans])
  const eventOrder = useMemo(() => new Map(
    [...spans]
      .sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at) || a.span_id - b.span_id)
      .map((span, index) => [span.span_id, index + 1]),
  ), [spans])
  const zoomAtCenter = useCallback((direction: 'in' | 'out') => {
    const viewport = viewportRef.current
    if (!viewport) return
    const nextZoom = direction === 'in'
      ? Math.min(24, zoom * 1.35)
      : Math.max(0.5, zoom / 1.35)
    if (nextZoom === zoom) return
    const nextPixelsPerMs = basePixelsPerMs * nextZoom
    const nextCanvasWidth = Math.max(contentViewport, durationMs * nextPixelsPerMs + 80)
    const nextScrollLeft = centeredZoomScrollLeft({
      scrollLeft: viewport.scrollLeft,
      clientWidth: viewport.clientWidth,
      labelWidth: LABEL_WIDTH,
      oldPixelsPerMs: pixelsPerMs,
      newPixelsPerMs: nextPixelsPerMs,
      durationMs,
      newStageWidth: nextCanvasWidth + LABEL_WIDTH,
    })
    setFollow(false)
    setZoom(nextZoom)
    window.requestAnimationFrame(() => {
      if (viewportRef.current) viewportRef.current.scrollLeft = nextScrollLeft
    })
  }, [basePixelsPerMs, contentViewport, durationMs, pixelsPerMs, zoom])

  const panTimeline = useCallback((direction: 'left' | 'right') => {
    const viewport = viewportRef.current
    if (!viewport) return
    setFollow(false)
    viewport.scrollBy({
      left: (direction === 'left' ? -1 : 1) * Math.max(120, viewport.clientWidth * 0.24),
      behavior: 'smooth',
    })
  }, [])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target?.matches('input, textarea, select, [contenteditable="true"]') || event.metaKey || event.ctrlKey || event.altKey) return
      if (event.code === 'KeyW') zoomAtCenter('in')
      else if (event.code === 'KeyS') zoomAtCenter('out')
      else if (event.code === 'KeyA') panTimeline('left')
      else if (event.code === 'KeyD') panTimeline('right')
      else return
      event.preventDefault()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [panTimeline, zoomAtCenter])

  useEffect(() => {
    if (follow && viewportRef.current) viewportRef.current.scrollLeft = viewportRef.current.scrollWidth
  }, [follow, spans.length, latestMs])

  const tickSegments = Math.max(8, Math.min(64, Math.floor((canvasWidth - 80) / 108)))
  const ticks = Array.from({ length: tickSegments + 1 }, (_, index) => ({
    left: (canvasWidth - 80) * index / tickSegments,
    elapsed: durationMs * index / tickSegments,
  }))

  return (
    <section className="timeline-card">
      <div className="timeline-toolbar">
        <div className="legend">
          <span><i className="legend-host" />Host runtime</span>
          <span><i className="legend-llm" />LLM call · depth = tokens</span>
        </div>
        <div className="timeline-actions">
          <span className="key-hint"><kbd>W</kbd><kbd>S</kbd> zoom · <kbd>A</kbd><kbd>D</kbd> move</span>
          <button className={follow ? 'active' : ''} onClick={() => setFollow((value) => !value)}>Live follow</button>
          <button onClick={() => zoomAtCenter('out')} aria-label="Zoom out around viewport center">−</button>
          <span>{Math.round(zoom * 100)}%</span>
          <button onClick={() => zoomAtCenter('in')} aria-label="Zoom in around viewport center">+</button>
        </div>
      </div>
      <div
        className="timeline-viewport"
        ref={viewportRef}
        tabIndex={0}
        aria-label="Trace timeline. W and S zoom; A and D move horizontally."
        onScroll={() => {
          const element = viewportRef.current
          if (element && element.scrollWidth - element.scrollLeft - element.clientWidth > 48) setFollow(false)
        }}
      >
        <div className="timeline-stage" style={{ width: canvasWidth + LABEL_WIDTH, height: RULER_HEIGHT + rowsHeight }}>
          <div className="ruler-corner">AGENT / SOURCE</div>
          <div className="time-ruler" style={{ left: LABEL_WIDTH, width: canvasWidth }}>
            {ticks.map((tick) => <div className="tick" key={tick.left} style={{ left: tick.left }}><span>{formatTickLabel(tick.elapsed, durationMs)}</span></div>)}
          </div>
          <ConnectorLayer rowLayouts={rowLayouts} spans={spans} connectors={connectorSpans} startMs={startMs} pixelsPerMs={pixelsPerMs} width={canvasWidth} height={rowsHeight} />
          {agents.map((agent, agentIndex) => (
            <AgentRows
              key={agent.agent_id}
              agent={agent}
              layout={rowLayouts[agentIndex]}
              spans={spans.filter((span) => span.agent_id === agent.agent_id)}
              startMs={startMs}
              timelineEndMs={latestMs}
              pixelsPerMs={pixelsPerMs}
              canvasWidth={canvasWidth}
              viewportWidth={contentViewport}
              eventOrder={eventOrder}
              childParentIds={childParentIds}
              selectedSpanId={selectedSpanId}
              onSelectSpan={onSelectSpan}
              onToggleCollapsed={() => setCollapsedAgentIds((current) => {
                const next = new Set(current)
                if (next.has(agent.agent_id)) next.delete(agent.agent_id)
                else next.add(agent.agent_id)
                return next
              })}
            />
          ))}
        </div>
      </div>
    </section>
  )
}

function AgentRows({ agent, layout, spans, startMs, timelineEndMs, pixelsPerMs, canvasWidth, viewportWidth, eventOrder, childParentIds, selectedSpanId, onSelectSpan, onToggleCollapsed }: {
  agent: AgentSummary
  layout: AgentRowLayout
  spans: TraceSpan[]
  startMs: number
  timelineEndMs: number
  pixelsPerMs: number
  canvasWidth: number
  viewportWidth: number
  eventOrder: Map<number, number>
  childParentIds: Set<number>
  selectedSpanId: number | null
  onSelectSpan: (span: TraceSpan) => void
  onToggleCollapsed: () => void
}) {
  return (
    <div className={`agent-row ${layout.collapsed ? 'collapsed' : ''}`} style={{ top: RULER_HEIGHT + layout.top, height: layout.height }}>
      <div className="agent-label">
        <span className="agent-index">{String(agent.activation_order).padStart(2, '0')}</span>
        <div><strong>{agent.agent_name}</strong><small>{agent.parent_agent_id ? `child of ${agent.parent_agent_id}` : 'main agent'}</small></div>
        <button
          className="agent-collapse-button"
          onClick={onToggleCollapsed}
          aria-label={`${layout.collapsed ? 'Expand' : 'Collapse'} agent ${agent.agent_name}`}
          title={`${layout.collapsed ? 'Expand' : 'Collapse'} agent lanes`}
        ><Icon name="chevron" /></button>
      </div>
      {!layout.collapsed && (['host', 'llm'] as const).map((sender) => (
        <div key={sender} className={`lane lane-${sender}`} style={{ left: LABEL_WIDTH, width: canvasWidth }}>
          <span className="lane-name">{sender}</span>
          {spans.filter((span) => span.sender === sender).map((span, spanIndex) => {
            const hasChild = childParentIds.has(span.span_id)
            const layout = layoutSpan(span, startMs, timelineEndMs, pixelsPerMs, viewportWidth, hasChild)
            const order = eventOrder.get(span.span_id) ?? spanIndex + 1
            const cost = sender === 'llm' ? tokenCost(span) : 0
            return (
              <button
                key={span.span_id}
                className={`trace-block ${sender} ${span.running ? 'running' : ''} ${selectedSpanId === span.span_id ? 'selected' : ''}`}
                style={{
                  left: layout.left,
                  width: layout.width,
                  background: sender === 'llm' ? tokenColor(cost) : undefined,
                }}
                onClick={() => onSelectSpan(span)}
                title={`#${order} · ${sender.toUpperCase()} · ${formatPreciseTime(span.started_at)} · ${formatDuration(span.duration_ms)}`}
              >
                <span className="block-order">#{String(order).padStart(2, '0')}</span>
                {sender === 'llm' && <span className="block-title">{llmLabel(span)}</span>}
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

function ConnectorLayer({ rowLayouts, spans, connectors, startMs, pixelsPerMs, width, height }: {
  rowLayouts: AgentRowLayout[]
  spans: TraceSpan[]
  connectors: TraceSpan[]
  startMs: number
  pixelsPerMs: number
  width: number
  height: number
}) {
  const layoutByAgent = new Map(rowLayouts.map((layout) => [layout.agentId, layout]))
  return (
    <svg className="connector-layer" style={{ left: LABEL_WIDTH, top: RULER_HEIGHT, width, height }}>
      {connectors.map((span) => {
        const parent = spans.find((candidate) => candidate.span_id === span.parent_span_id)
        const fromLayout = parent ? layoutByAgent.get(parent.agent_id) : undefined
        const toLayout = layoutByAgent.get(span.agent_id)
        if (!fromLayout || !toLayout) return null
        const x = Math.max(4, (Date.parse(span.started_at) - startMs) * pixelsPerMs)
        const fromY = fromLayout.top + (fromLayout.collapsed ? fromLayout.height / 2 : 27)
        const toY = toLayout.top + (toLayout.collapsed ? toLayout.height / 2 : 27)
        return <path key={span.span_id} d={`M ${x} ${fromY} h 14 V ${toY} h 9`} />
      })}
    </svg>
  )
}

function DetailDrawer({ span, loading, error, onLoadDetails, onClose }: {
  span: TraceSpan
  loading: boolean
  error: string
  onLoadDetails: () => void
  onClose: () => void
}) {
  const [copied, setCopied] = useState(false)
  const [height, setHeight] = useState(() => clampDrawerHeight(Math.min(window.innerHeight * 0.46, 480), window.innerHeight))
  const dragRef = useRef<{ pointerId: number; startY: number; startHeight: number } | null>(null)
  const raw = JSON.stringify(span, null, 2)
  const copy = async () => {
    await navigator.clipboard.writeText(raw)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1_200)
  }
  useEffect(() => {
    const onResize = () => setHeight((current) => clampDrawerHeight(current, window.innerHeight))
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      document.body.classList.remove('resizing-drawer')
    }
  }, [])
  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    dragRef.current = { pointerId: event.pointerId, startY: event.clientY, startHeight: height }
    event.currentTarget.setPointerCapture(event.pointerId)
    document.body.classList.add('resizing-drawer')
    event.preventDefault()
  }
  const resize = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    setHeight(clampDrawerHeight(drag.startHeight + drag.startY - event.clientY, window.innerHeight))
  }
  const stopResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return
    dragRef.current = null
    document.body.classList.remove('resizing-drawer')
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }
  return (
    <aside className="detail-drawer" style={{ height }}>
      <div
        className="drawer-resizer"
        role="separator"
        aria-label="Resize trace details"
        aria-orientation="horizontal"
        tabIndex={0}
        onPointerDown={startResize}
        onPointerMove={resize}
        onPointerUp={stopResize}
        onPointerCancel={stopResize}
        onKeyDown={(event) => {
          if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return
          setHeight((current) => clampDrawerHeight(current + (event.key === 'ArrowUp' ? 32 : -32), window.innerHeight))
          event.preventDefault()
        }}
      ><span /></div>
      <header className="drawer-header">
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
        <Metric label="Started" value={formatPreciseTime(span.started_at)} />
        <Metric label="Status" value={span.running ? 'Running' : 'Complete'} />
        {span.sender === 'llm' && <Metric label="Weighted cost" value={tokenCost(span)?.toLocaleString() ?? '—'} />}
      </div>
      <div className="drawer-grid">
        <JsonSection title="System prompt" value={span.system_prompt} wide loading={loading} error={error} onOpen={onLoadDetails} />
        <LazyUserInputsSection traceId={span.trace_id} spanId={span.span_id} />
        <JsonSection title="Output" value={span.output} loading={loading} error={error} onOpen={onLoadDetails} />
        <JsonSection title="Tools" value={span.tools} loading={loading} error={error} onOpen={onLoadDetails} />
        <JsonSection title="Tools called" value={span.tools_called} loading={loading} error={error} onOpen={onLoadDetails} />
        {span.sender === 'host' && <JsonSection title="Tool call results" value={span.tool_call_results} loading={loading} error={error} onOpen={onLoadDetails} />}
        <TokenUsageSection usage={span.token_usage ?? {}} loading={loading} error={error} onOpen={onLoadDetails} />
        <JsonSection title="Additional data" value={span.data} wide loading={loading} error={error} onOpen={onLoadDetails} />
      </div>
    </aside>
  )
}

export function UserInputsSection({ inputs, defaultOpen = false }: { inputs: unknown[]; defaultOpen?: boolean }) {
  return (
    <details className="user-inputs-section wide" open={defaultOpen}>
      <summary>User inputs <span>{inputs.length}</span></summary>
      <div className="user-input-list">
        {inputs.length === 0 && <div className="detail-empty">No user inputs</div>}
        {inputs.map((input, index) => (
          <article className="user-input-item" key={index}>
            <header>
              <span>Input {String(index + 1).padStart(2, '0')}</span>
              <b>{inputLabel(input)}</b>
            </header>
            <InputContent input={input} />
          </article>
        ))}
      </div>
    </details>
  )
}

function LazyUserInputsSection({ traceId, spanId }: { traceId: number; spanId: number }) {
  const [open, setOpen] = useState(false)
  const [inputs, setInputs] = useState<unknown[]>([])
  const [total, setTotal] = useState<number | null>(null)
  const [hasMore, setHasMore] = useState(true)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const loadingRef = useRef(false)

  const loadMore = useCallback(async () => {
    if (loadingRef.current || !hasMore) return
    loadingRef.current = true
    setLoading(true)
    setError('')
    try {
      const page = await fetchUserInputs(traceId, spanId, inputs.length, 10)
      setInputs((current) => [...current, ...page.items])
      setTotal(page.total)
      setHasMore(page.has_more)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      loadingRef.current = false
      setLoading(false)
    }
  }, [hasMore, inputs.length, spanId, traceId])

  return (
    <details
      className="user-inputs-section wide"
      open={open}
      onToggle={(event) => {
        const nextOpen = event.currentTarget.open
        setOpen(nextOpen)
        if (nextOpen && total === null) void loadMore()
      }}
    >
      <summary>User inputs {total !== null && <span>{total}</span>}</summary>
      {open && <div
        className="user-input-list lazy"
        onScroll={(event) => {
          const element = event.currentTarget
          if (element.scrollHeight - element.scrollTop - element.clientHeight <= 40) void loadMore()
        }}
      >
        {!loading && !error && inputs.length === 0 && <div className="detail-empty">No user inputs</div>}
        {inputs.map((input, index) => (
          <article className="user-input-item" key={index}>
            <header>
              <span>Input {total === null ? '—' : String(total - index).padStart(2, '0')}</span>
              <b>{inputLabel(input)}</b>
            </header>
            <InputContent input={input} />
          </article>
        ))}
        {loading && <div className="section-loading">Loading inputs…</div>}
        {error && <button className="section-retry" onClick={() => void loadMore()}>{error} · Retry</button>}
        {!loading && !error && inputs.length > 0 && !hasMore && <div className="detail-empty">All inputs loaded</div>}
      </div>}
    </details>
  )
}

function InputContent({ input }: { input: unknown }) {
  const item = inputRecord(input)
  if (item?.type === 'function_call') {
    return <div className="function-call-input">
      <div><span>Name</span><strong>{formatValue(item.name)}</strong></div>
      <div><span>Arguments</span><pre>{decodeEscapedText(formatValue(item.arguments))}</pre></div>
    </div>
  }
  if (item?.type === 'reasoning') {
    return <pre>{decodeEscapedText(reasoningText(item.summary))}</pre>
  }
  const content = item?.type === 'message' ? item.content : input
  return <pre>{decodeEscapedText(typeof content === 'string' ? content : formatValue(content))}</pre>
}

function inputRecord(input: unknown): Record<string, unknown> | null {
  return typeof input === 'object' && input !== null && !Array.isArray(input)
    ? input as Record<string, unknown>
    : null
}

function inputLabel(input: unknown): string {
  const item = inputRecord(input)
  const type = typeof item?.type === 'string' ? item.type : 'message'
  const role = type === 'message' && typeof item?.role === 'string' ? ` · ${item.role}` : ''
  return `${type}${role}`
}

function reasoningText(summary: unknown): string {
  if (Array.isArray(summary)) {
    return summary.map((item) => {
      const record = inputRecord(item)
      return typeof record?.text === 'string' ? record.text : formatValue(item)
    }).join('\n')
  }
  const record = inputRecord(summary)
  return typeof record?.text === 'string' ? record.text : formatValue(summary)
}

function TokenUsageSection({ usage, loading, error, onOpen }: {
  usage: Record<string, unknown>
  loading: boolean
  error: string
  onOpen: () => void
}) {
  const [open, setOpen] = useState(false)
  const cost = tokenCostBreakdown(usage)
  return (
    <details
      className="token-usage-section wide"
      open={open}
      onToggle={(event) => {
        setOpen(event.currentTarget.open)
        if (event.currentTarget.open) onOpen()
      }}
    >
      <summary>Token usage</summary>
      {open && <SectionContent loading={loading} error={error}>
        <div className="token-focus-grid">
          <TokenMetric label="Uncached input tokens" value={cost.uncachedInputTokens} tone="input" />
          <TokenMetric label="Output tokens" value={cost.outputTokens} tone="output" />
          <TokenMetric label="Cached tokens" value={cost.cachedTokens} tone="cached" />
          <TokenMetric label="Weighted cost" value={cost.weightedCost} tone="cost" />
        </div>
        <div className="token-formula">output × 100 + cached + uncached input × 50</div>
        <pre>{formatValue(usage)}</pre>
      </SectionContent>}
    </details>
  )
}

function TokenMetric({ label, value, tone }: { label: string; value: number; tone: string }) {
  return <div className={`token-metric ${tone}`}><span>{label}</span><strong>{value.toLocaleString()}</strong></div>
}

function JsonSection({ title, value, wide = false, loading, error, onOpen }: {
  title: string
  value: unknown
  wide?: boolean
  loading: boolean
  error: string
  onOpen: () => void
}) {
  const [open, setOpen] = useState(false)
  return <details
    className={wide ? 'wide' : ''}
    open={open}
    onToggle={(event) => {
      setOpen(event.currentTarget.open)
      if (event.currentTarget.open) onOpen()
    }}
  >
    <summary>{title}</summary>
    {open && <SectionContent loading={loading} error={error}><pre>{formatValue(value)}</pre></SectionContent>}
  </details>
}

function SectionContent({ loading, error, children }: {
  loading: boolean
  error: string
  children: ReactNode
}) {
  if (loading) return <div className="section-loading">Loading span details…</div>
  if (error) return <div className="section-error">{error}</div>
  return <>{children}</>
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
    status: detail.status,
  }
}

function updateTraceSummaries(current: TraceSummary[], event: TraceEvent): TraceSummary[] {
  const existing = current.find((trace) => trace.trace_id === event.trace_id)
  const next: TraceSummary = existing ? {
    ...existing,
    updated_at: event.timestamp,
    status: existing.status,
    agent_count: Math.max(existing.agent_count, event.agent_id),
    span_count: existing.span_count + (event.type === 'start' ? 1 : 0),
  } : {
    trace_id: event.trace_id,
    started_at: event.timestamp,
    updated_at: event.timestamp,
    status: 'running',
    agent_count: event.agent_id,
    span_count: 1,
  }
  return [next, ...current.filter((trace) => trace.trace_id !== event.trace_id)]
}

function llmLabel(span: TraceSpan): string {
  const calls = span.tools_called ?? []
  if (Array.isArray(calls) && calls.length) return `LLM · ${calls.length} tool${calls.length === 1 ? '' : 's'}`
  return 'LLM response'
}

function shortId(value: number): string {
  const text = String(value)
  return text.length > 8 ? text.slice(-8) : text
}

function formatPreciseTime(value: string): string {
  const date = new Date(value)
  const time = date.toLocaleTimeString(undefined, {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })
  return `${time}.${String(date.getMilliseconds()).padStart(3, '0')}`
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}
