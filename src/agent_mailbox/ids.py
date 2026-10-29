"""UUIDv7 ids (RFC 9562): time-ordered, strictly increasing within one process."""

from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last = 0  # last (ms << 12 | counter) handed out


def uuid7() -> str:
    """A UUIDv7 string. Uses the 12-bit ``rand_a`` field as a monotonic counter."""
    global _last
    with _lock:
        ms = time.time_ns() // 1_000_000
        candidate = ms << 12 | int.from_bytes(os.urandom(2), "big") & 0x3FF
        if candidate <= _last:
            candidate = _last + 1
        _last = candidate
    ms, counter = candidate >> 12, candidate & 0xFFF
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | counter << 64 | 0b10 << 62 | rand_b
    return str(uuid.UUID(int=value))
