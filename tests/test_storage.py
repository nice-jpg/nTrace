from __future__ import annotations

from nTrace.storage import TraceStorage


def event(event_type="start", **overrides):
    payload = {
        "schema_version": 1,
        "trace_id": 101,
        "span_id": 201,
        "parent_span_id": None,
        "agent_id": 1,
        "parent_agent_id": None,
        "agent_name": "main",
        "activation_order": 1,
        "sender": "host",
        "type": event_type,
        "timestamp": "2026-01-01T00:00:00.000Z" if event_type == "start" else "2026-01-01T00:00:02.500Z",
        "system_prompt": "system",
        "user_inputs": ["hello"],
        "output": None,
        "tools": [],
        "tools_called": [],
        "tool_call_results": [],
        "token_usage": {},
        "data": {},
    }
    payload.update(overrides)
    return payload


def test_storage_is_idempotent_persistent_and_assembles_out_of_order(tmp_path) -> None:
    path = tmp_path / "trace.sqlite3"
    storage = TraceStorage(path)
    storage.put_events([event("end", output="done"), event("start")])
    storage.put_events([event("start")])
    detail = storage.get_trace(101)
    assert detail is not None
    assert len(detail["events"]) == 2
    assert detail["spans"][0]["duration_ms"] == 2500
    assert detail["spans"][0]["output"] == "done"
    assert detail["status"] == "completed"
    assert detail["started_at"] == "2026-01-01T00:00:00.000Z"
    assert detail["updated_at"] == "2026-01-01T00:00:02.500Z"
    storage.close()

    reopened = TraceStorage(path)
    assert reopened.get_trace(101)["spans"][0]["duration_ms"] == 2500
    reopened.close()


def test_start_only_span_remains_running(tmp_path) -> None:
    storage = TraceStorage(tmp_path / "trace.sqlite3")
    storage.put_events([event("start")])
    span = storage.get_trace(101)["spans"][0]
    assert span["running"] is True
    assert span["ended_at"] is None
    storage.close()


def test_server_infers_child_trace_from_active_host_timing(tmp_path) -> None:
    storage = TraceStorage(tmp_path / "trace.sqlite3")
    root_start = event("start", trace_id=101, span_id=201, agent_name="conductor")
    child_start = event(
        "start",
        trace_id=102,
        span_id=301,
        agent_name="collector",
        timestamp="2026-01-01T00:00:01.000Z",
    )
    child_end = event(
        "end",
        trace_id=102,
        span_id=301,
        agent_name="collector",
        timestamp="2026-01-01T00:00:02.000Z",
    )
    root_end = event(
        "end",
        trace_id=101,
        span_id=201,
        agent_name="conductor",
        timestamp="2026-01-01T00:00:03.000Z",
    )

    stored = storage.put_events([root_start, child_start, child_end, root_end])
    detail = storage.get_trace(101)

    assert [item["trace_id"] for item in stored] == [101, 101, 101, 101]
    assert detail is not None
    assert [agent["agent_id"] for agent in detail["agents"]] == [1, 2]
    child_span = next(span for span in detail["spans"] if span["span_id"] == 301)
    assert child_span["agent_id"] == 2
    assert child_span["parent_agent_id"] == 1
    assert child_span["parent_span_id"] == 201
    assert child_span["data"]["source_trace_id"] == 102
    assert storage.get_trace(102) is None
    assert detail["status"] == "completed"
    storage.close()


def test_inferred_context_survives_storage_restart(tmp_path) -> None:
    path = tmp_path / "trace.sqlite3"
    storage = TraceStorage(path)
    storage.put_events(
        [
            event("start", trace_id=101, span_id=201),
            event(
                "start",
                trace_id=102,
                span_id=301,
                agent_name="collector",
                timestamp="2026-01-01T00:00:01.000Z",
            ),
        ]
    )
    storage.close()

    reopened = TraceStorage(path)
    stored = reopened.put_events(
        [
            event(
                "end",
                trace_id=102,
                span_id=301,
                agent_name="collector",
                timestamp="2026-01-01T00:00:02.000Z",
            )
        ]
    )
    assert stored[0]["trace_id"] == 101
    assert stored[0]["agent_id"] == 2
    assert reopened.get_trace(101)["spans"][1]["running"] is False
    reopened.close()
