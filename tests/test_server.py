from __future__ import annotations

from fastapi.testclient import TestClient

from nTrace.server import create_app
from nTrace.tests.test_storage import event


def test_api_persists_lists_and_streams_events(tmp_path) -> None:
    app = create_app(database_path=tmp_path / "server.sqlite3", static_dir=tmp_path / "missing")
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/stream") as websocket:
            assert websocket.receive_json()["kind"] == "stream.ready"
            response = client.post("/api/v1/events", json={"events": [event("start")]})
            assert response.status_code == 200
            assert response.json()["accepted"] == 1
            streamed = websocket.receive_json()
            assert streamed["kind"] == "event.created"
            assert streamed["event"]["span_id"] == 201

        traces = client.get("/api/v1/traces").json()["traces"]
        assert traces[0]["trace_id"] == 101
        detail = client.get("/api/v1/traces/101").json()
        assert detail["spans"][0]["running"] is True
        timeline = client.get("/api/v1/traces/101/timeline").json()
        assert "system_prompt" not in timeline["events"][0]
        span = client.get("/api/v1/traces/101/spans/201").json()
        assert span["system_prompt"] == "system"
        span_details = client.get("/api/v1/traces/101/spans/201/details").json()
        assert span_details["system_prompt"] == "system"
        assert "user_inputs" not in span_details
        inputs = client.get("/api/v1/traces/101/spans/201/user-inputs?offset=0&limit=10").json()
        assert inputs == {
            "items": ["hello"],
            "offset": 0,
            "limit": 10,
            "total": 1,
            "has_more": False,
        }
        assert client.get("/api/v1/traces/999").status_code == 404
        assert client.get("/api/v1/traces/999/spans/201/details").status_code == 404
        assert client.get("/api/v1/traces/999/spans/201/user-inputs").status_code == 404


def test_api_deletes_trace_and_broadcasts_removal(tmp_path) -> None:
    app = create_app(database_path=tmp_path / "server.sqlite3", static_dir=tmp_path / "missing")
    with TestClient(app) as client:
        client.post("/api/v1/events", json=event("start"))
        with client.websocket_connect("/api/v1/stream") as websocket:
            websocket.receive_json()
            response = client.delete("/api/v1/traces/101")
            message = websocket.receive_json()

        assert response.status_code == 200
        assert response.json() == {"deleted": 101}
        assert message == {"kind": "trace.deleted", "trace_id": 101}
        assert client.get("/api/v1/traces/101").status_code == 404
        assert client.delete("/api/v1/traces/101").status_code == 404


def test_api_accepts_single_event_and_validates_sender(tmp_path) -> None:
    app = create_app(database_path=tmp_path / "server.sqlite3", static_dir=tmp_path / "missing")
    with TestClient(app) as client:
        assert client.post("/api/v1/events", json=event("start")).status_code == 200
        invalid = event("start", sender="other")
        assert client.post("/api/v1/events", json=invalid).status_code == 422


def test_stream_broadcasts_server_inferred_child_context(tmp_path) -> None:
    app = create_app(database_path=tmp_path / "server.sqlite3", static_dir=tmp_path / "missing")
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/stream") as websocket:
            websocket.receive_json()
            client.post("/api/v1/events", json=event("start", trace_id=101, span_id=201))
            websocket.receive_json()
            client.post(
                "/api/v1/events",
                json=event(
                    "start",
                    trace_id=102,
                    span_id=301,
                    agent_name="collector",
                    timestamp="2026-01-01T00:00:01.000Z",
                ),
            )
            streamed = websocket.receive_json()["event"]

        assert streamed["trace_id"] == 101
        assert streamed["agent_id"] == 2
        assert streamed["parent_agent_id"] == 1
        assert streamed["parent_span_id"] == 201
        assert streamed["data"]["source_trace_id"] == 102
