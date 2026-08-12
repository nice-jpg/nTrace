from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.tools import StructuredTool

from nTrace import NTrace, createNTraceEndMiddleware, createNTraceStartMiddleware
from nTrace.client import default_client
from nTrace.events import current_tool_exchange, json_value, normalize_token_usage, user_inputs
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
    start.before_model(state, None)
    end.before_model(state, None)

    assert [event["type"] for event in client.events] == ["start", "end"]
    assert client.events[0]["span_id"] == client.events[1]["span_id"]
    assert client.events[0]["tool_call_results"] == []
    assert client.events[0]["user_inputs"] == [
        {"type": "message", "role": "human", "content": "hello"},
        {"type": "message", "role": "tool", "content": "tool-result"},
        {"type": "message", "role": "ai", "content": "done"},
    ]
    assert client.events[1]["user_inputs"] == client.events[0]["user_inputs"]
    assert client.events[1]["tool_call_results"] == []
    assert client.events[1]["data"]["state"]["custom"] == {"page": 2}


def test_each_model_iteration_emits_a_distinct_host_span() -> None:
    client = MemoryClient()
    trace = NTrace(agent_name="main", client=client)
    start = createNTraceStartMiddleware(trace)
    end = createNTraceEndMiddleware(trace)
    trace.update(session_id="iterations")

    for content in ("first", "second"):
        state = {"messages": [message("human", content)]}
        start.before_model(state, None)
        end.before_model(state, None)

    assert [(item["sender"], item["type"]) for item in client.events] == [
        ("host", "start"),
        ("host", "end"),
        ("host", "start"),
        ("host", "end"),
    ]
    assert client.events[0]["span_id"] == client.events[1]["span_id"]
    assert client.events[2]["span_id"] == client.events[3]["span_id"]
    assert client.events[0]["span_id"] != client.events[2]["span_id"]


def test_llm_span_links_to_the_latest_host_model_preparation_span() -> None:
    client = MemoryClient()
    trace = NTrace(agent_name="main", client=client)
    start = createNTraceStartMiddleware(trace)
    end = createNTraceEndMiddleware(trace)
    trace.update(session_id="linked")
    state = {"messages": [message("human", "hello")]}
    request = SimpleNamespace(messages=[], system_prompt="s", system_message=None, tools=[], model_settings={})
    response = SimpleNamespace(result=[], structured_response=None)

    start.before_model(state, None)
    end.before_model(state, None)
    end.wrap_model_call(request, lambda _request: response)

    host_span_id = client.events[0]["span_id"]
    assert [item["sender"] for item in client.events] == ["host", "host", "llm", "llm"]
    assert client.events[2]["parent_span_id"] == host_span_id
    assert client.events[3]["parent_span_id"] == host_span_id


def test_model_middleware_captures_actual_request_response_and_usage() -> None:
    client = MemoryClient()
    trace = NTrace(agent_name="main", client=client)
    middleware = createNTraceEndMiddleware(trace)
    search_tool = StructuredTool.from_function(
        func=lambda q: q,
        name="search",
        description="Search for matching records",
    )
    request = SimpleNamespace(
        messages=[message("human", "actual input")],
        system_prompt="actual system",
        system_message=None,
        tools=[search_tool],
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
    assert client.events[0]["tools"] == [{
        "name": "search",
        "description": "Search for matching records",
    }]
    assert client.events[0]["user_inputs"] == [
        {"type": "message", "role": "human", "content": "actual input"}
    ]
    assert client.events[1]["output"] == "answer"
    assert client.events[1]["token_usage"]["total_tokens"] == 13
    assert client.events[1]["tools_called"][0]["name"] == "search"


def test_async_model_call_is_traced() -> None:
    client = MemoryClient()
    trace = NTrace(agent_name="main", client=client)
    start = createNTraceStartMiddleware(trace)
    middleware = createNTraceEndMiddleware(trace)
    request = SimpleNamespace(messages=[], system_prompt="s", system_message=None, tools=[], model_settings={})
    response = SimpleNamespace(result=[], structured_response=None)

    async def run():
        async def handler(_request):
            return response
        trace.update(session_id="async-test")
        await start.abefore_model({"messages": []}, None)
        await middleware.abefore_model({"messages": []}, None)
        return await middleware.awrap_model_call(request, handler)

    assert asyncio.run(run()) is response
    assert [(event["sender"], event["type"]) for event in client.events] == [
        ("host", "start"),
        ("host", "end"),
        ("llm", "start"),
        ("llm", "end"),
    ]


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


def test_user_input_boundary_is_kept_when_a_trace_resumes() -> None:
    client = MemoryClient()
    trace = NTrace(agent_name="main", client=client)
    first = trace.update(session_id="session", user_input_boundary=True)
    resumed = trace.update(session_id="session", resume=True)

    assert resumed is first
    binding, span_id = trace.begin_host_span()
    trace.emit(
        sender="host",
        event_type="start",
        span_id=span_id,
        parent_span_id=binding.parent_span_id,
        system_prompt="",
        user_inputs=[],
    )
    assert client.events[0]["data"]["trace_boundary"] == "user_input"


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


def test_inputs_keep_message_reasoning_and_function_call_types() -> None:
    inputs = user_inputs([
        message("human", "hello"),
        message(
            "ai",
            "",
            additional_kwargs={
                "reasoning_items": [{
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Inspecting the page"}],
                }]
            },
            tool_calls=[{"id": "call-1", "name": "search", "args": {"q": "x"}}],
        ),
    ])

    assert inputs == [
        {"type": "message", "role": "human", "content": "hello"},
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "Inspecting the page"}],
        },
        {"type": "function_call", "name": "search", "arguments": {"q": "x"}},
    ]


def test_current_tool_exchange_excludes_previous_tool_turns() -> None:
    messages = [
        message("ai", "", tool_calls=[{"id": "old", "name": "old_tool", "args": {}}]),
        message("tool", "old result", tool_call_id="old"),
        message("ai", "", tool_calls=[{"id": "new", "name": "new_tool", "args": {"value": 2}}]),
        message("tool", "new result", tool_call_id="new"),
    ]

    calls, results = current_tool_exchange(messages)

    assert [call["name"] for call in calls] == ["new_tool"]
    assert [result["content"] for result in results] == ["new result"]


def test_host_end_emits_only_the_current_tool_exchange() -> None:
    client = MemoryClient()
    trace = NTrace(agent_name="main", client=client)
    start = createNTraceStartMiddleware(trace)
    end = createNTraceEndMiddleware(trace)
    state = {"messages": [
        message("ai", "", tool_calls=[{"id": "old", "name": "old_tool", "args": {}}]),
        message("tool", "old result", tool_call_id="old"),
        message("ai", "", tool_calls=[{"id": "new", "name": "new_tool", "args": {}}]),
        message("tool", "new result", tool_call_id="new"),
    ]}

    trace.update(session_id="current-tools")
    start.before_model(state, None)
    end.before_model(state, None)

    assert [call["name"] for call in client.events[1]["tools_called"]] == ["new_tool"]
    assert [result["content"] for result in client.events[1]["tool_call_results"]] == ["new result"]
