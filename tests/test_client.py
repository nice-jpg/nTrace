from __future__ import annotations

import json
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pytest

from nTrace.client import NTraceClient
from nTrace.server import create_app
from nTrace.tests.test_storage import event


class Reply:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture
def receiver(tmp_path):
    app = create_app(database_path=tmp_path / "trace.sqlite3", static_dir=tmp_path / "missing")
    with TestClient(app) as api:
        yield app.state.storage, api


def test_large_context_survives_proxy_body_limit(monkeypatch, receiver):
    """Large starts must arrive along with smaller ends, without losing data."""
    storage, api = receiver
    requests = []

    def proxy(req, **kwargs):
        requests.append(len(req.data))
        if len(req.data) > 1024 * 1024:
            raise HTTPError(req.full_url, 413, "Request Entity Too Large", {}, None)
        response = api.post(urlsplit(req.full_url).path, content=req.data, headers=dict(req.header_items()))
        assert response.status_code == 200, response.text
        return Reply()

    monkeypatch.setattr("nTrace.client.request.urlopen", proxy)
    context = "x" * 600_000
    client = NTraceClient(enabled=True, max_retries=0)
    packets = [
        # Whole host pair can exceed the proxy limit when batched together.
        [event("start", span_id=10, user_inputs=[context]), event("end", span_id=10, user_inputs=[context])],
        [event("start", span_id=20, user_inputs=["x" * 400_000], data={"state": "x" * 400_000})],
        [event("end", span_id=20, user_inputs=[context], data={"state": context})],
    ]
    for span_id in (21, 22):
        packets.extend([
            [event("start", span_id=span_id, sender="llm", parent_span_id=20,
                   user_inputs=[context], data={"messages": context})],
            [event("end", span_id=span_id, sender="llm", parent_span_id=20,
                   user_inputs=[context], data={"response": "done"})],
        ])
    try:
        outcomes = [client._post(packet) for packet in packets]
        assert all(outcomes), (outcomes, requests)
        detail = storage.get_trace(101)
        assert len(detail["events"]) == 8
        assert len(detail["spans"]) == 4
        assert all(span["duration_ms"] == 2500 and not span["running"] for span in detail["spans"])
        assert storage.get_span(101, 21)["start_event"]["data"]["messages"] == context
    finally:
        client.close()


def test_flush_waits_for_in_flight_upload(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    client = NTraceClient(enabled=True)

    def post(events):
        entered.set()
        release.wait(2)
        return True

    monkeypatch.setattr(client, "_post", post)
    try:
        client.emit(event())
        assert entered.wait(1)
        assert client.flush(0.02) is False
        release.set()
        assert client.flush(1) is True
    finally:
        release.set()
        client.close()


def test_remote_upload_has_configurable_timeout(monkeypatch):
    monkeypatch.delenv("NTRACE_REQUEST_TIMEOUT", raising=False)
    observed = []

    def slow_receiver(req, *, timeout):
        observed.append(timeout)
        if timeout < 2:
            raise TimeoutError("remote upload exceeds one second")
        return Reply()

    monkeypatch.setattr("nTrace.client.request.urlopen", slow_receiver)
    client = NTraceClient(enabled=True, max_retries=0)
    try:
        assert client._post([event()])
        assert observed == [30.0]
        assert client.dropped_events == 0
    finally:
        client.close()
    monkeypatch.setenv("NTRACE_REQUEST_TIMEOUT", "90")
    assert NTraceClient(enabled=False).request_timeout == 90
    assert NTraceClient(enabled=False, request_timeout=5).request_timeout == 5


@pytest.mark.parametrize("value", ["invalid", "nan", "inf", "0", "-1"])
def test_invalid_timeout_environment_is_fail_open(monkeypatch, value, caplog):
    monkeypatch.setenv("NTRACE_REQUEST_TIMEOUT", value)
    assert NTraceClient(enabled=False).request_timeout == 30
    assert "NTRACE_REQUEST_TIMEOUT" in caplog.text


@pytest.mark.parametrize("reason", [TimeoutError("private content"), ConnectionRefusedError(61, "private content")])
def test_url_error_logs_underlying_cause(monkeypatch, reason, caplog):
    def fail(req, **kwargs):
        raise URLError(reason)

    monkeypatch.setattr("nTrace.client.request.urlopen", fail)
    client = NTraceClient(enabled=True, max_retries=0)
    try:
        assert not client._post([event()])
        assert f"reason_type={type(reason).__name__}" in caplog.text
        assert "timeout_s=" in caplog.text
        assert "elapsed_s=" in caplog.text
        assert "raw_bytes=" in caplog.text
        assert "private content" not in caplog.text
    finally:
        client.close()


def test_413_splits_batch_and_preserves_other_events(monkeypatch, caplog):
    received = []
    attempts = []

    def proxy(req, **kwargs):
        events = json.loads(req.data)["events"]
        attempts.append([item["span_id"] for item in events])
        if len(events) > 1 or events[0]["span_id"] == 2:
            raise HTTPError(req.full_url, 413, "Too large", {}, None)
        received.extend(events)
        return Reply()

    monkeypatch.setattr("nTrace.client.request.urlopen", proxy)
    client = NTraceClient(enabled=True, max_retries=2)
    try:
        assert not client._post([event(span_id=i) for i in (1, 2, 3)])
        assert [item["span_id"] for item in received] == [1, 3]
        assert attempts.count([2]) == 1  # Retrying the same oversized body cannot help.
        assert client.dropped_events == 1
        assert "status=413" in caplog.text
        assert "client_max_body_size" in caplog.text
        assert "hello" not in caplog.text
    finally:
        client.close()


def test_encoded_byte_limit_splits_before_post(monkeypatch):
    monkeypatch.setattr("nTrace.client.MAX_BATCH_BYTES", 1100)
    sizes = []
    received = []

    def proxy(req, **kwargs):
        sizes.append(len(req.data))
        received.extend(item["span_id"] for item in json.loads(req.data)["events"])
        return Reply()

    monkeypatch.setattr("nTrace.client.request.urlopen", proxy)
    client = NTraceClient(enabled=True)
    try:
        assert client._post([event(span_id=i) for i in range(4)])
        assert len(sizes) >= 2
        assert received == list(range(4))
        assert max(sizes) <= 1100
    finally:
        client.close()


def test_failed_delivery_is_counted_without_raising(monkeypatch, caplog):
    attempts = []

    def unavailable(req, **kwargs):
        attempts.append(req)
        raise TimeoutError("sensitive transport detail")

    monkeypatch.setattr("nTrace.client.request.urlopen", unavailable)
    client = NTraceClient(enabled=True, max_retries=1)
    try:
        client.emit(event())
        assert client.flush(2)
        assert len(attempts) == 2
        assert client.failed_batches == 1
        assert client.dropped_events == 1
        assert "TimeoutError" in caplog.text
        assert "sensitive transport detail" not in caplog.text
    finally:
        client.close()


def test_invalid_event_does_not_kill_worker_or_drop_neighbors(monkeypatch):
    received = []

    def post(req, **kwargs):
        received.extend(json.loads(req.data)["events"])
        return Reply()

    monkeypatch.setattr("nTrace.client.request.urlopen", post)
    client = NTraceClient(enabled=True)
    try:
        assert not client._post([event(span_id=1, data={"bad": object()}), event(span_id=2)])
        client.emit(event(span_id=3))
        assert client.flush(1)
        assert [item["span_id"] for item in received] == [2, 3]
        assert client.dropped_events == 1
    finally:
        client.close()


def test_raw_byte_limit_splits_highly_compressible_batch(monkeypatch, receiver):
    storage, api = receiver
    monkeypatch.setattr("nTrace.client.MAX_RAW_BATCH_BYTES", 100_000)
    counts = []

    def proxy(req, **kwargs):
        response = api.post(urlsplit(req.full_url).path, content=req.data, headers=dict(req.header_items()))
        assert response.status_code == 200
        counts.append(response.json()["accepted"])
        return Reply()

    monkeypatch.setattr("nTrace.client.request.urlopen", proxy)
    client = NTraceClient(enabled=True)
    try:
        assert client._post([event(span_id=i, user_inputs=[str(i) * 80_000]) for i in range(4)])
        assert counts == [1, 1, 1, 1]
        assert len(storage.get_trace(101)["events"]) == 4
    finally:
        client.close()
