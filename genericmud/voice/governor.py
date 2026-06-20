"""Token-bucket throttle for the self-voice fast-output governor."""

from __future__ import annotations

import time
from collections.abc import Callable


class TokenBucket:
    """Allows ``capacity`` immediate takes, refilling at ``refill_per_sec``.

    The clock is injectable so tests can advance time deterministically.
    """

    def __init__(
        self,
        capacity: float,
        refill_per_sec: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._tokens = capacity
        self._refill = refill_per_sec
        self._clock = clock
        self._stamp = clock()

    def take(self, amount: float = 1.0) -> bool:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._stamp) * self._refill)
        self._stamp = now
        if self._tokens >= amount:
            self._tokens -= amount
            return True
        return False
