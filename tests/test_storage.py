from __future__ import annotations

from nTrace.storage import TraceStorage, assemble_spans


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
    assert detail["status"] == "running"
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


def test_timeline_omits_heavy_payload_and_span_detail_loads_it_on_demand(tmp_path) -> None:
    storage = TraceStorage(tmp_path / "trace.sqlite3")
    storage.put_events(
        [
            event("start", system_prompt="large-system", user_inputs=["large-input"]),
            event(
                "end",
                system_prompt=None,
                output="large-output",
                token_usage={"total_tokens": 123},
                tool_call_results=[{"content": "large-result"}],
            ),
        ]
    )

    timeline = storage.get_trace_timeline(101)
    assert timeline is not None
    assert "system_prompt" not in timeline["events"][0]
    assert "user_inputs" not in timeline["events"][0]
    assert "output" not in timeline["events"][1]
    assert timeline["spans"][0]["token_usage"] == {"total_tokens": 123}

    span = storage.get_span(101, 201)
    assert span is not None
    assert span["system_prompt"] == "large-system"
    assert span["output"] == "large-output"
    assert span["tool_call_results"] == [{"content": "large-result"}]

    details = storage.get_span_details(101, 201)
    assert details is not None
    assert details["system_prompt"] == "large-system"
    assert "user_inputs" not in details
    assert "start_event" not in details
    assert "end_event" not in details
    storage.close()


def test_span_user_inputs_are_paged_newest_first(tmp_path) -> None:
    storage = TraceStorage(tmp_path / "trace.sqlite3")
    storage.put_events([event("start", user_inputs=[f"input-{index}" for index in range(25)])])

    first = storage.get_span_user_inputs(101, 201)
    assert first == {
        "items": [f"input-{index}" for index in range(24, 14, -1)],
        "offset": 0,
        "limit": 10,
        "total": 25,
        "has_more": True,
    }
    last = storage.get_span_user_inputs(101, 201, offset=20, limit=10)
    assert last == {
        "items": [f"input-{index}" for index in range(4, -1, -1)],
        "offset": 20,
        "limit": 10,
        "total": 25,
        "has_more": False,
    }
    storage.close()


def test_delete_trace_removes_events_agents_and_context(tmp_path) -> None:
    storage = TraceStorage(tmp_path / "trace.sqlite3")
    storage.put_events([event("start")])

    assert storage.delete_trace(101) is True
    assert storage.get_trace(101) is None
    assert storage.get_trace_timeline(101) is None
    assert storage.get_span(101, 201) is None
    assert storage.delete_trace(101) is False
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
    assert detail["status"] == "running"
    storage.close()


def test_user_input_boundary_starts_a_new_root_while_previous_host_is_active(tmp_path) -> None:
    storage = TraceStorage(tmp_path / "trace.sqlite3")
    stored = storage.put_events(
        [
            event("start", trace_id=101, span_id=201, agent_name="conductor"),
            event(
                "start",
                trace_id=102,
                span_id=301,
                agent_name="conductor",
                timestamp="2026-01-01T00:00:01.000Z",
                data={"trace_boundary": "user_input", "session_id": "same-chat"},
            ),
        ]
    )

    assert [item["trace_id"] for item in stored] == [101, 102]
    assert stored[1]["agent_id"] == 1
    assert stored[1]["parent_agent_id"] is None
    assert stored[1]["parent_span_id"] is None
    assert storage.get_trace(101) is not None
    assert storage.get_trace(102) is not None
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


def test_server_links_child_after_parent_host_span_has_ended(tmp_path) -> None:
    storage = TraceStorage(tmp_path / "trace.sqlite3")
    llm_end = event(
        "end",
        trace_id=101,
        span_id=202,
        parent_span_id=201,
        sender="llm",
        timestamp="2026-01-01T00:00:01.000Z",
        tools_called=[{"name": "prepare_collector", "args": {}}],
    )
    stored = storage.put_events(
        [
            event("start", trace_id=101, span_id=201),
            event("end", trace_id=101, span_id=201, timestamp="2026-01-01T00:00:00.100Z"),
            llm_end,
            event(
                "start",
                trace_id=102,
                span_id=301,
                agent_name="collector",
                timestamp="2026-01-01T00:00:01.100Z",
            ),
        ]
    )

    child = stored[-1]
    assert child["trace_id"] == 101
    assert child["agent_id"] == 2
    assert child["parent_span_id"] == 201
    storage.close()


def test_newer_tool_call_beats_stale_unfinished_host_when_linking_child(tmp_path) -> None:
    storage = TraceStorage(tmp_path / "trace.sqlite3")
    stored = storage.put_events(
        [
            event("start", trace_id=101, span_id=201, timestamp="2026-01-01T00:00:00.000Z"),
            event(
                "start",
                trace_id=201,
                span_id=401,
                timestamp="2026-01-01T00:00:10.000Z",
                data={"trace_boundary": "user_input", "session_id": "new-turn"},
            ),
            event("end", trace_id=201, span_id=401, timestamp="2026-01-01T00:00:10.100Z"),
            event(
                "end",
                trace_id=201,
                span_id=402,
                parent_span_id=401,
                sender="llm",
                timestamp="2026-01-01T00:00:11.000Z",
                tools_called=[{"name": "prepare_collector", "args": {}}],
            ),
            event(
                "start",
                trace_id=202,
                span_id=501,
                agent_name="collector",
                timestamp="2026-01-01T00:00:11.100Z",
            ),
        ]
    )

    child = stored[-1]
    assert child["trace_id"] == 201
    assert child["parent_span_id"] == 401
    assert child["parent_agent_id"] == 1
    assert storage.get_trace(201)["agents"][-1]["agent_name"] == "collector"
    storage.close()


def test_next_lane_start_implicitly_bounds_an_unfinished_span() -> None:
    spans = assemble_spans(
        [
            event("start", span_id=201, timestamp="2026-01-01T00:00:00.000Z"),
            event("start", span_id=202, timestamp="2026-01-01T00:00:05.000Z"),
            event("end", span_id=202, timestamp="2026-01-01T00:00:06.000Z"),
        ]
    )

    assert spans[0]["ended_at"] == "2026-01-01T00:00:05.000Z"
    assert spans[0]["duration_ms"] == 5000
    assert spans[0]["running"] is False
