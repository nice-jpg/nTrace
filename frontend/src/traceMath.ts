import type { TraceEvent, TraceSpan } from './types'

export const LABEL_WIDTH = 188
export const AGENT_HEIGHT = 104
export const RULER_HEIGHT = 46

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
  return spans.sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at) || a.span_id - b.span_id)
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

export function tokenTotal(span: TraceSpan): number | null {
  const raw = span.token_usage?.total_tokens
  const parsed = typeof raw === 'number' ? raw : Number(raw)
  return Number.isFinite(parsed) ? Math.max(0, parsed) : null
}

export function tokenColor(total: number | null, maximum: number): string {
  if (total === null) return 'hsl(215 12% 42%)'
  const ratio = maximum > 0 ? Math.log1p(total) / Math.log1p(maximum) : 0
  const lightness = 70 - Math.min(1, ratio) * 36
  return `hsl(263 78% ${lightness.toFixed(1)}%)`
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
  nowMs: number,
  pixelsPerMs: number,
  viewportWidth: number,
  hasChild: boolean,
): SpanLayout {
  const spanStart = Date.parse(span.started_at)
  const spanEnd = span.ended_at ? Date.parse(span.ended_at) : nowMs
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
