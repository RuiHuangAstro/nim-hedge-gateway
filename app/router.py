"""Key selection router with per-key token bucket rate limiting and queue waiting."""
import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.config import config
from app.health import health_store


@dataclass
class TokenBucket:
    capacity: float
    tokens: float
    last_refill_mono: float
    rate_per_second: float  # tokens/second = rpm/60

    def _refill(self, now: float) -> None:
        elapsed = now - self.last_refill_mono
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_second)
        self.last_refill_mono = now

    def available(self, now: float) -> float:
        self._refill(now)
        return self.tokens

    def try_consume(self, now: float) -> bool:
        self._refill(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class RouterState:
    def __init__(self) -> None:
        self._buckets: Dict[str, Optional[TokenBucket]] = {}
        self._rr_counter: int = 0

    def _bucket(self, key_alias: str) -> Optional[TokenBucket]:
        if key_alias in self._buckets:
            return self._buckets[key_alias]
        rpm = config.server.rpm_limit_per_api
        if rpm <= 0:
            self._buckets[key_alias] = None
            return None
        burst = config.server.burst_per_api or rpm
        bucket = TokenBucket(
            capacity=float(burst),
            tokens=float(burst),
            last_refill_mono=time.monotonic(),
            rate_per_second=rpm / 60.0,
        )
        self._buckets[key_alias] = bucket
        return bucket

    def reset_buckets(self) -> None:
        """Clear cached buckets so they pick up new config values. Used in tests."""
        self._buckets.clear()

    def select_key(self, now: Optional[float] = None) -> Optional[str]:
        """Return an eligible API key using round-robin tie-breaking, or None."""
        if now is None:
            now = time.monotonic()
        keys = config.server.api_key_envs
        if not keys:
            return None

        allow_best_effort = config.server.allow_best_effort_when_all_limited
        n = len(keys)

        # True round-robin: walk forward from last position. The only gate is
        # the token bucket (rpm); 429 pressure is handled downstream via the
        # per-key 2^N pre-request delay, not by skipping keys here.
        start = self._rr_counter % n
        for offset in range(n):
            idx = (start + offset) % n
            key = keys[idx]
            bucket = self._bucket(key)
            if bucket is not None and bucket.available(now) < 1.0 and not allow_best_effort:
                continue
            self._rr_counter = (idx + 1) % n
            return key
        return None

    def consume_token(self, key_alias: str, now: Optional[float] = None) -> bool:
        """Spend one token. Returns True even when bucket is None (unlimited)."""
        bucket = self._bucket(key_alias)
        if bucket is None:
            return True
        return bucket.try_consume(now if now is not None else time.monotonic())

    def tokens_available(self, key_alias: str) -> float:
        bucket = self._bucket(key_alias)
        if bucket is None:
            return float("inf")
        return bucket.available(time.monotonic())

    async def wait_for_key(self, max_seconds: float) -> Optional[str]:
        """Poll until a key becomes available or max_seconds elapses."""
        deadline = time.monotonic() + max_seconds
        poll_interval = 0.25
        while True:
            key = self.select_key()
            if key is not None:
                return key
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(poll_interval, remaining))

    def get_key_stats(self) -> List[Dict]:
        now_wall = time.time()
        stats = []
        for key in config.server.api_key_envs:
            recent_429 = [
                t for t in health_store.api_recent_429_ts.get(key, [])
                if t >= now_wall - 300
            ]
            stats.append({
                "key_alias": key,
                "active_count": health_store.get_active_count(key),
                "token_bucket_remaining": round(self.tokens_available(key), 2),
                "rpm_limit": config.server.rpm_limit_per_api or None,
                "recent_429_count_5m": len(recent_429),
                "pre_request_delay_seconds": round(health_store.get_api_pre_request_delay(key), 1),
            })
        return stats


router_state = RouterState()
