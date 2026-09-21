"""Rate limiting and /v1/compare tests (SPEC sections 5, 9, 10).

The rate limiter is the only component in this project that protects **money**
rather than correctness. SPEC section 10: "Prevents one caller draining the API
budget." Without it, one caller in a retry loop empties the account and no
amount of clever routing matters.
"""

import time

import pytest

from app.ratelimit import Bucket, RateLimiter
from conftest import rows


# ---------------------------------------------------------------------------
# The bucket itself
# ---------------------------------------------------------------------------

def test_new_caller_starts_full():
    """Starting empty would reject everyone's first request, which is
    indistinguishable from an outage to someone trying the API for the first
    time."""
    limiter = RateLimiter(per_minute=60)
    allowed, _ = limiter.check("1.2.3.4")
    assert allowed is True


def test_bucket_exhausts_after_capacity():
    limiter = RateLimiter(per_minute=5)
    results = [limiter.check("1.2.3.4")[0] for _ in range(7)]
    assert results[:5] == [True] * 5
    assert results[5:] == [False, False]


def test_rejection_reports_retry_after():
    limiter = RateLimiter(per_minute=60)
    for _ in range(60):
        limiter.check("1.2.3.4")

    allowed, retry_after = limiter.check("1.2.3.4")
    assert allowed is False
    assert 0 < retry_after <= 2, f"expected ~1s at 60/min, got {retry_after}"


def test_callers_are_independent():
    """One caller exhausting their allowance must not affect anyone else --
    otherwise the limit is a shared outage rather than per-caller fairness."""
    limiter = RateLimiter(per_minute=3)
    for _ in range(5):
        limiter.check("noisy")

    assert limiter.check("noisy")[0] is False
    assert limiter.check("quiet")[0] is True


def test_bucket_refills_over_time():
    """Continuous refill is what distinguishes a token bucket from a fixed
    window, which allows 2x the rate across a window boundary."""
    limiter = RateLimiter(per_minute=6000)     # 100/sec -> fast enough to test
    for _ in range(6000):
        limiter.check("1.2.3.4")
    assert limiter.check("1.2.3.4")[0] is False

    time.sleep(0.05)      # ~5 tokens back
    assert limiter.check("1.2.3.4")[0] is True


def test_tokens_are_fractional():
    """Rounding down on every check would leak allowance and make the real
    rate lower than the configured one."""
    bucket = Bucket(tokens=0.0, last_refill=0.0, last_seen=0.0)
    bucket.refill(capacity=60, per_second=1.0, now=0.5)
    assert 0 < bucket.tokens < 1.0


def test_refill_never_exceeds_capacity():
    """An idle caller must not bank unlimited allowance and then burst."""
    bucket = Bucket(tokens=0.0, last_refill=0.0, last_seen=0.0)
    bucket.refill(capacity=10, per_second=1.0, now=10_000)
    assert bucket.tokens == 10


def test_idle_buckets_are_evicted():
    """Unbounded growth per distinct IP is a slow memory leak an attacker can
    accelerate deliberately."""
    limiter = RateLimiter(per_minute=60)
    for i in range(50):
        limiter.check(f"10.0.0.{i}")
    assert limiter.snapshot()["tracked_callers"] == 50

    # Age every bucket past the eviction window, then force a sweep.
    for bucket in limiter._buckets.values():
        bucket.last_seen -= 7200
    limiter._last_sweep -= 120
    limiter.check("fresh.caller")

    assert limiter.snapshot()["tracked_callers"] == 1


# ---------------------------------------------------------------------------
# Through the endpoint
# ---------------------------------------------------------------------------

def test_endpoint_returns_429_with_retry_after(client):
    """SPEC section 5: 429 includes retry_after_seconds."""
    client.app.state.limiter = RateLimiter(per_minute=3)

    statuses = [
        client.post("/v1/chat", json={"prompt": f"request {i}"}).status_code
        for i in range(5)
    ]
    assert 429 in statuses

    limited = client.post("/v1/chat", json={"prompt": "one more"})
    assert limited.status_code == 429
    body = limited.json()
    assert "retry_after_seconds" in body
    assert limited.headers.get("Retry-After") is not None


def test_rate_limited_request_never_reaches_a_provider(client, mock_provider):
    """The whole point: a rejected request must cost nothing."""
    client.app.state.limiter = RateLimiter(per_minute=1)
    client.post("/v1/chat", json={"prompt": "the one allowed request"})
    calls_after_first = mock_provider.calls

    for _ in range(3):
        client.post("/v1/chat", json={"prompt": "rejected"})

    assert mock_provider.calls == calls_after_first


def test_rate_limited_request_still_writes_a_row(client, db_path):
    """SPEC section 2: nothing is silently dropped. A 429 is still a request
    that happened, and an operator investigating a complaint needs to see it."""
    client.app.state.limiter = RateLimiter(per_minute=1)
    client.post("/v1/chat", json={"prompt": "allowed"})

    before = len(rows(db_path))
    client.post("/v1/chat", json={"prompt": "rejected"})
    after = rows(db_path)

    assert len(after) == before + 1
    assert after[-1]["status"] == "error"
    assert "rate limited" in after[-1]["error_message"]


def test_forwarded_for_cannot_bypass_the_limit(client):
    """X-Forwarded-For is caller-controlled. Honouring it without a verified
    proxy in front would let anyone bypass the limit by inventing a header,
    turning the protection into decoration."""
    client.app.state.limiter = RateLimiter(per_minute=2)

    statuses = [
        client.post("/v1/chat", json={"prompt": f"p{i}"},
                    headers={"X-Forwarded-For": f"9.9.9.{i}"}).status_code
        for i in range(5)
    ]
    assert 429 in statuses, "spoofed headers granted extra allowance"


# ---------------------------------------------------------------------------
# /v1/compare -- the D3 mitigation
# ---------------------------------------------------------------------------

def test_compare_runs_both_tiers(client):
    body = client.post("/v1/compare", json={"prompt": "What is 2+2?"}).json()

    assert body["routed"]["ok"] is True
    assert body["baseline"]["ok"] is True
    assert body["baseline"]["tier"] == "large"
    assert "routing" in body


def test_compare_measures_the_d3_bias(client):
    """D3: cost_if_large_usd ASSUMES output_token_ratio == 1.0. This endpoint
    exists to turn that assumption into a measurement."""
    body = client.post("/v1/compare", json={"prompt": "Explain gravity."}).json()

    comparison = body["comparison"]
    assert "output_token_ratio" in comparison
    assert "d3_bias_note" in comparison
    assert comparison["cost_saved_usd"] >= 0


def test_compare_never_stores_the_full_prompt(client):
    """Same privacy rule as the request log (SPEC section 10)."""
    secret = "SENSITIVE" + "q" * 300
    body = client.post("/v1/compare", json={"prompt": secret}).json()
    assert len(body["prompt_preview"]) <= 80


def test_compare_rejects_an_empty_prompt(client):
    assert client.post("/v1/compare", json={"prompt": "  "}).status_code == 400


def test_compare_is_rate_limited_too(client):
    """It costs TWO provider calls, so it is the LAST endpoint that should be
    exempt from the budget protection."""
    client.app.state.limiter = RateLimiter(per_minute=2)
    statuses = [
        client.post("/v1/compare", json={"prompt": f"p{i}"}).status_code
        for i in range(5)
    ]
    assert 429 in statuses
