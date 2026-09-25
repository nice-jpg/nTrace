"""LangChain middleware endpoints for host and model spans."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.types import Command

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


class NTraceMiddleware(AgentMiddleware, ABC):
    """Public stage interface. All telemetry goes through the fail-open sender."""

    @property
    @abstractmethod
    def sender(self) -> str:
        """The lane owned by this stage."""

    def __init__(self, trace: NTrace) -> None:
        self.trace = trace

    def _send(self, **payload: Any) -> None:
        try:
            self.trace.emit(**{"sender": self.sender, "system_prompt": None,
                               "user_inputs": [], **payload})
        except Exception:
            # Observability must not change execution or replace a business error.
            return


class NTraceStartMiddleware(NTraceMiddleware):
    """Compatibility endpoint for existing runtime middleware ordering."""
    sender: str = "host"

    def before_model(self, state: AgentState, runtime: Any) -> None:
        self._emit(state)
        return None

    async def abefore_model(self, state: AgentState, runtime: Any) -> None:
        self._emit(state)
        return None

    def _emit(self, state: AgentState) -> None:
        binding, span_id = self.trace.begin_host_span()
        messages = _messages(state)
        self._send(
            sender="host",
            event_type="start",
            span_id=span_id,
            parent_span_id=binding.parent_span_id,
            system_prompt=self.trace.system_prompt,
            user_inputs=user_inputs(messages),
            tool_call_results=[],
            data={"state": json_value(state)},
        )


class LLMTraceMiddleware(NTraceMiddleware):
    sender: str = "llm"
    _legacy_context = False

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        if not self._legacy_context:
            self.trace.ensure_binding()
        span_id = self.trace.next_span_id()
        self._emit_model_start(request, span_id)
        try:
            response = handler(request)
        except BaseException as error:
            self._emit_model_error(request, span_id, error)
            raise
        self._emit_model_end(request, response, span_id)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if not self._legacy_context:
            self.trace.ensure_binding()
        span_id = self.trace.next_span_id()
        self._emit_model_start(request, span_id)
        try:
            response = await handler(request)
        except BaseException as error:
            self._emit_model_error(request, span_id, error)
            raise
        self._emit_model_end(request, response, span_id)
        return response

    def _emit_model_start(self, request: ModelRequest, span_id: int) -> None:
        binding = self.trace.current_binding
        if binding is None:
            return
        messages = list(request.messages)
        if not self._legacy_context:
            self._send(event_type="start", span_id=span_id, parent_span_id=self.trace.latest_host_span_id,
                       system_prompt=request.system_prompt, user_inputs=user_inputs(messages))
            return
        self._send(
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
        if not self._legacy_context:
            self._send(event_type="end", span_id=span_id, parent_span_id=self.trace.latest_host_span_id,
                       output=json_value(result), token_usage=normalize_token_usage(result),
                       tools_called=tool_calls(result))
            return
        self._send(
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

    def _emit_model_error(self, request: ModelRequest, span_id: int, error: BaseException) -> None:
        binding = self.trace.current_binding
        if binding is None:
            return
        messages = list(request.messages)
        if not self._legacy_context:
            self._send(event_type="end", span_id=span_id, parent_span_id=self.trace.latest_host_span_id,
                       data={"error_type": type(error).__name__, "error": str(error)})
            return
        self._send(
            sender="llm",
            event_type="end",
            span_id=span_id,
            parent_span_id=self.trace.latest_host_span_id,
            system_prompt=request.system_prompt,
            user_inputs=user_inputs(messages),
            tools=tools_payload(request.tools),
            data={"error_type": type(error).__name__, "error": str(error)},
        )


class NTraceEndMiddleware(LLMTraceMiddleware):
    """Compatibility shim; new integrations should install the three stage factories."""
    _legacy_context = True

    def before_model(self, state: AgentState, runtime: Any) -> None:
        self._emit_host_end(state)

    async def abefore_model(self, state: AgentState, runtime: Any) -> None:
        self._emit_host_end(state)

    def _emit_host_end(self, state: AgentState) -> None:
        host_span = self.trace.finish_host_span()
        if host_span is None:
            return
        binding, span_id = host_span
        messages = _messages(state)
        calls, results = current_tool_exchange(messages)
        self._send(
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


class HostTraceMiddleware(NTraceMiddleware):
    sender: str = "host"

    def before_agent(self, state: AgentState, runtime: Any) -> None:
        binding, span_id = self.trace.begin_host_span()
        self._send(event_type="start", span_id=span_id, parent_span_id=binding.parent_span_id,
                   data={"state": json_value(state)})

    async def abefore_agent(self, state: AgentState, runtime: Any) -> None:
        self.before_agent(state, runtime)

    def before_model(self, state: AgentState, runtime: Any) -> None:
        host = self.trace.finish_host_span()
        if host is None:
            # Later graph iterations still capture their own pre-model state.
            self.before_agent(state, runtime)
            host = self.trace.finish_host_span()
        assert host is not None
        binding, span_id = host
        self._send(event_type="end", span_id=span_id, parent_span_id=binding.parent_span_id,
                   data={"state": json_value(state)})

    async def abefore_model(self, state: AgentState, runtime: Any) -> None:
        self.before_model(state, runtime)

    def after_model(self, state: AgentState, runtime: Any) -> None:
        messages = _messages(state)
        if messages and tool_calls(messages[-1:]):
            # Tool execution prepares the next model call's state.
            self.before_agent(state, runtime)

    async def aafter_model(self, state: AgentState, runtime: Any) -> None:
        self.after_model(state, runtime)

    def after_agent(self, state: AgentState, runtime: Any) -> None:
        pending = self.trace.finish_host_span()
        if pending is not None:
            binding, span_id = pending
            self._send(event_type="end", span_id=span_id, parent_span_id=binding.parent_span_id,
                       data={"state": json_value(state), "model_skipped": True})

    async def aafter_agent(self, state: AgentState, runtime: Any) -> None:
        self.after_agent(state, runtime)


class ToolTraceMiddleware(NTraceMiddleware):
    sender: str = "tool"

    def _start(self, request: ToolCallRequest) -> tuple[int, int | None]:
        self.trace.ensure_binding()
        span_id, parent = self.trace.next_span_id(), self.trace.latest_host_span_id
        self._send(event_type="start", span_id=span_id, parent_span_id=parent,
                   tools_called=[json_value(request.tool_call)])
        return span_id, parent

    def wrap_tool_call(self, request: ToolCallRequest,
                       handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command:
        span_id, parent = self._start(request)
        try:
            result = handler(request)
        except BaseException as error:
            self._send(event_type="end", span_id=span_id, parent_span_id=parent,
                       data={"error_type": type(error).__name__, "error": str(error)})
            raise
        self._send(event_type="end", span_id=span_id, parent_span_id=parent,
                   tool_call_results=[json_value(result)])
        return result

    async def awrap_tool_call(self, request: ToolCallRequest,
                             handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]) -> ToolMessage | Command:
        span_id, parent = self._start(request)
        try:
            result = await handler(request)
        except BaseException as error:
            self._send(event_type="end", span_id=span_id, parent_span_id=parent,
                       data={"error_type": type(error).__name__, "error": str(error)})
            raise
        self._send(event_type="end", span_id=span_id, parent_span_id=parent,
                   tool_call_results=[json_value(result)])
        return result


def createNTraceHostMiddleware(trace: NTrace) -> NTraceMiddleware:
    return HostTraceMiddleware(trace)


def createNTraceLLMMiddleware(trace: NTrace) -> NTraceMiddleware:
    return LLMTraceMiddleware(trace)


def createNTraceToolMiddleware(trace: NTrace) -> NTraceMiddleware:
    return ToolTraceMiddleware(trace)


def createNTraceStartMiddleware(trace: NTrace) -> NTraceMiddleware:
    return NTraceStartMiddleware(trace)


def createNTraceEndMiddleware(trace: NTrace) -> NTraceMiddleware:
    return NTraceEndMiddleware(trace)


__all__ = [
    "NTraceMiddleware", "HostTraceMiddleware", "LLMTraceMiddleware", "ToolTraceMiddleware",
    "createNTraceHostMiddleware", "createNTraceLLMMiddleware", "createNTraceToolMiddleware",
    "NTraceEndMiddleware",
    "NTraceStartMiddleware",
    "createNTraceEndMiddleware",
    "createNTraceStartMiddleware",
]
