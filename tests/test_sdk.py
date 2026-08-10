from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nTrace import NTrace, createNTraceEndMiddleware, createNTraceStartMiddleware
from nTrace.client import default_client
from nTrace.events import json_value, normalize_token_usage
from nTrace.ids import MAX_SAFE_INTEGER, next_id


class MemoryClient:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event):
        self.events.append(event)
        return True


def message(role, content, **kwargs):
    return SimpleNamespace(type=role, content=content, **kwargs)


def test_host_start_and_end_share_span_and_start_has_no_tool_results() -> None:
    client = MemoryClient()
    trace = NTrace(agent_name="main", system_prompt="system", client=client)
    start = createNTraceStartMiddleware(trace)
    end = createNTraceEndMiddleware(trace)
    state = {
        "messages": [
            message("human", "hello"),
            message("tool", "tool-result", name="search", tool_call_id="call-1"),
            message("ai", "done", tool_calls=[]),
        ],
        "custom": {"page": 2},
    }

    trace.update(session_id="host-test")
    start.before_agent(state, None)
    end.after_agent(state, None)

    assert [event["type"] for event in client.events] == ["start", "end"]
    assert client.events[0]["span_id"] == client.events[1]["span_id"]
    assert client.events[0]["tool_call_results"] == []
    assert client.events[1]["tool_call_results"][0]["content"] == "tool-result"
    assert client.events[1]["data"]["state"]["custom"] == {"page": 2}


def test_model_middleware_captures_actual_request_response_and_usage() -> None:
    client = MemoryClient()
    trace = NTrace(agent_name="main", client=client)
    middleware = createNTraceEndMiddleware(trace)
    request = SimpleNamespace(
        messages=[message("human", "actual input")],
        system_prompt="actual system",
        system_message=None,
        tools=[{"name": "search", "description": "Search"}],
        model_settings={"temperature": 0},
    )
    response = SimpleNamespace(
        result=[
            message(
                "ai",
                "answer",
                tool_calls=[{"id": "c1", "name": "search", "args": {"q": "x"}}],
                usage_metadata={"input_tokens": 8, "output_tokens": 5, "total_tokens": 13},
                response_metadata={},
            )
        ],
        structured_response=None,
    )

    trace.update(session_id="model-test")
    returned = middleware.wrap_model_call(request, lambda actual: response)

    assert returned is response
    assert [event["sender"] for event in client.events] == ["llm", "llm"]
    assert client.events[0]["user_inputs"] == ["actual input"]
    assert client.events[1]["output"] == "answer"
    assert client.events[1]["token_usage"]["total_tokens"] == 13
    assert client.events[1]["tools_called"][0]["name"] == "search"


def test_async_model_call_is_traced() -> None:
    client = MemoryClient()
    trace = NTrace(agent_name="main", client=client)
    middleware = createNTraceEndMiddleware(trace)
    request = SimpleNamespace(messages=[], system_prompt="s", system_message=None, tools=[], model_settings={})
    response = SimpleNamespace(result=[], structured_response=None)

    async def run():
        async def handler(_request):
            return response
        trace.update(session_id="async-test")
        return await middleware.awrap_model_call(request, handler)

    assert asyncio.run(run()) is response
    assert [event["type"] for event in client.events] == ["start", "end"]


def test_each_agent_owns_an_independent_trace_without_context_inheritance() -> None:
    client = MemoryClient()
    root = NTrace(agent_name="main", client=client)
    child = NTrace(agent_name="collector", client=client)
    root_binding = root.update(session_id="parent-session")
    child_binding = child.update(session_id="collector:visit-1")

    assert child_binding.trace_id != root_binding.trace_id
    assert child_binding.agent_id == 1
    assert child_binding.parent_agent_id is None
    assert child_binding.parent_span_id is None
    assert root.current_binding is root_binding


def test_agent_local_traces_share_only_the_default_ordered_transport() -> None:
    root = NTrace(agent_name="main")
    child = NTrace(agent_name="collector")

    assert root.client is default_client()
    assert child.client is root.client
    assert root.current_binding is None
    assert child.current_binding is None


def test_update_replaces_the_single_active_trace_and_resume_reuses_it() -> None:
    trace = NTrace(agent_name="main", client=MemoryClient())
    first = trace.update(session_id="session")
    assert trace.update(session_id="session", resume=True) is first

    second = trace.update(session_id="session")
    assert second is not first
    assert second.trace_id != first.trace_id
    assert trace.trace_id == second.trace_id


def test_replacing_trace_state_does_not_close_the_transport() -> None:
    class CloseAwareClient(MemoryClient):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    client = CloseAwareClient()
    trace = NTrace(agent_name="main", client=client)
    trace.update(session_id="first")
    trace.update(session_id="second")
    trace.clear()

    assert client.close_calls == 0


def test_ids_are_javascript_safe_and_serialization_is_recursive_safe() -> None:
    values = [next_id() for _ in range(100)]
    assert len(values) == len(set(values))
    assert all(0 <= value <= MAX_SAFE_INTEGER for value in values)
    recursive = []
    recursive.append(recursive)
    assert json_value(recursive) == ["<recursive>"]


def test_token_usage_supports_provider_metadata_shape() -> None:
    item = message(
        "ai",
        "done",
        usage_metadata=None,
        response_metadata={"token_usage": {"prompt_tokens": 3, "completion_tokens": 4}},
    )
    assert normalize_token_usage([item])["total_tokens"] == 7
