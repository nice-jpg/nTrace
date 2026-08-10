"""LangChain middleware endpoints for host and model spans."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from .events import (
    json_value,
    latest_output,
    normalize_token_usage,
    tool_calls,
    tool_results,
    tools_payload,
    user_inputs,
)
from .trace import NTrace


def _messages(state: Any) -> list[Any]:
    if isinstance(state, dict):
        return list(state.get("messages") or [])
    return list(getattr(state, "messages", None) or [])


class NTraceStartMiddleware(AgentMiddleware):
    def __init__(self, trace: NTrace) -> None:
        self.trace = trace

    def before_agent(self, state: Any, runtime: Any) -> None:
        self._emit(state)
        return None

    async def abefore_agent(self, state: Any, runtime: Any) -> None:
        self._emit(state)
        return None

    def _emit(self, state: Any) -> None:
        binding = self.trace.begin_agent()
        messages = _messages(state)
        self.trace.emit(
            sender="host",
            event_type="start",
            span_id=binding.host_span_id,
            parent_span_id=binding.parent_span_id,
            system_prompt=self.trace.system_prompt,
            user_inputs=user_inputs(messages),
            tool_call_results=[],
            data={"state": json_value(state)},
        )


class NTraceEndMiddleware(AgentMiddleware):
    def __init__(self, trace: NTrace) -> None:
        self.trace = trace

    def after_agent(self, state: Any, runtime: Any) -> None:
        self._emit_host_end(state)
        return None

    async def aafter_agent(self, state: Any, runtime: Any) -> None:
        self._emit_host_end(state)
        return None

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return self._wrap_model(request, handler)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        span_id = self.trace.next_span_id()
        self._emit_model_start(request, span_id)
        try:
            response = await handler(request)
        except Exception as error:
            self._emit_model_error(request, span_id, error)
            raise
        self._emit_model_end(request, response, span_id)
        return response

    def _wrap_model(self, request: Any, handler: Any) -> Any:
        span_id = self.trace.next_span_id()
        self._emit_model_start(request, span_id)
        try:
            response = handler(request)
        except Exception as error:
            self._emit_model_error(request, span_id, error)
            raise
        self._emit_model_end(request, response, span_id)
        return response

    def _emit_model_start(self, request: Any, span_id: int) -> None:
        binding = self.trace.current_binding
        if binding is None:
            return
        messages = list(getattr(request, "messages", None) or [])
        system_prompt = getattr(request, "system_prompt", None) or getattr(request, "system_message", None)
        self.trace.emit(
            sender="llm",
            event_type="start",
            span_id=span_id,
            parent_span_id=binding.host_span_id,
            system_prompt=system_prompt,
            user_inputs=user_inputs(messages),
            tools=tools_payload(getattr(request, "tools", None)),
            data={"messages": json_value(messages), "model_settings": json_value(getattr(request, "model_settings", None))},
        )

    def _emit_model_end(self, request: Any, response: Any, span_id: int) -> None:
        binding = self.trace.current_binding
        if binding is None:
            return
        request_messages = list(getattr(request, "messages", None) or [])
        result = list(getattr(response, "result", None) or [])
        self.trace.emit(
            sender="llm",
            event_type="end",
            span_id=span_id,
            parent_span_id=binding.host_span_id,
            system_prompt=getattr(request, "system_prompt", None) or getattr(request, "system_message", None),
            user_inputs=user_inputs(request_messages),
            output=latest_output(result),
            tools=tools_payload(getattr(request, "tools", None)),
            tools_called=tool_calls(result),
            token_usage=normalize_token_usage(result),
            data={"response": json_value(response)},
        )

    def _emit_model_error(self, request: Any, span_id: int, error: Exception) -> None:
        binding = self.trace.current_binding
        if binding is None:
            return
        messages = list(getattr(request, "messages", None) or [])
        self.trace.emit(
            sender="llm",
            event_type="end",
            span_id=span_id,
            parent_span_id=binding.host_span_id,
            system_prompt=getattr(request, "system_prompt", None) or getattr(request, "system_message", None),
            user_inputs=user_inputs(messages),
            tools=tools_payload(getattr(request, "tools", None)),
            data={"error_type": type(error).__name__, "error": str(error)},
        )

    def _emit_host_end(self, state: Any) -> None:
        binding = self.trace.current_binding
        if binding is None:
            return
        messages = _messages(state)
        self.trace.emit(
            sender="host",
            event_type="end",
            span_id=binding.host_span_id,
            parent_span_id=binding.parent_span_id,
            system_prompt=self.trace.system_prompt,
            user_inputs=user_inputs(messages),
            output=latest_output(messages),
            tools_called=tool_calls(messages),
            tool_call_results=tool_results(messages),
            data={"state": json_value(state)},
        )


def createNTraceStartMiddleware(trace: NTrace) -> NTraceStartMiddleware:
    return NTraceStartMiddleware(trace)


def createNTraceEndMiddleware(trace: NTrace) -> NTraceEndMiddleware:
    return NTraceEndMiddleware(trace)


__all__ = [
    "NTraceEndMiddleware",
    "NTraceStartMiddleware",
    "createNTraceEndMiddleware",
    "createNTraceStartMiddleware",
]
