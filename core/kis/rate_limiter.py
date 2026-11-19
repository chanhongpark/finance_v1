"""
Per-account token-bucket rate limiter for KIS API (R8).
"""
from __future__ import annotations

import threading
import time


class AccountRateLimiter:
    """Thread-safe minimum-interval rate limiter bound to a single KIS AppKey."""

    def __init__(self, calls_per_second: float = 5.0) -> None:
        self.min_interval = 1.0 / max(0.1, float(calls_per_second))
        self._lock = threading.Lock()
        self._last_call = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
