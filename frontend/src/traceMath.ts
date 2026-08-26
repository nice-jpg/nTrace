import type { AgentSummary, TraceEvent, TraceSpan } from './types'

export const LABEL_WIDTH = 188
export const AGENT_HEIGHT = 104
export const COLLAPSED_AGENT_HEIGHT = 40
export const RULER_HEIGHT = 46

export interface AgentRowLayout {
  agentId: number
  top: number
  height: number
  collapsed: boolean
}

export function layoutAgentRows(agentIds: number[], collapsedAgentIds: Set<number>): AgentRowLayout[] {
  let top = 0
  return agentIds.map((agentId) => {
    const collapsed = collapsedAgentIds.has(agentId)
    const height = collapsed ? COLLAPSED_AGENT_HEIGHT : AGENT_HEIGHT
    const layout = { agentId, top, height, collapsed }
    top += height
    return layout
  })
}

export function assembleSpans(events: TraceEvent[]): TraceSpan[] {
  const pairs = new Map<number, { start?: TraceEvent; end?: TraceEvent }>()
  for (const event of events) {
    const pair = pairs.get(event.span_id) ?? {}
    pair[event.type] = event
    pairs.set(event.span_id, pair)
  }
  const spans: TraceSpan[] = []
  for (const [spanId, pair] of pairs.entries()) {
    const base = pair.end ?? pair.start
    if (!base) continue
    const start = pair.start ?? pair.end!
    const merged = { ...start, ...(pair.end ?? {}) }
    const { type: _type, timestamp: _timestamp, ...fields } = merged
    const duration = pair.end
      ? Math.max(0, Date.parse(pair.end.timestamp) - Date.parse(start.timestamp))
      : null
    spans.push({
      ...fields,
      span_id: spanId,
      type: 'span',
      started_at: start.timestamp,
      ended_at: pair.end?.timestamp ?? null,
      duration_ms: duration,
      running: !pair.end,
      start_event: pair.start ?? null,
      end_event: pair.end ?? null,
    })
  }
  spans.sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at) || a.span_id - b.span_id)
  const previousByLane = new Map<string, TraceSpan>()
  for (const span of spans) {
    const lane = `${span.agent_id}:${span.sender}`
    const previous = previousByLane.get(lane)
    if (previous?.running) {
      const previousStart = Date.parse(previous.started_at)
      const nextStart = Date.parse(span.started_at)
      previous.ended_at = span.started_at
      previous.duration_ms = Number.isFinite(previousStart) && Number.isFinite(nextStart)
        ? Math.max(0, nextStart - previousStart)
        : null
      previous.running = false
    }
    previousByLane.set(lane, span)
  }
  return spans
}

export function upsertEvent(events: TraceEvent[], incoming: TraceEvent): TraceEvent[] {
  const index = events.findIndex(
    (event) => event.trace_id === incoming.trace_id && event.span_id === incoming.span_id && event.type === incoming.type,
  )
  if (index < 0) return [...events, incoming]
  const copy = [...events]
  copy[index] = incoming
  return copy
}

export function latestEventTime(events: TraceEvent[], fallbackMs: number): number {
  return events.reduce((latest, event) => {
    const timestamp = Date.parse(event.timestamp)
    return Number.isFinite(timestamp) ? Math.max(latest, timestamp) : latest
  }, fallbackMs)
}

export function centeredZoomScrollLeft({
  scrollLeft,
  clientWidth,
  labelWidth,
  oldPixelsPerMs,
  newPixelsPerMs,
  durationMs,
  newStageWidth,
}: {
  scrollLeft: number
  clientWidth: number
  labelWidth: number
  oldPixelsPerMs: number
  newPixelsPerMs: number
  durationMs: number
  newStageWidth: number
}): number {
  const viewportCenter = scrollLeft + clientWidth / 2
  const centerTime = Math.min(
    durationMs,
    Math.max(0, (viewportCenter - labelWidth) / Math.max(oldPixelsPerMs, Number.EPSILON)),
  )
  const anchoredCenter = labelWidth + centerTime * newPixelsPerMs
  return Math.min(
    Math.max(0, newStageWidth - clientWidth),
    Math.max(0, anchoredCenter - clientWidth / 2),
  )
}

export function clampDrawerHeight(height: number, viewportHeight: number): number {
  const minimumHeight = 190
  const workspaceReserve = 200
  return Math.min(
    Math.max(minimumHeight, height),
    Math.max(minimumHeight, viewportHeight - workspaceReserve),
  )
}

export function tokenTotal(span: TraceSpan): number | null {
  const raw = span.token_usage?.total_tokens
  const parsed = typeof raw === 'number' ? raw : Number(raw)
  return Number.isFinite(parsed) ? Math.max(0, parsed) : null
}

function numericTokenField(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? Math.max(0, parsed) : 0
}

export interface TokenCostBreakdown {
  inputTokens: number
  uncachedInputTokens: number
  outputTokens: number
  cachedTokens: number
  weightedCost: number
}

export function tokenCostBreakdown(usage: Record<string, unknown> | null | undefined): TokenCostBreakdown {
  const source = usage ?? {}
  const details = typeof source.details === 'object' && source.details !== null
    ? source.details as Record<string, unknown>
    : {}
  const inputDetails = typeof details.input_token_details === 'object' && details.input_token_details !== null
    ? details.input_token_details as Record<string, unknown>
    : typeof source.input_token_details === 'object' && source.input_token_details !== null
      ? source.input_token_details as Record<string, unknown>
      : {}
  const inputTokens = numericTokenField(source.input_tokens ?? details.input_tokens)
  const outputTokens = numericTokenField(source.output_tokens ?? details.output_tokens)
  const cachedTokens = Math.min(inputTokens, numericTokenField(inputDetails.cached_tokens))
  const uncachedInputTokens = inputTokens - cachedTokens
  return {
    inputTokens,
    uncachedInputTokens,
    outputTokens,
    cachedTokens,
    weightedCost: outputTokens * 100 + cachedTokens + uncachedInputTokens * 50,
  }
}

export function tokenCost(span: TraceSpan): number | null {
  if (!span.token_usage || Object.keys(span.token_usage).length === 0) return null
  return tokenCostBreakdown(span.token_usage).weightedCost
}

export interface TokenStatPoint extends TokenCostBreakdown {
  index: number
  spanId: number
  agentId: number
  label: string
  llmCalls: number
}

export interface AgentTokenStatistics {
  llmCalls: TokenStatPoint[]
  subagentCalls: TokenStatPoint[]
  totalCost: number
}

export function agentTokenStatistics(
  agentId: number,
  agents: AgentSummary[],
  spans: TraceSpan[],
): AgentTokenStatistics {
  const orderedSpans = [...spans].sort(
    (a, b) => Date.parse(a.started_at) - Date.parse(b.started_at) || a.span_id - b.span_id,
  )
  const llmCalls = orderedSpans
    .filter((span) => span.agent_id === agentId && span.sender === 'llm')
    .map((span, index) => ({
      ...tokenCostBreakdown(span.token_usage),
      index: index + 1,
      spanId: span.span_id,
      agentId: span.agent_id,
      label: `LLM call ${index + 1}`,
      llmCalls: 1,
    }))

  const childrenByParent = new Map<number, number[]>()
  for (const agent of agents) {
    if (agent.parent_agent_id === null) continue
    const children = childrenByParent.get(agent.parent_agent_id) ?? []
    children.push(agent.agent_id)
    childrenByParent.set(agent.parent_agent_id, children)
  }
  const subtreeAgentIds = (rootAgentId: number): Set<number> => {
    const result = new Set<number>()
    const pending = [rootAgentId]
    while (pending.length) {
      const current = pending.pop()!
      if (result.has(current)) continue
      result.add(current)
      pending.push(...(childrenByParent.get(current) ?? []))
    }
    return result
  }
  const directChildren = agents
    .filter((agent) => agent.parent_agent_id === agentId)
    .sort((a, b) => a.activation_order - b.activation_order || a.agent_id - b.agent_id)
  const subagentCalls = directChildren.flatMap((agent, childIndex) => {
    const targetSpan = orderedSpans.find(
      (span) => span.agent_id === agent.agent_id && span.sender === 'host',
    ) ?? orderedSpans.find((span) => span.agent_id === agent.agent_id)
    if (!targetSpan) return []
    const descendants = subtreeAgentIds(agent.agent_id)
    const childLlmSpans = orderedSpans.filter(
      (span) => descendants.has(span.agent_id) && span.sender === 'llm',
    )
    const total = childLlmSpans.reduce<TokenCostBreakdown>((sum, span) => {
      const cost = tokenCostBreakdown(span.token_usage)
      return {
        inputTokens: sum.inputTokens + cost.inputTokens,
        uncachedInputTokens: sum.uncachedInputTokens + cost.uncachedInputTokens,
        outputTokens: sum.outputTokens + cost.outputTokens,
        cachedTokens: sum.cachedTokens + cost.cachedTokens,
        weightedCost: sum.weightedCost + cost.weightedCost,
      }
    }, { inputTokens: 0, uncachedInputTokens: 0, outputTokens: 0, cachedTokens: 0, weightedCost: 0 })
    return [{
      ...total,
      index: childIndex + 1,
      spanId: targetSpan.span_id,
      agentId: agent.agent_id,
      label: agent.agent_name,
      llmCalls: childLlmSpans.length,
    }]
  })

  return {
    llmCalls,
    subagentCalls,
    totalCost: llmCalls.reduce((sum, point) => sum + point.weightedCost, 0)
      + subagentCalls.reduce((sum, point) => sum + point.weightedCost, 0),
  }
}

export function decodeEscapedText(value: string): string {
  return value.replace(/\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|n|r|t|b|f|v|0|\\|"|')/g, (match, escape: string) => {
    if (escape.startsWith('u{')) return String.fromCodePoint(Number.parseInt(escape.slice(2, -1), 16))
    if (escape.startsWith('u')) return String.fromCharCode(Number.parseInt(escape.slice(1), 16))
    const values: Record<string, string> = {
      n: '\n', r: '\r', t: '\t', b: '\b', f: '\f', v: '\v', 0: '\0', '\\': '\\', '"': '"', "'": "'",
    }
    return values[escape] ?? match
  })
}

export function formatTickLabel(milliseconds: number, durationMs: number): string {
  if (durationMs < 1_000) return `${milliseconds.toFixed(1)}ms`
  if (durationMs < 10_000) return `${(milliseconds / 1_000).toFixed(3)}s`
  if (durationMs < 60_000) return `${(milliseconds / 1_000).toFixed(3)}s`
  if (durationMs < 3_600_000) {
    const minutes = Math.floor(milliseconds / 60_000)
    const precision = 3
    const seconds = ((milliseconds % 60_000) / 1_000).toFixed(precision).padStart(precision + 3, '0')
    return `${minutes}:${seconds}`
  }
  const hours = Math.floor(milliseconds / 3_600_000)
  const minutes = Math.floor((milliseconds % 3_600_000) / 60_000)
  const seconds = Math.floor((milliseconds % 60_000) / 1_000)
  return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

export function tokenColor(weightedCost: number | null): string {
  if (weightedCost === null) return 'hsl(215 12% 42%)'
  const costInThousands = Math.max(0, weightedCost) / 1_000
  const ratio = costInThousands <= 300
    ? Math.pow(costInThousands / 300, 0.45) * 0.92
    : 0.92 + Math.min(1, (costInThousands - 300) / 700) * 0.08
  const lightness = 80 - ratio * 59
  const saturation = 62 + ratio * 28
  return `hsl(263 ${saturation.toFixed(1)}% ${lightness.toFixed(1)}%)`
}

export function childConnectorSpans(spans: TraceSpan[]): TraceSpan[] {
  const firstByChild = new Map<number, TraceSpan>()
  for (const span of [...spans].sort(
    (a, b) => Date.parse(a.started_at) - Date.parse(b.started_at) || a.span_id - b.span_id,
  )) {
    if (span.sender !== 'host' || span.parent_span_id === null || span.parent_agent_id === null) continue
    if (!firstByChild.has(span.agent_id)) firstByChild.set(span.agent_id, span)
  }
  return [...firstByChild.values()]
}

export interface SpanLayout {
  left: number
  naturalWidth: number
  width: number
  clipped: boolean
}

export function layoutSpan(
  span: TraceSpan,
  startMs: number,
  timelineEndMs: number,
  pixelsPerMs: number,
  viewportWidth: number,
  hasChild: boolean,
): SpanLayout {
  const spanStart = Date.parse(span.started_at)
  const spanEnd = span.ended_at ? Date.parse(span.ended_at) : timelineEndMs
  const naturalWidth = Math.max(18, (Math.max(spanStart, spanEnd) - spanStart) * pixelsPerMs)
  const cap = Math.max(48, viewportWidth / 3)
  const width = hasChild ? naturalWidth : Math.min(naturalWidth, cap)
  return {
    left: Math.max(0, (spanStart - startMs) * pixelsPerMs),
    naturalWidth,
    width,
    clipped: !hasChild && naturalWidth > cap,
  }
}

export function formatDuration(milliseconds: number | null): string {
  if (milliseconds === null) return 'running'
  if (milliseconds < 1_000) return `${Math.round(milliseconds)} ms`
  if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(milliseconds < 10_000 ? 2 : 1)} s`
  return `${(milliseconds / 60_000).toFixed(1)} min`
}
