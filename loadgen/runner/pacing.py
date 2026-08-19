"""Token-bucket pacing for open-loop mode."""
from __future__ import annotations

import threading
import time


class TokenBucket:
    """Shared across the threads of one worker process."""

    def __init__(self, rate: float, burst: float | None = None):
        self.rate = max(rate, 0.001)
        self.burst = burst if burst is not None else max(rate, 1.0)
        self.tokens = self.burst
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def set_rate(self, rate: float) -> None:
        with self.lock:
            self.rate = max(rate, 0.001)
            self.burst = max(self.rate, 1.0)

    def acquire(self, stop_event: threading.Event) -> bool:
        """Block until a token is available. Returns False if stopping."""
        while not stop_event.is_set():
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.ts) * self.rate)
                self.ts = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
                wait = (1.0 - self.tokens) / self.rate
            stop_event.wait(min(wait, 0.5))
        return False
