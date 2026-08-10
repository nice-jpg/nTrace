"""Non-blocking HTTP client for nTrace event packets."""

from __future__ import annotations

import atexit
from functools import lru_cache
import json
import logging
import os
import queue
import threading
import time
from typing import Any
from urllib import request

LOGGER = logging.getLogger("nTrace.client")


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
        request_timeout: float = 1.0,
        max_retries: int = 2,
    ) -> None:
        self.server_url = (server_url or os.getenv("NTRACE_SERVER_URL") or "http://127.0.0.1:8765").rstrip("/")
        if enabled is None:
            enabled = os.getenv("NTRACE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
        self.enabled = bool(enabled)
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.01, float(flush_interval))
        self.request_timeout = max(0.05, float(request_timeout))
        self.max_retries = max(0, int(max_retries))
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.dropped_events = 0
        self.failed_batches = 0
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
        deadline = time.monotonic() + max(0.0, timeout)
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._queue.empty()

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

    def _post(self, events: list[dict[str, Any]]) -> bool:
        body = json.dumps({"events": events}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        for attempt in range(self.max_retries + 1):
            try:
                req = request.Request(
                    f"{self.server_url}/api/v1/events",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with request.urlopen(req, timeout=self.request_timeout) as response:  # noqa: S310 - configured local endpoint.
                    return 200 <= response.status < 300
            except Exception as error:  # noqa: BLE001 - telemetry is deliberately fail-open.
                if attempt >= self.max_retries:
                    LOGGER.debug("nTrace upload failed: %s", error)
                    return False
                time.sleep(0.05 * (2**attempt))
        return False


@lru_cache(maxsize=1)
def default_client() -> NTraceClient:
    """Return the process-wide ordered transport used by agent-local traces."""

    return NTraceClient()


__all__ = ["NTraceClient", "default_client"]
