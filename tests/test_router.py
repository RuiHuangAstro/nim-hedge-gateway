"""Tests for app/router.py: TokenBucket, RouterState key selection, and queue waiting."""
import asyncio
import time
import pytest
from unittest.mock import MagicMock, patch

from app.router import TokenBucket, RouterState


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------

def test_token_bucket_consumes_token():
    now = time.monotonic()
    bucket = TokenBucket(capacity=5.0, tokens=5.0, last_refill_mono=now, rate_per_second=1.0)
    assert bucket.try_consume(now) is True
    assert abs(bucket.tokens - 4.0) < 0.01


def test_token_bucket_empty_returns_false():
    now = time.monotonic()
    bucket = TokenBucket(capacity=5.0, tokens=0.5, last_refill_mono=now, rate_per_second=1.0)
    assert bucket.try_consume(now) is False


def test_token_bucket_refills_over_time():
    now = time.monotonic()
    bucket = TokenBucket(capacity=5.0, tokens=0.0, last_refill_mono=now, rate_per_second=2.0)
    # Advance time by 1 second → should add 2 tokens
    later = now + 1.0
    assert bucket.available(later) == pytest.approx(2.0, abs=0.01)


def test_token_bucket_does_not_exceed_capacity():
    now = time.monotonic()
    bucket = TokenBucket(capacity=3.0, tokens=2.0, last_refill_mono=now, rate_per_second=10.0)
    later = now + 10.0
    assert bucket.available(later) == pytest.approx(3.0, abs=0.01)


# ---------------------------------------------------------------------------
# Helpers to build a mock config and health_store
# ---------------------------------------------------------------------------

def _make_config(keys, rpm=0, burst=0, allow_best_effort=False,
                 queue_when_limited=False, max_queue_seconds=0.5):
    cfg = MagicMock()
    cfg.server.api_key_envs = keys
    cfg.server.rpm_limit_per_api = rpm
    cfg.server.burst_per_api = burst
    cfg.server.allow_best_effort_when_all_limited = allow_best_effort
    cfg.server.queue_when_limited = queue_when_limited
    cfg.server.max_queue_seconds = max_queue_seconds
    return cfg


def _make_health(active=None):
    """active is a dict key_alias -> int (kept for stats only; no longer gates)."""
    hs = MagicMock()
    hs.get_active_count.side_effect = lambda k: (active or {}).get(k, 0)
    hs.api_recent_429_ts = {}
    hs.get_api_pre_request_delay.return_value = 0.0
    return hs


# ---------------------------------------------------------------------------
# RouterState.select_key
# ---------------------------------------------------------------------------

def test_select_key_returns_eligible_key():
    cfg = _make_config(["k1", "k2"])
    hs = _make_health()
    router = RouterState()
    with patch("app.router.config", cfg), patch("app.router.health_store", hs):
        key = router.select_key()
    assert key in ["k1", "k2"]


def test_select_key_ignores_recent_429():
    """Recent 429s no longer remove a key from rotation — only the downstream
    2^N pre-request delay throttles. Both keys stay selectable."""
    cfg = _make_config(["k1", "k2"])
    hs = _make_health()
    hs.api_recent_429_ts = {"k1": [time.time()] * 5}
    router = RouterState()
    seen = set()
    with patch("app.router.config", cfg), patch("app.router.health_store", hs):
        for _ in range(6):
            seen.add(router.select_key())
    assert seen == {"k1", "k2"}


def test_select_key_ignores_active_count():
    """There is no concurrency cap: a key with many in-flight requests is
    still eligible."""
    cfg = _make_config(["k1", "k2"])
    hs = _make_health(active={"k1": 99, "k2": 99})
    router = RouterState()
    seen = set()
    with patch("app.router.config", cfg), patch("app.router.health_store", hs):
        for _ in range(6):
            seen.add(router.select_key())
    assert seen == {"k1", "k2"}


def test_select_key_returns_none_when_all_buckets_empty():
    cfg = _make_config(["k1", "k2"], rpm=60, burst=5)
    hs = _make_health()
    router = RouterState()
    now = time.monotonic()
    with patch("app.router.config", cfg), patch("app.router.health_store", hs):
        router._bucket("k1").tokens = 0.0
        router._bucket("k2").tokens = 0.0
        assert router.select_key(now) is None


def test_select_key_round_robin_distribution():
    """Equal-idle keys should be distributed, not always returning the first key."""
    cfg = _make_config(["k1", "k2", "k3"])
    hs = _make_health()
    router = RouterState()
    counts = {"k1": 0, "k2": 0, "k3": 0}
    with patch("app.router.config", cfg), patch("app.router.health_store", hs):
        for _ in range(30):
            key = router.select_key()
            counts[key] += 1
    # Each key should appear at least 8 times out of 30
    for k, c in counts.items():
        assert c >= 8, f"{k} only selected {c} times"


def test_select_key_round_robin_equal_distribution():
    """With 2 equal keys, calls should alternate."""
    cfg = _make_config(["k1", "k2"])
    hs = _make_health()
    router = RouterState()
    selected = []
    with patch("app.router.config", cfg), patch("app.router.health_store", hs):
        for _ in range(6):
            selected.append(router.select_key())
    # Should not be all k1
    assert len(set(selected)) == 2


def test_select_key_token_bucket_empty_skipped():
    cfg = _make_config(["k1", "k2"], rpm=60, burst=5)
    hs = _make_health()
    router = RouterState()
    now = time.monotonic()
    with patch("app.router.config", cfg), patch("app.router.health_store", hs):
        # Drain k1's bucket
        bucket = router._bucket("k1")
        bucket.tokens = 0.0
        # k2 bucket still full
        key = router.select_key(now)
        assert key == "k2"


def test_select_key_best_effort_overrides_empty_bucket():
    cfg = _make_config(["k1"], rpm=60, burst=5, allow_best_effort=True)
    hs = _make_health()
    router = RouterState()
    now = time.monotonic()
    with patch("app.router.config", cfg), patch("app.router.health_store", hs):
        bucket = router._bucket("k1")
        bucket.tokens = 0.0
        key = router.select_key(now)
        assert key == "k1"


# ---------------------------------------------------------------------------
# RouterState.consume_token
# ---------------------------------------------------------------------------

def test_consume_token_decrements_bucket():
    cfg = _make_config(["k1"], rpm=60, burst=10)
    hs = _make_health()
    router = RouterState()
    with patch("app.router.config", cfg), patch("app.router.health_store", hs):
        before = router.tokens_available("k1")
        router.consume_token("k1")
        after = router.tokens_available("k1")
    assert before - after == pytest.approx(1.0, abs=0.01)


def test_consume_token_unlimited_always_true():
    cfg = _make_config(["k1"], rpm=0)
    hs = _make_health()
    router = RouterState()
    with patch("app.router.config", cfg), patch("app.router.health_store", hs):
        assert router.consume_token("k1") is True
        assert router.tokens_available("k1") == float("inf")


# ---------------------------------------------------------------------------
# RouterState.wait_for_key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_for_key_returns_when_key_becomes_available():
    cfg = _make_config(["k1"], rpm=60, burst=5, queue_when_limited=True, max_queue_seconds=2.0)
    hs = _make_health()
    router = RouterState()

    with patch("app.router.config", cfg), patch("app.router.health_store", hs):
        bucket = router._bucket("k1")
        bucket.tokens = 0.0  # drained: no budget yet

        async def refill_after_delay():
            await asyncio.sleep(0.3)
            bucket.tokens = 5.0

        refill_task = asyncio.create_task(refill_after_delay())
        start = time.monotonic()
        key = await router.wait_for_key(2.0)
        elapsed = time.monotonic() - start
        await refill_task

    assert key == "k1"
    assert elapsed >= 0.25  # waited at least one poll interval


@pytest.mark.asyncio
async def test_wait_for_key_returns_none_on_timeout():
    # rpm=1 → ~0.017 tokens/s, so a drained bucket never refills to 1 within 0.4s.
    cfg = _make_config(["k1"], rpm=1, burst=1, max_queue_seconds=0.4)
    hs = _make_health()
    router = RouterState()
    with patch("app.router.config", cfg), patch("app.router.health_store", hs):
        router._bucket("k1").tokens = 0.0
        start = time.monotonic()
        key = await router.wait_for_key(0.4)
        elapsed = time.monotonic() - start

    assert key is None
    assert elapsed >= 0.35


@pytest.mark.asyncio
async def test_wait_for_key_returns_first_key_with_budget():
    cfg = _make_config(["k1", "k2"], rpm=60, burst=5)
    hs = _make_health()
    router = RouterState()
    with patch("app.router.config", cfg), patch("app.router.health_store", hs):
        router._bucket("k1").tokens = 0.0  # k1 drained, k2 still has budget
        key = await router.wait_for_key(2.0)

    assert key == "k2"
