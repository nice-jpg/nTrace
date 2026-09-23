"""Non-blocking HTTP client for nTrace event packets."""

from __future__ import annotations

import atexit
from collections import OrderedDict
import gzip
from functools import lru_cache
import json
import logging
import math
import os
import queue
import threading
import time
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from .snapshots import encode_events, image_paths

LOGGER = logging.getLogger("nTrace.client")
MAX_BATCH_BYTES = 512 * 1024
MAX_RAW_BATCH_BYTES = 8 * 1024 * 1024
COMPRESS_THRESHOLD = 64 * 1024
DEFAULT_REQUEST_TIMEOUT = 30.0


class NTraceClient:
    """Send events on a bounded background queue without blocking agent work."""

    def __init__(
        self,
        server_url: str | None = None,
        *,
        enabled: bool | None = None,
        queue_size: int = 2_000,
        batch_size: int = 64,
        flush_interval: float = 0.05,
        request_timeout: float | None = None,
        max_retries: int = 2,
    ) -> None:
        self.server_url = (server_url or os.getenv("NTRACE_SERVER_URL") or "http://127.0.0.1:8765").rstrip("/")
        if enabled is None:
            enabled = os.getenv("NTRACE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
        self.enabled = bool(enabled)
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.01, float(flush_interval))
        if request_timeout is None:
            try:
                request_timeout = float(os.getenv("NTRACE_REQUEST_TIMEOUT", str(DEFAULT_REQUEST_TIMEOUT)))
                if not math.isfinite(request_timeout) or request_timeout <= 0:
                    raise ValueError("Timeout must be positive and finite")
            except ValueError:
                LOGGER.warning("Invalid NTRACE_REQUEST_TIMEOUT; using %s seconds", DEFAULT_REQUEST_TIMEOUT)
                request_timeout = DEFAULT_REQUEST_TIMEOUT
        self.request_timeout = max(0.05, float(request_timeout))
        self.max_retries = max(0, int(max_retries))
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.dropped_events = 0
        self.failed_batches = 0
        self._known_snapshots: OrderedDict[str, None] = OrderedDict()
        atexit.register(self.close)

    def emit(self, event: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        self._ensure_worker()
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            self.dropped_events += 1
            if self.dropped_events in {1, 10, 100} or self.dropped_events % 1_000 == 0:
                LOGGER.warning("nTrace queue full; dropped_events=%s", self.dropped_events)
            return False

    def flush(self, timeout: float = 1.0) -> bool:
        """Wait for queued and in-flight events to finish their delivery attempts."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True

    def close(self) -> None:
        self.flush(0.5)
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.5)

    def _ensure_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="ntrace-sender", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                first = self._queue.get(timeout=self.flush_interval)
            except queue.Empty:
                continue
            batch = [first]
            while len(batch) < self.batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                if not self._post(batch):
                    self.failed_batches += 1
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _post(self, events: list[dict[str, Any]], *, _restore: bool = False) -> bool:
        try:
            events = image_paths(events)
            encoded, objects = encode_events(events)
            packet = {"events": encoded}
            if objects:
                packet.update(snapshot_version=1, objects={
                    key: node for key, node in objects.items() if key not in self._known_snapshots
                })
            body = json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, OSError, RecursionError):
            if len(events) > 1:
                return self._split_post(events)
            self.dropped_events += len(events)
            LOGGER.warning("nTrace event/image serialization failed; dropped_events=%s", self.dropped_events)
            return False
        if len(body) > MAX_RAW_BATCH_BYTES and len(events) > 1:
            return self._split_post(events)
        raw_bytes = len(body)
        headers = {"Content-Type": "application/json"}
        if len(body) >= COMPRESS_THRESHOLD:
            body = gzip.compress(body, compresslevel=1, mtime=0)
            headers["Content-Encoding"] = "gzip"
        if len(body) > MAX_BATCH_BYTES and len(events) > 1:
            return self._split_post(events)
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                req = request.Request(
                    f"{self.server_url}/api/v1/{'snapshot-events' if objects else 'events'}",
                    data=body,
                    headers=headers,
                    method="POST",
                )
                with request.urlopen(req, timeout=self.request_timeout) as response:  # noqa: S310 - configured local endpoint.
                    if 200 <= response.status < 300:
                        for key in objects:
                            self._known_snapshots[key] = None
                            self._known_snapshots.move_to_end(key)
                        while len(self._known_snapshots) > 32_768:
                            self._known_snapshots.popitem(last=False)
                        return True
                    raise HTTPError(req.full_url, response.status, "Upload rejected", {}, None)
            except Exception as error:  # noqa: BLE001 - telemetry is deliberately fail-open.
                status = error.code if isinstance(error, HTTPError) else None
                if isinstance(error, HTTPError):
                    error.close()
                if status == 409 and objects and not _restore:
                    # Receiver may have deleted a trace or restored an older database.
                    self._known_snapshots.clear()
                    return self._post(events, _restore=True)
                if status == 413 and len(events) > 1:
                    return self._split_post(events)
                if status == 413 or attempt >= self.max_retries:
                    self.dropped_events += len(events)
                    reason = error.reason if isinstance(error, URLError) else error
                    LOGGER.warning(
                        "nTrace upload failed; status=%s error_type=%s reason_type=%s errno=%s "
                        "timeout_s=%s elapsed_s=%.3f attempts=%s events=%s bytes=%s raw_bytes=%s encoding=%s "
                        "trace_id=%s span_id=%s dropped_events=%s%s",
                        status, type(error).__name__, type(reason).__name__,
                        reason.errno if isinstance(reason, OSError) else None,
                        self.request_timeout, time.monotonic() - started, attempt + 1,
                        len(events), len(body), raw_bytes, headers.get("Content-Encoding", "identity"),
                        events[0].get("trace_id"), events[0].get("span_id"), self.dropped_events,
                        "; increase proxy client_max_body_size / receiver body limit" if status == 413 else "",
                    )
                    return False
                time.sleep(min(5.0, 0.5 * (2**attempt)))
        return False

    def _split_post(self, events: list[dict[str, Any]]) -> bool:
        midpoint = len(events) // 2
        first = self._post(events[:midpoint])
        # Always attempt the second half, even if an individual event was rejected.
        second = self._post(events[midpoint:])
        return first and second


@lru_cache(maxsize=1)
def default_client() -> NTraceClient:
    """Return the process-wide ordered transport used by agent-local traces."""

    return NTraceClient()


__all__ = ["NTraceClient", "default_client"]
