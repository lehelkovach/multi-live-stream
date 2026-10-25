"""Token-bucket rate limiter for outgoing chat messages (one bucket per account)."""

from __future__ import annotations

import time
from collections.abc import Callable


class TokenBucket:
    """``rate`` tokens/second refill, up to ``burst``. ``reserve()`` returns how long the
    caller must wait before its reserved token is valid (0 when one is available now).
    Reservations are sequential, so concurrent callers queue instead of colliding."""

    def __init__(
        self, rate: float, burst: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.rate = max(rate, 1e-6)
        self.burst = max(1, burst)
        self._clock = clock
        self._tokens = float(self.burst)
        self._updated = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(float(self.burst), self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    def reserve(self) -> float:
        self._refill()
        self._tokens -= 1.0
        if self._tokens >= 0:
            return 0.0
        return -self._tokens / self.rate

    def peek_wait(self) -> float:
        self._refill()
        return 0.0 if self._tokens >= 1.0 else (1.0 - self._tokens) / self.rate


class RateLimiters:
    """Per-account buckets configured per platform."""

    def __init__(
        self,
        rates: dict[str, float],
        bursts: dict[str, int],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rates = rates
        self._bursts = bursts
        self._clock = clock
        self._buckets: dict[int, TokenBucket] = {}

    def for_account(self, account_id: int, platform: str) -> TokenBucket:
        bucket = self._buckets.get(account_id)
        if bucket is None:
            bucket = TokenBucket(
                self._rates.get(platform, 1.0), self._bursts.get(platform, 1), self._clock
            )
            self._buckets[account_id] = bucket
        return bucket
