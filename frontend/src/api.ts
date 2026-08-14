import type { TraceDetail, TraceEvent, TraceSpan, TraceSummary } from './types'

export async function fetchTraces(): Promise<TraceSummary[]> {
  const response = await fetch('/api/v1/traces')
  if (!response.ok) throw new Error(`Unable to load traces (${response.status})`)
  return (await response.json()).traces
}

export async function fetchTrace(traceId: number): Promise<TraceDetail> {
  const response = await fetch(`/api/v1/traces/${traceId}/timeline`)
  if (!response.ok) throw new Error(`Unable to load trace (${response.status})`)
  return response.json()
}

export async function fetchSpan(traceId: number, spanId: number): Promise<TraceSpan> {
  const response = await fetch(`/api/v1/traces/${traceId}/spans/${spanId}`)
  if (!response.ok) throw new Error(`Unable to load span (${response.status})`)
  return response.json()
}

export async function deleteTrace(traceId: number): Promise<void> {
  const response = await fetch(`/api/v1/traces/${traceId}`, { method: 'DELETE' })
  if (!response.ok) throw new Error(`Unable to delete trace (${response.status})`)
}

export function openTraceStream(
  onEvent: (event: TraceEvent) => void,
  onReady: () => void,
  onDeleted: (traceId: number) => void,
): () => void {
  let socket: WebSocket | null = null
  let stopped = false
  let retry = 0
  let timer: number | undefined
  const connect = () => {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    socket = new WebSocket(`${protocol}//${location.host}/api/v1/stream`)
    socket.onopen = () => {
      retry = 0
      onReady()
    }
    socket.onmessage = (message) => {
      const payload = JSON.parse(message.data)
      if (payload.kind === 'event.created') onEvent(payload.event)
      if (payload.kind === 'trace.deleted') onDeleted(payload.trace_id)
    }
    socket.onclose = () => {
      if (stopped) return
      const delay = Math.min(10_000, 500 * 2 ** retry++)
      timer = window.setTimeout(connect, delay)
    }
  }
  connect()
  return () => {
    stopped = true
    if (timer) window.clearTimeout(timer)
    socket?.close()
  }
}
