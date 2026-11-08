"""Minimal in-process login throttle.

Single-store, single-process deployment, so an in-memory window is enough and
avoids a Redis dependency. If the API is ever run with multiple workers this
must move to a shared store — noted in docs/ROADMAP.md Phase 8.
"""

import threading
import time
from collections import defaultdict

from app.core.exceptions import AppError


class RateLimitError(AppError):
    status_code = 429
    code = "RATE_LIMITED"


class SlidingWindowLimiter:
    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Raise if `key` has exceeded its budget; otherwise record the hit."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = [t for t in self._hits[key] if t > cutoff]
            if len(hits) >= self.max_attempts:
                retry_in = int(self.window_seconds - (now - hits[0])) + 1
                self._hits[key] = hits
                raise RateLimitError(
                    f"Too many attempts. Try again in {retry_in} seconds.",
                )
            hits.append(now)
            self._hits[key] = hits

    def reset(self, key: str) -> None:
        """Called on success so a correct login clears the counter."""
        with self._lock:
            self._hits.pop(key, None)


# 10 failed logins per identity per 5 minutes.
login_limiter = SlidingWindowLimiter(max_attempts=10, window_seconds=300)
