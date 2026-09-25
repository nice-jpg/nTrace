from datetime import datetime, timezone

from fastapi.testclient import TestClient

from nTrace.server import create_app
from nTrace.storage import TraceStorage
from nTrace.tests.test_storage import event


def test_expiry_uses_start_time_and_preserves_favorites_and_shared_snapshots(tmp_path):
    storage = TraceStorage(tmp_path / "trace.sqlite3")
    try:
        for trace_id, timestamp in [(101, "2026-01-01T00:00:00Z"), (102, "2026-01-01T00:00:00Z"),
                                    (103, "2026-01-01T00:00:01Z")]:
            storage.put_events([event(trace_id=trace_id, timestamp=timestamp,
                user_inputs=["shared context" * 100], data={"trace_boundary": "user_input"})])
        storage._connection.execute("UPDATE traces SET favorite=1 WHERE trace_id=102")
        storage._connection.execute("UPDATE traces SET updated_at='2026-01-04T00:00:00Z' WHERE trace_id=101")
        storage._connection.commit()
        assert storage.prune_expired(now=datetime(2026, 1, 4, tzinfo=timezone.utc)) == [101]
        assert storage.get_trace(101) is None
        assert storage.get_span(102, 201)["user_inputs"] == ["shared context" * 100]
        assert storage.get_trace(103) is not None
        assert storage._connection.execute("SELECT 1 FROM trace_contexts WHERE source_trace_id=101").fetchone() is None
    finally:
        storage.close()


def test_startup_prunes_old_traces(tmp_path):
    path = tmp_path / "trace.sqlite3"
    storage = TraceStorage(path)
    storage.put_events([event()])
    storage.close()
    with TestClient(create_app(database_path=path, static_dir=tmp_path / "missing")) as api:
        assert api.get("/api/v1/traces").json()["traces"] == []


def test_periodic_cleanup_broadcasts_deletion(tmp_path, monkeypatch):
    monkeypatch.setattr("nTrace.server.RETENTION_INTERVAL_SECONDS", 0.02)
    with TestClient(create_app(database_path=tmp_path / "trace.sqlite3", static_dir=tmp_path / "missing")) as api:
        with api.websocket_connect("/api/v1/stream") as socket:
            socket.receive_json()
            api.post("/api/v1/events", json=event())
            messages = [socket.receive_json(), socket.receive_json()]
            assert any(message == {"kind": "trace.deleted", "trace_id": 101} for message in messages)
            assert api.get("/api/v1/traces/101").status_code == 404


def test_tool_display_is_lightweight_in_snapshot_and_stream(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "trace.sqlite3", static_dir=tmp_path / "missing")) as api:
        with api.websocket_connect("/api/v1/stream") as socket:
            socket.receive_json()
            start = event(sender="tool", tools_called=[{"name": "search", "args": {"q": "secret"}}])
            assert api.post("/api/v1/events", json=start).status_code == 200
            incoming = socket.receive_json()["event"]
            assert incoming["tool_name"] == "search"
            assert "tools_called" not in incoming
            end = event("end", sender="tool", tool_call_results=[{"status": "error", "content": "failure"}])
            api.post("/api/v1/events", json=end)
            assert socket.receive_json()["event"]["tool_error"] is True
        span = api.get("/api/v1/traces/101/timeline").json()["spans"][0]
        assert span["tool_name"] == "search" and span["tool_error"]
        assert "tool_call_results" not in span


def test_old_tool_display_is_backfilled_and_exception_is_red(tmp_path):
    storage = TraceStorage(tmp_path / "trace.sqlite3")
    try:
        storage.put_events([event(sender="tool", tools_called=[{"name": "echo"}]),
                            event("end", sender="tool", data={"error_type": "RuntimeError"})])
        storage._connection.execute("UPDATE events SET tool_display_json=NULL")
        storage._connection.commit()
        span = storage.get_trace_timeline(101)["spans"][0]
        assert span["tool_name"] == "echo" and span["tool_error"]
        assert storage._connection.execute("SELECT COUNT(*) FROM events WHERE tool_display_json IS NULL").fetchone()[0] == 0
    finally:
        storage.close()
