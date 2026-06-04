from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Async token-bucket limiter shared across all outbound MRKT requests.

    Smooths request rate to stay under the marketplace limit and avoid 429s,
    while still allowing short bursts up to `burst`.
    """

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self._rate = max(rate_per_sec, 0.1)
        self._capacity = max(burst, 1)
        self._tokens = float(self._capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                await asyncio.sleep(deficit / self._rate)
