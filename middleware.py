"""LangChain middleware endpoints for host and model spans."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langchain_core.messages import BaseMessage

from .events import (
    current_tool_exchange,
    json_value,
    latest_output,
    normalize_token_usage,
    tool_calls,
    tools_payload,
    user_inputs,
)
from .trace import NTrace


def _messages(state: AgentState) -> list[BaseMessage]:
    return list(state.get("messages") or [])


class NTraceStartMiddleware(AgentMiddleware):
    def __init__(self, trace: NTrace) -> None:
        self.trace = trace

    def before_model(self, state: AgentState, runtime: Any) -> None:
        self._emit(state)
        return None

    async def abefore_model(self, state: AgentState, runtime: Any) -> None:
        self._emit(state)
        return None

    def _emit(self, state: AgentState) -> None:
        binding, span_id = self.trace.begin_host_span()
        messages = _messages(state)
        self.trace.emit(
            sender="host",
            event_type="start",
            span_id=span_id,
            parent_span_id=binding.parent_span_id,
            system_prompt=self.trace.system_prompt,
            user_inputs=user_inputs(messages),
            tool_call_results=[],
            data={"state": json_value(state)},
        )


class NTraceEndMiddleware(AgentMiddleware):
    def __init__(self, trace: NTrace) -> None:
        self.trace = trace

    def before_model(self, state: AgentState, runtime: Any) -> None:
        self._emit_host_end(state)
        return None

    async def abefore_model(self, state: AgentState, runtime: Any) -> None:
        self._emit_host_end(state)
        return None

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        span_id = self.trace.next_span_id()
        self._emit_model_start(request, span_id)
        try:
            response = handler(request)
        except Exception as error:
            self._emit_model_error(request, span_id, error)
            raise
        self._emit_model_end(request, response, span_id)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        span_id = self.trace.next_span_id()
        self._emit_model_start(request, span_id)
        try:
            response = await handler(request)
        except Exception as error:
            self._emit_model_error(request, span_id, error)
            raise
        self._emit_model_end(request, response, span_id)
        return response

    def _emit_model_start(self, request: ModelRequest, span_id: int) -> None:
        binding = self.trace.current_binding
        if binding is None:
            return
        messages = list(request.messages)
        self.trace.emit(
            sender="llm",
            event_type="start",
            span_id=span_id,
            parent_span_id=self.trace.latest_host_span_id,
            system_prompt=request.system_prompt,
            user_inputs=user_inputs(messages),
            tools=tools_payload(request.tools),
            data={"messages": json_value(messages), "model_settings": json_value(request.model_settings)},
        )

    def _emit_model_end(self, request: ModelRequest, response: ModelResponse, span_id: int) -> None:
        binding = self.trace.current_binding
        if binding is None:
            return
        request_messages = list(request.messages)
        result = list(response.result)
        self.trace.emit(
            sender="llm",
            event_type="end",
            span_id=span_id,
            parent_span_id=self.trace.latest_host_span_id,
            system_prompt=request.system_prompt,
            user_inputs=user_inputs(request_messages),
            output=latest_output(result),
            tools=tools_payload(request.tools),
            tools_called=tool_calls(result),
            token_usage=normalize_token_usage(result),
            data={"response": json_value(response)},
        )

    def _emit_model_error(self, request: ModelRequest, span_id: int, error: Exception) -> None:
        binding = self.trace.current_binding
        if binding is None:
            return
        messages = list(request.messages)
        self.trace.emit(
            sender="llm",
            event_type="end",
            span_id=span_id,
            parent_span_id=self.trace.latest_host_span_id,
            system_prompt=request.system_prompt,
            user_inputs=user_inputs(messages),
            tools=tools_payload(request.tools),
            data={"error_type": type(error).__name__, "error": str(error)},
        )

    def _emit_host_end(self, state: AgentState) -> None:
        host_span = self.trace.finish_host_span()
        if host_span is None:
            return
        binding, span_id = host_span
        messages = _messages(state)
        calls, results = current_tool_exchange(messages)
        self.trace.emit(
            sender="host",
            event_type="end",
            span_id=span_id,
            parent_span_id=binding.parent_span_id,
            system_prompt=self.trace.system_prompt,
            user_inputs=user_inputs(messages),
            output=latest_output(messages),
            tools_called=calls,
            tool_call_results=results,
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
