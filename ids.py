"""Small integer identifiers that remain exact in JavaScript."""

from __future__ import annotations

import threading
import time

MAX_SAFE_INTEGER = (1 << 53) - 1
_CUSTOM_EPOCH_MS = 1_735_689_600_000  # 2025-01-01 UTC
_lock = threading.Lock()
_last_milliseconds = -1
_sequence = 0


def next_id() -> int:
    """Return a process-local, time-sortable 53-bit identifier."""

    global _last_milliseconds, _sequence
    with _lock:
        milliseconds = max(0, int(time.time() * 1000) - _CUSTOM_EPOCH_MS)
        if milliseconds == _last_milliseconds:
            _sequence += 1
            if _sequence > 0xFFF:
                while milliseconds <= _last_milliseconds:
                    time.sleep(0.0001)
                    milliseconds = max(0, int(time.time() * 1000) - _CUSTOM_EPOCH_MS)
                _sequence = 0
        else:
            _sequence = 0
        _last_milliseconds = milliseconds
        value = ((milliseconds & ((1 << 41) - 1)) << 12) | _sequence
    return value & MAX_SAFE_INTEGER


__all__ = ["MAX_SAFE_INTEGER", "next_id"]
