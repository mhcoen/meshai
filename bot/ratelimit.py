"""Token-bucket rate limiting: one global bucket plus one bucket per sender name.

Sender names are attacker-controlled, so the per-sender limit only protects against
a *polite* flood; anyone can rotate names. The global bucket is the real ceiling.
The global bucket's refill rate can be scaled at runtime (by the channel-utilization
monitor); a factor of 0 pauses refill entirely.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: int, clock: Callable[[], float] = time.monotonic):
        if rate_per_sec <= 0 or capacity < 1:
            raise ValueError("rate_per_sec must be positive and capacity at least 1")
        self.rate = float(rate_per_sec)
        self.capacity = int(capacity)
        self._clock = clock
        self._tokens = float(capacity)
        self._updated = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(float(self.capacity), self._tokens + elapsed * self.rate)
        self._updated = now

    def set_rate(self, rate_per_sec: float) -> None:
        """Change the refill rate from now on. Zero pauses refill; tokens already held remain."""
        if rate_per_sec < 0:
            raise ValueError("rate_per_sec must not be negative")
        self._refill()
        self.rate = float(rate_per_sec)

    def peek(self) -> float:
        """Current token count after refill, without consuming."""
        self._refill()
        return self._tokens

    def try_acquire(self) -> bool:
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    reason: str  # "" | "global" | "sender"


class RateLimiter:
    """Global + per-sender buckets; a request consumes from both or from neither."""

    def __init__(
        self,
        global_per_min: float,
        global_burst: int,
        sender_per_min: float,
        sender_burst: int,
        clock: Callable[[], float] = time.monotonic,
        max_senders: int = 512,
    ):
        self._clock = clock
        self._global_base_rate = global_per_min / 60.0
        self._global_factor = 1.0
        self._global = TokenBucket(self._global_base_rate, global_burst, clock)
        self._sender_rate = sender_per_min / 60.0
        self._sender_burst = sender_burst
        self._senders: OrderedDict[str, TokenBucket] = OrderedDict()
        self._max_senders = max_senders

    def _bucket_for(self, sender: str) -> TokenBucket:
        bucket = self._senders.get(sender)
        if bucket is None:
            bucket = TokenBucket(self._sender_rate, self._sender_burst, self._clock)
            self._senders[sender] = bucket
            while len(self._senders) > self._max_senders:
                self._senders.popitem(last=False)
        else:
            self._senders.move_to_end(sender)
        return bucket

    def allow(self, sender: str) -> LimitDecision:
        """Consume one token from both buckets if both have one; otherwise consume nothing."""
        bucket = self._bucket_for(sender)
        if self._global.peek() < 1.0:
            return LimitDecision(False, "global")
        if bucket.peek() < 1.0:
            return LimitDecision(False, "sender")
        self._global.try_acquire()
        bucket.try_acquire()
        return LimitDecision(True, "")

    @property
    def global_factor(self) -> float:
        return self._global_factor

    def set_global_factor(self, factor: float) -> None:
        """Scale the global refill rate: 1.0 = configured rate, 0.5 = half, 0.0 = paused."""
        if not 0.0 <= factor <= 1.0:
            raise ValueError("factor must be between 0 and 1")
        self._global_factor = float(factor)
        self._global.set_rate(self._global_base_rate * self._global_factor)

    def snapshot(self, recent: int = 5) -> dict:
        """State for the monitoring UI."""
        senders = list(self._senders.items())[-recent:]
        return {
            "global_tokens": round(self._global.peek(), 2),
            "global_capacity": self._global.capacity,
            "global_factor": self._global_factor,
            "global_per_min": round(self._global_base_rate * self._global_factor * 60.0, 3),
            "senders": {name: round(b.peek(), 2) for name, b in senders},
            "sender_capacity": self._sender_burst,
        }
