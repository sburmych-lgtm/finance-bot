import pytest

from security_controls import TokenBucketLimiter


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_token_bucket_denies_after_burst_and_returns_retry_after_headers():
    clock = Clock()
    limiter = TokenBucketLimiter(capacity=2, refill_rate=0.5, clock=clock)

    first = limiter.check("user-1")
    second = limiter.check("user-1")
    denied = limiter.check("user-1")

    assert first.allowed is True
    assert first.remaining == 1
    assert second.allowed is True
    assert second.remaining == 0
    assert denied.allowed is False
    assert denied.retry_after == 2
    assert denied.headers() == {
        "X-RateLimit-Limit": "2",
        "X-RateLimit-Remaining": "0",
        "Retry-After": "2",
    }


def test_token_bucket_refills_using_monotonic_time_without_charging_denials():
    clock = Clock()
    limiter = TokenBucketLimiter(capacity=1, refill_rate=0.25, clock=clock)

    assert limiter.check("user-1").allowed is True
    assert limiter.check("user-1").retry_after == 4

    clock.advance(3)
    assert limiter.check("user-1").retry_after == 1
    assert limiter.check("user-1").retry_after == 1

    clock.advance(1)
    assert limiter.check("user-1").allowed is True


def test_token_bucket_isolates_keys_and_supports_weighted_costs():
    clock = Clock()
    limiter = TokenBucketLimiter(capacity=5, refill_rate=1, clock=clock)

    user_one = limiter.check("user-1", cost=3)
    user_two = limiter.check("user-2", cost=5)

    assert user_one.allowed is True
    assert user_one.remaining == 2
    assert user_two.allowed is True
    assert user_two.remaining == 0
    assert limiter.check("user-1", cost=3).allowed is False
    assert limiter.check("user-2").allowed is False


def test_token_bucket_prunes_idle_entries_and_can_reset_one_key():
    clock = Clock()
    limiter = TokenBucketLimiter(
        capacity=1,
        refill_rate=1,
        clock=clock,
        idle_ttl=10,
        max_entries=10,
    )
    limiter.check("user-1")
    limiter.check("user-2")
    assert len(limiter) == 2

    limiter.reset("user-1")
    assert len(limiter) == 1

    clock.advance(11)
    assert limiter.prune() == 1
    assert len(limiter) == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"capacity": 0, "refill_rate": 1},
        {"capacity": 1, "refill_rate": 0},
        {"capacity": 1, "refill_rate": 1, "idle_ttl": 0},
        {"capacity": 1, "refill_rate": 1, "max_entries": 0},
    ],
)
def test_token_bucket_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        TokenBucketLimiter(**kwargs)


def test_token_bucket_rejects_invalid_cost():
    limiter = TokenBucketLimiter(capacity=2, refill_rate=1)

    with pytest.raises(ValueError):
        limiter.check("user", cost=0)
    with pytest.raises(ValueError):
        limiter.check("user", cost=3)
