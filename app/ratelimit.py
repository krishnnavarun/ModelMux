"""Per-caller rate limiting (SPEC section 10).

SPEC: "Rate limiting -- per-caller, in-memory token bucket. Prevents one caller
draining the API budget."

That last clause is the point. Every other limit in this service protects
correctness; this one protects **money**. Without it a single caller in a retry
loop can spend the entire API budget in minutes, and no amount of clever
routing helps once the credit is gone.

-----------------------------------------------------------------------------
WHY A TOKEN BUCKET RATHER THAN A FIXED WINDOW
-----------------------------------------------------------------------------
A fixed window ("60 requests per minute, reset on the minute") allows a burst
of 120 across a window boundary -- 60 at 11:59:59 and 60 more at 12:00:00. That
is double the intended rate at exactly the worst moment.

A token bucket refills continuously, so the long-run rate is exactly the
configured one while still permitting a short burst up to the bucket's
capacity. It is also cheaper: two floats per caller and no history to sweep.

-----------------------------------------------------------------------------
STATED LIMITATION: PER PROCESS, AND KEYED BY IP
-----------------------------------------------------------------------------
State is in memory, like the circuit breakers. With several workers each keeps
its own bucket, so the effective limit is `workers x rate`. Sharing it would
mean Redis on the request path -- a dependency on the fast path, for a limit
that is a safety net rather than a billing boundary.

Callers are identified by IP because this service has no authentication. Once
it does, the key should become the API key or account id: an IP is shared by
everyone behind a NAT and changes for a single user on a phone.
"""

import sys
import time
from dataclasses import dataclass

# Callers that have not been seen for this long are forgotten, so an unbounded
# stream of distinct IPs cannot grow the dict forever. That is itself a small
# denial-of-service vector if left unbounded.
IDLE_EVICTION_SECONDS = 3600


@dataclass
class Bucket:
    """One caller's allowance.

    `tokens` is a float, not an int: refill is continuous, and rounding down on
    every check would leak allowance and make the effective rate lower than
    configured.
    """

    tokens: float
    last_refill: float
    last_seen: float

    def refill(self, capacity: float, per_second: float, now: float) -> None:
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(capacity, self.tokens + elapsed * per_second)
            self.last_refill = now

    def take(self) -> bool:
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def retry_after(self, per_second: float) -> float:
        """Seconds until one whole token is available."""
        if per_second <= 0:
            return 60.0
        return max(0.0, (1.0 - self.tokens) / per_second)


class RateLimiter:
    """Token buckets keyed by caller."""

    def __init__(self, per_minute: int):
        self.capacity = float(per_minute)
        self.per_second = per_minute / 60.0
        self._buckets: dict[str, Bucket] = {}
        self._last_sweep = time.monotonic()

    def check(self, caller: str) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds).

        `retry_after` is 0.0 when allowed -- callers should ignore it then.
        """
        now = time.monotonic()
        self._maybe_sweep(now)

        bucket = self._buckets.get(caller)
        if bucket is None:
            # A new caller starts FULL. Starting empty would reject the first
            # request from everyone, which is indistinguishable from an outage
            # to someone trying the API for the first time.
            bucket = Bucket(tokens=self.capacity, last_refill=now, last_seen=now)
            self._buckets[caller] = bucket

        bucket.last_seen = now
        bucket.refill(self.capacity, self.per_second, now)

        if bucket.take():
            return True, 0.0
        return False, round(bucket.retry_after(self.per_second), 2)

    def _maybe_sweep(self, now: float) -> None:
        """Drop buckets for callers long gone.

        Without this the dict grows once per distinct IP forever -- a slow
        memory leak that an attacker can accelerate deliberately.
        """
        if now - self._last_sweep < 60:
            return
        self._last_sweep = now

        stale = [k for k, b in self._buckets.items()
                 if now - b.last_seen > IDLE_EVICTION_SECONDS]
        for key in stale:
            del self._buckets[key]
        if stale:
            print(f"[ratelimit] evicted {len(stale)} idle buckets",
                  file=sys.stderr)

    def snapshot(self) -> dict:
        """For /health/providers -- how many callers are being tracked."""
        return {
            "tracked_callers": len(self._buckets),
            "capacity_per_minute": int(self.capacity),
        }


def caller_id(request) -> str:
    """Identify the caller.

    IP only, because this service has no authentication. Documented as a
    limitation rather than presented as identity: an IP is shared by everyone
    behind a NAT, and changes for one user on a mobile network.

    X-Forwarded-For is deliberately NOT trusted. It is caller-controlled, so
    honouring it without a verified proxy in front would let anyone bypass the
    limit by inventing a header -- turning the protection into decoration.
    """
    client = getattr(request, "client", None)
    return client.host if client and client.host else "unknown"
