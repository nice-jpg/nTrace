from __future__ import annotations

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from src.agent import AgentRuntime


class MemoryClient:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event):
        self.events.append(event)
        return True


class ToolFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_real_bines_runtime_emits_host_and_llm_spans() -> None:
    client = MemoryClient()
    model = ToolFakeModel(responses=[AIMessage(content="done")])
    runtime = AgentRuntime(model=model, tools=[], name="conductor")
    runtime.agent._ntrace.client = client

    result = runtime.run_turn([{"role": "user", "content": "hello"}], session_id="trace-test")

    assert result.output == "done"
    assert [(event["sender"], event["type"]) for event in client.events] == [
        ("host", "start"),
        ("host", "end"),
        ("llm", "start"),
        ("llm", "end"),
    ]
    assert len({event["trace_id"] for event in client.events}) == 1
    assert client.events[0]["user_inputs"] == [
        {"type": "message", "role": "human", "content": "hello"}
    ]
    assert client.events[0]["data"]["session_id"] == "trace-test"
    assert client.events[0]["data"]["trace_boundary"] == "user_input"
    assert runtime.agent._ntrace.trace_id == client.events[0]["trace_id"]


def test_run_turn_replaces_active_trace_while_resume_update_reuses_it() -> None:
    client = MemoryClient()
    model = ToolFakeModel(responses=[AIMessage(content="first"), AIMessage(content="second")])
    runtime = AgentRuntime(model=model, tools=[], name="conductor")
    runtime.agent._ntrace.client = client

    runtime.run_turn([{"role": "user", "content": "one"}], session_id="session")
    first = runtime.agent._ntrace.trace_id
    runtime.agent._ntrace.update(session_id="session", resume=True)
    assert runtime.agent._ntrace.trace_id == first

    runtime.run_turn([{"role": "user", "content": "two"}], session_id="session")
    assert runtime.agent._ntrace.trace_id != first
