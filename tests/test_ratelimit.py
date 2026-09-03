import pytest

from bot.ratelimit import RateLimiter, TokenBucket


def test_bucket_starts_full_and_refills_over_time(clock):
    bucket = TokenBucket(rate_per_sec=1 / 60, capacity=1, clock=clock)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False
    clock.advance(30)
    assert bucket.try_acquire() is False
    clock.advance(30)
    assert bucket.try_acquire() is True


def test_bucket_never_exceeds_capacity(clock):
    bucket = TokenBucket(rate_per_sec=1.0, capacity=2, clock=clock)
    clock.advance(1000)
    assert bucket.peek() == 2.0


def test_bucket_rejects_bad_parameters(clock):
    with pytest.raises(ValueError):
        TokenBucket(0, 1, clock)
    with pytest.raises(ValueError):
        TokenBucket(1, 0, clock)


def test_global_limit_applies_across_senders(clock):
    rl = RateLimiter(global_per_min=1, global_burst=1, sender_per_min=60, sender_burst=10, clock=clock)
    assert rl.allow("alice").allowed
    d = rl.allow("bob")
    assert (d.allowed, d.reason) == (False, "global")
    clock.advance(60)
    assert rl.allow("bob").allowed


def test_sender_limit_is_per_name(clock):
    rl = RateLimiter(global_per_min=60, global_burst=10, sender_per_min=1, sender_burst=1, clock=clock)
    assert rl.allow("alice").allowed
    d = rl.allow("alice")
    assert (d.allowed, d.reason) == (False, "sender")
    assert rl.allow("bob").allowed  # a different (spoofable) name has its own bucket
    clock.advance(60)
    assert rl.allow("alice").allowed


def test_denied_request_consumes_nothing(clock):
    rl = RateLimiter(global_per_min=60, global_burst=3, sender_per_min=1, sender_burst=1, clock=clock)
    assert rl.allow("alice").allowed
    before = rl.snapshot()["global_tokens"]
    assert not rl.allow("alice").allowed  # sender exhausted, global still has tokens
    assert rl.snapshot()["global_tokens"] == before


def test_sender_table_is_bounded(clock):
    rl = RateLimiter(60, 10, 60, 10, clock=clock, max_senders=3)
    for name in ("a", "b", "c", "d"):
        rl.allow(name)
    assert set(rl.snapshot(recent=10)["senders"]) == {"b", "c", "d"}


def test_snapshot_reports_state_for_the_ui(clock):
    rl = RateLimiter(global_per_min=1, global_burst=1, sender_per_min=1, sender_burst=1, clock=clock)
    rl.allow("alice")
    snap = rl.snapshot()
    assert snap["global_tokens"] == 0.0
    assert snap["global_capacity"] == 1
    assert snap["senders"] == {"alice": 0.0}
