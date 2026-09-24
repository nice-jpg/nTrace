import asyncio
import inspect

import pytest
from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from nTrace import (NTrace, NTraceMiddleware, HostTraceMiddleware, LLMTraceMiddleware, ToolTraceMiddleware,
                    createNTraceHostMiddleware, createNTraceLLMMiddleware, createNTraceToolMiddleware)
from nTrace.server import create_app
from nTrace.storage import TraceStorage, assemble_spans
from nTrace.tests.test_sdk import MemoryClient
from nTrace.tests.test_storage import event


class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@tool
def echo(value: str) -> str:
    """Echo a value."""
    return value


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_graph_three_stages(asynchronous):
    client = MemoryClient()
    trace = NTrace(agent_name="main", client=client)
    trace.update(session_id="three-stages")
    middleware = [factory(trace) for factory in (
        createNTraceHostMiddleware, createNTraceLLMMiddleware, createNTraceToolMiddleware)]
    assert inspect.isabstract(NTraceMiddleware)
    assert all(isinstance(stage, NTraceMiddleware) for stage in middleware)
    for stage in (HostTraceMiddleware, LLMTraceMiddleware, ToolTraceMiddleware):
        assert stage.__bases__ == (NTraceMiddleware,)
    model = Model(responses=[
        AIMessage(content="", tool_calls=[{"name": "echo", "args": {"value": "hello"}, "id": "call1"}]),
        AIMessage(content="done"),
    ])
    graph = create_agent(model=model, tools=[echo], middleware=middleware)
    inputs = {"messages": [{"role": "user", "content": "go"}]}
    result = asyncio.run(graph.ainvoke(inputs)) if asynchronous else graph.invoke(inputs)
    assert result["messages"][-1].content == "done"
    spans = assemble_spans(client.events)
    assert [span["sender"] for span in spans].count("host") == 2
    assert [span["sender"] for span in spans].count("llm") == 2
    assert [span["sender"] for span in spans].count("tool") == 1
    assert all(not span["running"] for span in spans)
    host = next(span for span in spans if span["sender"] == "host")
    assert "state" in host["data"] and not host["user_inputs"]
    tool_span = next(span for span in spans if span["sender"] == "tool")
    assert tool_span["tools_called"][0]["args"] == {"value": "hello"}
    assert tool_span["tool_call_results"][0]["content"] == "hello"
    assert not tool_span["user_inputs"]
    llm = next(span for span in spans if span["sender"] == "llm")
    assert llm["user_inputs"] and llm["output"]
    assert "messages" not in llm["data"] and not llm["tools"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_tool_failure_keeps_pair_and_original_exception(asynchronous):
    client = MemoryClient()
    trace = NTrace(agent_name="main", client=client)
    stage = createNTraceToolMiddleware(trace)
    request = ToolCallRequest(tool_call={"name": "echo", "args": {}, "id": "c1"},
                              tool=echo, state={}, runtime=None)
    error = RuntimeError("broken tool")
    def fail(request):
        raise error
    async def afail(request):
        raise error
    with pytest.raises(RuntimeError) as caught:
        if asynchronous:
            asyncio.run(stage.awrap_tool_call(request, afail))
        else:
            stage.wrap_tool_call(request, fail)
    assert caught.value is error
    assert [item["type"] for item in client.events] == ["start", "end"]
    assert client.events[-1]["data"]["error_type"] == "RuntimeError"


def test_tool_protocol_parent_link_and_concurrent_spans(tmp_path):
    app = create_app(database_path=tmp_path / "tool.sqlite3", static_dir=tmp_path / "missing")
    with TestClient(app) as api:
        events = [event("start", sender="tool", span_id=501),
                  event("start", sender="tool", span_id=502, timestamp="2026-01-01T00:00:01Z"),
                  event("start", trace_id=202, span_id=503, timestamp="2026-01-01T00:00:02Z")]
        assert api.post("/api/v1/events", json={"events": events}).status_code == 200
        snapshot = api.get("/api/v1/traces/101").json()
        spans = {span["span_id"]: span for span in snapshot["spans"]}
        assert spans[501]["running"] and spans[502]["running"]
        assert spans[503]["parent_span_id"] == 502


def test_existing_sender_constraint_migrates_without_losing_events(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    storage = TraceStorage(path)
    storage.put_events([event()])
    connection = storage._connection
    schema = connection.execute("SELECT sql FROM sqlite_master WHERE name='events'").fetchone()[0]
    with connection:
        connection.execute("ALTER TABLE events RENAME TO legacy_events")
        connection.execute(schema.replace("'host', 'llm', 'tool'", "'host', 'llm'"))
        connection.execute("INSERT INTO events SELECT * FROM legacy_events")
        connection.execute("DROP TABLE legacy_events")
        connection.execute("CREATE INDEX events_trace_time_idx ON events(trace_id, timestamp)")
    storage.close()
    reopened = TraceStorage(path)
    try:
        assert reopened.get_span(101, 201)["user_inputs"] == ["hello"]
        reopened.put_events([event(sender="tool", span_id=202)])
        assert reopened.get_span(101, 202)["sender"] == "tool"
        assert reopened._connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert reopened._connection.execute("SELECT name FROM sqlite_master WHERE name='events_trace_time_idx'").fetchone()
    finally:
        reopened.close()
