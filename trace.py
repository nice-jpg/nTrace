"""Per-agent single-active-trace state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import NTraceClient, default_client
from .events import SCHEMA_VERSION, json_value, utc_timestamp
from .ids import next_id


@dataclass(frozen=True)
class TraceBinding:
    trace_id: int
    agent_id: int
    agent_name: str
    activation_order: int
    host_span_id: int
    session_id: str | None = None
    parent_agent_id: int | None = None
    parent_span_id: int | None = None


class NTrace:
    """Trace state privately owned by one agent."""

    def __init__(
        self,
        *,
        agent_name: str,
        system_prompt: str = "",
        client: NTraceClient | Any | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.system_prompt = system_prompt
        self.client = client if client is not None else default_client()
        self.session_id: str | None = None
        self.trace_id: int | None = None
        self._binding: TraceBinding | None = None
        self._prepared = False

    def update(
        self,
        *,
        session_id: str | None,
        resume: bool = False,
    ) -> TraceBinding:
        """Replace this agent's active trace, or retain it for a resume."""

        if resume and self._binding is not None and self.session_id == session_id:
            self._prepared = True
            return self._binding
        self.clear()
        self.session_id = session_id
        self.trace_id = next_id()
        self._binding = TraceBinding(
            trace_id=self.trace_id,
            agent_id=1,
            agent_name=self.agent_name,
            activation_order=1,
            host_span_id=next_id(),
            session_id=session_id,
        )
        self._prepared = True
        return self._binding

    def begin_agent(self) -> TraceBinding:
        """Consume an explicit turn update or create a fresh standalone trace."""

        if not self._prepared:
            self.update(session_id=None)
        self._prepared = False
        assert self._binding is not None
        return self._binding

    def clear(self) -> None:
        """Forget the prior active trace without touching the sender connection."""

        self.session_id = None
        self.trace_id = None
        self._binding = None
        self._prepared = False

    @property
    def current_binding(self) -> TraceBinding | None:
        return self._binding

    def next_span_id(self) -> int:
        return next_id()

    def emit(
        self,
        *,
        sender: str,
        event_type: str,
        span_id: int,
        parent_span_id: int | None,
        system_prompt: Any,
        user_inputs: Any,
        output: Any = None,
        tools: Any = None,
        tools_called: Any = None,
        tool_call_results: Any = None,
        token_usage: Any = None,
        data: Any = None,
    ) -> dict[str, Any] | None:
        binding = self.current_binding
        if binding is None:
            return None
        extra_data = json_value(data or {})
        if isinstance(extra_data, dict):
            extra_data = {"session_id": binding.session_id, **extra_data}
        event = {
            "schema_version": SCHEMA_VERSION,
            "trace_id": binding.trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "agent_id": binding.agent_id,
            "parent_agent_id": binding.parent_agent_id,
            "agent_name": binding.agent_name,
            "activation_order": binding.activation_order,
            "sender": sender,
            "type": event_type,
            "timestamp": utc_timestamp(),
            "system_prompt": json_value(system_prompt),
            "user_inputs": json_value(user_inputs),
            "output": json_value(output),
            "tools": json_value(tools or []),
            "tools_called": json_value(tools_called or []),
            "tool_call_results": json_value(tool_call_results or []),
            "token_usage": json_value(token_usage or {}),
            "data": extra_data,
        }
        try:
            self.client.emit(event)
        except Exception:  # noqa: BLE001 - instrumentation must not affect the agent.
            return None
        return event


__all__ = ["NTrace", "TraceBinding"]
