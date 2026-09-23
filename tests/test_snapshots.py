from __future__ import annotations

import base64
import gzip
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from nTrace.client import NTraceClient
from nTrace.server import create_app
from nTrace.snapshots import REF, digest, encode_events, image_paths, resolve_event
from nTrace.storage import TraceStorage
from nTrace.tests.test_client import Reply
from nTrace.tests.test_storage import event


@pytest.fixture
def transport(tmp_path, monkeypatch):
    app = create_app(database_path=tmp_path / "snapshots.sqlite3", static_dir=tmp_path / "missing")
    packets = []
    with TestClient(app) as api:
        def post(req, **kwargs):
            body = gzip.decompress(req.data) if req.get_header("Content-encoding") == "gzip" else req.data
            packets.append(json.loads(body))
            response = api.post(urlsplit(req.full_url).path, content=req.data, headers=dict(req.header_items()))
            if response.status_code != 200:
                raise HTTPError(req.full_url, response.status_code, "rejected", {}, None)
            return Reply()
        monkeypatch.setattr("nTrace.client.request.urlopen", post)
        client = NTraceClient(enabled=True, max_retries=0)
        try:
            yield client, api, app.state.storage, packets
        finally:
            client.close()


def test_only_new_context_is_uploaded_and_details_are_reconstructed(transport):
    client, api, storage, packets = transport
    old = "old context " * 30_000
    inputs = [{"type": "message", "content": old}]
    assert client._post([event(user_inputs=inputs, data={"messages": inputs})])
    assert client._post([event("end", user_inputs=inputs + ["new input"], data={"messages": inputs})])
    assert old not in json.dumps(packets[1])
    assert "new input" in json.dumps(packets[1])
    assert len(json.dumps(packets[1])) < len(json.dumps(packets[0])) / 100
    assert set(packets[0]["objects"]).isdisjoint(packets[1]["objects"])
    assert api.get("/api/v1/traces/101/spans/201").json()["user_inputs"] == inputs + ["new input"]
    assert api.get("/api/v1/traces/101/spans/201/user-inputs").json()["items"] == ["new input", *inputs]
    assert storage.get_span(101, 201)["data"]["messages"] == inputs
    # The large text is stored once, even across fields and start/end events.
    assert storage._connection.execute("SELECT COUNT(*) FROM snapshots WHERE snapshot_id=?", (digest(old),)).fetchone()[0] == 1
    payloads = storage._connection.execute("SELECT payload_json FROM events").fetchall()
    assert all(old not in row[0] for row in payloads)
    timeline = api.get("/api/v1/traces/101/timeline").json()
    assert "user_inputs" not in timeline["spans"][0]


def test_failed_packet_is_not_acknowledged(transport, monkeypatch):
    client, api, storage, packets = transport
    from nTrace.client import request
    actual = request.urlopen
    def fail(req, **kwargs):
        raise URLError(TimeoutError())
    monkeypatch.setattr(request, "urlopen", fail)
    content = "history" * 1000
    assert not client._post([event(user_inputs=[content])])
    assert not client._known_snapshots
    monkeypatch.setattr(request, "urlopen", actual)
    assert client._post([event("end", user_inputs=[content])])
    assert storage.get_span(101, 201)["user_inputs"] == [content]


def test_deleted_snapshots_are_restored_after_409(transport):
    client, api, storage, packets = transport
    content = "persistent context" * 1000
    assert client._post([event(user_inputs=[content])])
    assert api.delete("/api/v1/traces/101").status_code == 200
    assert storage._connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    assert client._post([event(user_inputs=[content])])
    assert len(packets) == 3  # Initial upload, stale references, automatic repair.
    assert packets[1]["objects"] == {}
    assert packets[2]["objects"]
    assert storage.get_span(101, 201)["user_inputs"] == [content]


def test_snapshot_restart_and_shared_trace_deletion(tmp_path):
    path = tmp_path / "db.sqlite3"
    content = "shared" * 1000
    storage = TraceStorage(path)
    storage.put_events([event(user_inputs=[content], data={"trace_boundary": "user_input"}),
                        event(trace_id=102, user_inputs=[content], data={"trace_boundary": "user_input"})])
    storage.close()
    storage = TraceStorage(path)
    try:
        assert storage.delete_trace(101)
        assert storage.get_span(102, 201)["user_inputs"] == [content]
        assert storage.delete_trace(102)
        assert storage._connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    finally:
        storage.close()


def test_invalid_or_missing_snapshot_is_atomic(transport):
    _, api, storage, _ = transport
    encoded, objects = encode_events([event(user_inputs=["context" * 100])])
    packet = {"snapshot_version": 1, "events": encoded, "objects": {}}
    assert api.post("/api/v1/snapshot-events", json=packet).status_code == 409
    packet["objects"] = {**objects, "bad hash": "value"}
    assert api.post("/api/v1/snapshot-events", json=packet).status_code == 422
    assert storage.list_traces() == []
    assert storage._connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0


def test_images_are_local_paths_on_wire_and_in_history(transport, monkeypatch, tmp_path):
    client, _, storage, packets = transport
    directory = tmp_path / "images"
    monkeypatch.setenv("NTRACE_IMAGE_DIR", str(directory))
    pixels = b"\x89PNG\r\n\x1a\n" + b"sample image bytes" * 100
    encoded = base64.b64encode(pixels).decode()
    url = "data:image/png;base64," + encoded
    image = {"type": "image_url", "image_url": {"url": url}}
    assert client.emit(event(user_inputs=[image], data={"messages": [image]}))
    assert client.flush(3)
    assert encoded not in json.dumps(packets)
    assert "data:image" not in json.dumps(packets)
    files = list(directory.iterdir())
    assert len(files) == 1 and files[0].read_bytes() == pixels
    assert storage.get_span(101, 201)["user_inputs"][0]["image_url"]["url"] == str(files[0])
    assert image["image_url"]["url"] == url  # Never mutate the model's input.
    assert image_paths({"type": "base64", "media_type": "image/png", "data": encoded})["path"] == str(files[0])
    assert image_paths("/existing/screenshot.png") == "/existing/screenshot.png"


def test_rewritten_history_and_literal_reference_dict_round_trip():
    for messages in (["first", "second"], ["changed"], [], [{REF: "literal user data"}]):
        source = event(user_inputs=messages)
        encoded, objects = encode_events([source])
        assert resolve_event(encoded[0], objects.__getitem__) == source


def test_bad_image_does_not_drop_timing_event(transport, monkeypatch, tmp_path):
    client, _, storage, packets = transport
    monkeypatch.setenv("NTRACE_IMAGE_DIR", str(tmp_path / "images"))
    assert client._post([event(user_inputs=["data:image/png;base64,not valid!"])])
    assert storage.get_span(101, 201)["user_inputs"] == [
        {"type": "image_path", "path": None, "unavailable": True}
    ]
    assert "not valid!" not in json.dumps(packets)


def test_duplicate_packet_and_websocket_stay_idempotent(transport):
    client, api, storage, packets = transport
    with api.websocket_connect("/api/v1/stream") as socket:
        socket.receive_json()
        assert client._post([event(user_inputs=["same" * 100])])
        streamed = socket.receive_json()
        assert streamed["kind"] == "event.created"
        assert "user_inputs" not in streamed["event"]
    assert client._post([event(user_inputs=["same" * 100])])
    assert packets[-1]["objects"] == {}
    assert len(storage.get_trace(101)["events"]) == 1


def test_old_inline_records_are_still_readable(tmp_path):
    storage = TraceStorage(tmp_path / "legacy.sqlite3")
    original = event(user_inputs=["legacy inline context"])
    try:
        storage.put_events([original])
        storage._connection.execute("UPDATE events SET payload_json=?", (json.dumps(original),))
        storage._connection.commit()
        assert storage.get_span(101, 201)["user_inputs"] == original["user_inputs"]
    finally:
        storage.close()
