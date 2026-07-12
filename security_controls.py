"""Small, dependency-free security controls shared by bot/API middleware.

The limiter is intentionally process-local. It is suitable for Ruby Finance's
single Railway worker; a multi-worker deployment must replace the storage with
an atomic shared backend before relying on one global quota.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor, isfinite
from threading import Lock
from time import monotonic
from typing import Callable, Hashable


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Result contract consumed by HTTP or Telegram adapters."""

    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    reset_after: int

    def headers(self) -> dict[str, str]:
        """Return safe response headers; Retry-After exists only on denial."""
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after)
        return headers


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float
    last_seen: float


class TokenBucketLimiter:
    """Thread-safe token bucket keyed by a trusted identity.

    ``refill_rate`` is tokens per second. A denied check does not consume a
    token, and its decision contains the whole-second ``Retry-After`` delay.
    Idle pruning and a hard entry cap prevent unbounded key growth.
    """

    def __init__(
        self,
        *,
        capacity: int,
        refill_rate: float,
        clock: Callable[[], float] = monotonic,
        idle_ttl: float = 3600,
        max_entries: int = 10_000,
    ):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        if not isfinite(float(refill_rate)) or refill_rate <= 0:
            raise ValueError("refill_rate must be finite and positive")
        if not isfinite(float(idle_ttl)) or idle_ttl <= 0:
            raise ValueError("idle_ttl must be finite and positive")
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")

        self.capacity = capacity
        self.refill_rate = float(refill_rate)
        self.idle_ttl = float(idle_ttl)
        self.max_entries = max_entries
        self._clock = clock
        self._buckets: dict[Hashable, _Bucket] = {}
        self._lock = Lock()

    @classmethod
    def per_minute(
        cls,
        requests: int,
        *,
        burst: int | None = None,
        **kwargs,
    ) -> "TokenBucketLimiter":
        """Build a limiter from an easier-to-read requests/minute policy."""
        if isinstance(requests, bool) or not isinstance(requests, int) or requests <= 0:
            raise ValueError("requests must be a positive integer")
        return cls(
            capacity=burst if burst is not None else requests,
            refill_rate=requests / 60,
            **kwargs,
        )

    def check(self, key: Hashable, *, cost: float = 1) -> RateLimitDecision:
        """Consume ``cost`` tokens if available and return a decision."""
        numeric_cost = float(cost)
        if not isfinite(numeric_cost) or numeric_cost <= 0 or numeric_cost > self.capacity:
            raise ValueError("cost must be finite, positive, and at most capacity")

        now = float(self._clock())
        if not isfinite(now):
            raise ValueError("clock must return a finite number")

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._ensure_room_locked(now)
                bucket = _Bucket(
                    tokens=float(self.capacity),
                    updated_at=now,
                    last_seen=now,
                )
                self._buckets[key] = bucket

            elapsed = max(0.0, now - bucket.updated_at)
            tokens = min(
                float(self.capacity),
                bucket.tokens + elapsed * self.refill_rate,
            )
            allowed = tokens >= numeric_cost
            if allowed:
                tokens -= numeric_cost

            bucket.tokens = tokens
            bucket.updated_at = now
            bucket.last_seen = now

            remaining = max(0, floor(tokens + 1e-12))
            retry_after = 0
            if not allowed:
                retry_after = max(
                    1,
                    ceil((numeric_cost - tokens) / self.refill_rate),
                )
            reset_after = max(
                0,
                ceil((self.capacity - tokens) / self.refill_rate),
            )
            return RateLimitDecision(
                allowed=allowed,
                limit=self.capacity,
                remaining=remaining,
                retry_after=retry_after,
                reset_after=reset_after,
            )

    def reset(self, key: Hashable) -> bool:
        """Forget one identity, returning whether a bucket existed."""
        with self._lock:
            return self._buckets.pop(key, None) is not None

    def prune(self) -> int:
        """Remove idle buckets and return the number removed."""
        now = float(self._clock())
        if not isfinite(now):
            raise ValueError("clock must return a finite number")
        with self._lock:
            return self._prune_locked(now)

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)

    def _ensure_room_locked(self, now: float) -> None:
        self._prune_locked(now)
        if len(self._buckets) < self.max_entries:
            return
        oldest_key = min(
            self._buckets,
            key=lambda candidate: self._buckets[candidate].last_seen,
        )
        self._buckets.pop(oldest_key, None)

    def _prune_locked(self, now: float) -> int:
        stale = [
            key
            for key, bucket in self._buckets.items()
            if max(0.0, now - bucket.last_seen) >= self.idle_ttl
        ]
        for key in stale:
            self._buckets.pop(key, None)
        return len(stale)
