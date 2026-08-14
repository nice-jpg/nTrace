export type Sender = 'host' | 'llm'
export type EventType = 'start' | 'end'

export interface TraceEvent {
  schema_version: number
  trace_id: number
  span_id: number
  parent_span_id: number | null
  agent_id: number
  parent_agent_id: number | null
  agent_name: string
  activation_order: number
  sender: Sender
  type: EventType
  timestamp: string
  system_prompt?: unknown
  user_inputs?: unknown[]
  output?: unknown
  tools?: unknown[]
  tools_called?: unknown[]
  tool_call_results?: unknown[]
  token_usage?: Record<string, unknown>
  data?: unknown
}

export interface TraceSpan extends Omit<TraceEvent, 'type' | 'timestamp'> {
  type: 'span'
  started_at: string
  ended_at: string | null
  duration_ms: number | null
  running: boolean
  start_event: TraceEvent | null
  end_event: TraceEvent | null
}

export interface AgentSummary {
  agent_id: number
  parent_agent_id: number | null
  agent_name: string
  activation_order: number
  first_seen_at: string
}

export interface TraceSummary {
  trace_id: number
  started_at: string
  updated_at: string
  status: 'running' | 'completed'
  agent_count: number
  span_count: number
}

export interface TraceDetail extends TraceSummary {
  agents: AgentSummary[]
  events: TraceEvent[]
  spans: TraceSpan[]
}
