# Resilience Patterns

---

## 1. What it is

**Resilience** is how a system behaves when something it depends on fails.

- **Retry** — try the same thing again, hoping the failure was transient
- **Backoff** — wait longer between each retry
- **Jitter** — randomise the wait so clients do not retry in lockstep
- **Fallback** — try a different thing
- **Circuit breaker** — stop trying something that keeps failing
- **Bulkhead** — isolate failures so one dependency cannot sink the whole system

---

## 2. How it works

### Retry

The simplest mechanism and the easiest to get wrong.

**Only retry what can succeed on a second attempt.** A 500 might be a bad
server in a pool; a 401 will be a 401 forever. Retrying a deterministic failure
multiplies latency and load for a guaranteed identical result.

**Retries must be bounded.** Unbounded retries turn a brief outage into a
self-inflicted denial of service against your own dependency.

**Only retry idempotent operations.** Retrying a non-idempotent POST can charge
a customer twice. This is what idempotency keys are for.

### Exponential backoff with jitter

```
delay = random_uniform(0, base * 2^attempt)
```

Doubling gives a struggling service room to recover. **Jitter is the part
people omit and the part that matters most under load:** without it, every
client that failed at the same instant retries at the same instant, producing a
synchronised burst — the *thundering herd* — that re-triggers the overload they
were backing off from.

Variants: *full jitter* (`uniform(0, cap)`), *equal jitter*
(`cap/2 + uniform(0, cap/2)`), *decorrelated jitter*.

### Circuit breaker

A state machine that stops you paying to rediscover a known failure.

```
         failures >= threshold
 CLOSED ───────────────────────▶ OPEN
   ▲                              │
   │                              │ cooldown elapsed
   │ probe succeeds               ▼
   └────────────────────── HALF_OPEN
                                  │ probe fails
                                  └──▶ OPEN (cooldown restarts)
```

- **CLOSED** — normal. Count failures within a rolling window.
- **OPEN** — fail fast without calling. This is the point: you stop paying a
  30-second timeout on every request to learn what you already know.
- **HALF_OPEN** — after a cooldown, allow **exactly one** probe. Success closes;
  failure reopens and restarts the cooldown.

**The rolling window matters.** "N failures ever" would trip on a provider that
failed five times over a month, which says nothing about its health now.

**Exactly one probe matters.** Letting several through recreates the thundering
herd against a service at its most fragile moment.

### Fallback and escalation

When one provider fails, try another. The design question is *what counts as
"another"* — a different instance, a different vendor, a degraded response, or
a cached one.

Fallback has a cost worth naming: falling back to something **more expensive**
turns an availability incident into a billing incident.

---

## 3. How we used it in ModelMux

### The taxonomy IS the retry policy

`providers/base.py` has defined four error types since Stage 1. Stage 5 is
where they stop being documentation and become control flow:

```python
ProviderTimeout       retry
ProviderRateLimited   retry with backoff, honour Retry-After
ProviderServerError   retry
ProviderBadRequest    NEVER retry -- deterministic
```

Retrying a dead API key means three attempts, three identical 401s, three times
the latency, and a caller waiting for an answer that was never coming.

`RETRYABLE` is defined by **exclusion**, so a new error type added to `base.py`
defaults to "do not retry" — the safe direction to fail.

### Honour Retry-After over our own guess

```python
delay = backoff_seconds(attempt, base)
if isinstance(exc, ProviderRateLimited) and exc.retry_after_seconds:
    delay = max(delay, exc.retry_after_seconds)
```

`max`, not replacement: if our backoff is already longer, the provider's hint
should not make us *more* aggressive.

### The bug: two probes instead of one

The `OPEN -> HALF_OPEN` transition returned `True` without claiming the probe:

```python
if elapsed >= cooldown_seconds:
    self.state = CircuitState.HALF_OPEN
    self.probe_in_flight = False     # <-- WRONG
    return True
```

The next caller fell through to the `HALF_OPEN` branch, found the flag unset,
and was also allowed past. **Two requests hitting a recovering provider at the
exact moment it is least able to cope** — the failure mode the breaker exists
to prevent.

> **The lesson: a state machine with a side effect on a transition needs that
> side effect on EVERY path that performs the transition**, not just the one you
> had in mind while writing it.

Caught by `test_half_open_allows_exactly_one_probe`, which is a test I only
wrote because the spec named that transition explicitly.

### 502 vs 503

- **502** — we tried and they failed
- **503** — every circuit was open, so nothing was attempted; carries
  `estimated_recovery_seconds`

An operator reading logs during an incident needs to tell "they broke" from "we
have stopped trying" — they call for different responses.

### Escalation is a switch, not a law

SPEC 8.7 says an exhausted tier should escalate rather than fail. Correct for
quality, but during a Groq outage *every* small-tier request escalates to the
large tier, and an availability incident silently becomes a bill.

`escalate_on_tier_exhausted` is a config flag so it can be turned off mid-
incident without a code change. (DECISIONS.md D5.)

### Circuit state is per process, and that is stated

SPEC allows in-memory single-process. With several workers, each keeps its own
view — a provider can be open in one and closed in another.

**The consequence is uneven traffic to a failing provider, not wrong answers.**
Sharing the state would mean putting it in Redis: a dependency on the thing
most likely to be down during exactly the incident the breaker is for.

### Testing failure without waiting for one

Every test here runs in under a second because `MockProvider.fail_with` makes
failure deterministic:

```python
MockProvider(fail_with="server_error")   # or timeout / rate_limited / bad_request
```

That parameter was written on Day 1 with no immediate use. It is the reason the
entire state machine — including `half_open -> open` on probe failure — is
covered without a real outage.

---

## 4. Interview questions

**Q: When should you NOT retry?**
When the failure is deterministic — 400, 401, 404 — because the second attempt
fails identically while costing latency and load. And when the operation is not
idempotent, unless you have an idempotency key, because a retry can duplicate a
side effect like a payment.

**Q: Why does backoff need jitter?**
Without it, all clients that failed at the same moment retry at the same
moment. That synchronised burst re-triggers the overload they are backing off
from — the thundering herd. Randomising spreads the load over the window and is
what makes backoff actually work.

**Q: Explain the circuit breaker states.**
CLOSED: normal, counting failures in a rolling window. OPEN: fail fast without
calling, so you stop paying the timeout to rediscover a known failure.
HALF_OPEN: after a cooldown, allow exactly one probe — success closes it,
failure reopens and restarts the cooldown.

**Q: Why "exactly one" probe in half-open?**
Because the provider is at its most fragile at that moment. Letting several
through recreates the herd the breaker exists to prevent. *I got this wrong: my
OPEN→HALF_OPEN transition returned true without claiming the probe, so two
requests got through. A test named after that specific transition caught it.*

**Q: Why a rolling window rather than a total failure count?**
"N failures ever" trips on a provider that failed five times over a month,
which says nothing about its health now. The question is whether it is failing
*currently*, so the window has to be bounded in time.

**Q: Where should circuit breaker state live?**
In memory is simplest and is correct for a single process. Shared state (Redis,
a coordination service) gives consistency across workers but adds a dependency
that may itself be down during the incident. *Mine is per-process, documented:
the cost is uneven traffic to a failing provider, never wrong answers.*

**Q: What is the danger of fallback?**
Falling back to something more expensive turns an availability incident into a
cost incident. Falling back to something less capable turns it into a quality
incident. *Mine escalates to a pricier tier, which is right for quality — so it
is behind a config flag that can be switched off mid-outage.*

**Q: How do you test resilience without waiting for a real outage?**
Make failure injectable. *My fake provider takes a `fail_with` parameter naming
one of the four error types, so retry, fallback and every circuit transition are
deterministic and run in under a second.* Beyond that: fault injection in
staging, and chaos engineering in production if you have the maturity for it.

**Q: Retry, fallback and circuit breaking all handle failure. Why have all
three?**
They answer different questions. Retry handles a blip in an otherwise healthy
provider. Fallback handles one provider being down while others are not.
The circuit breaker handles a provider being down *and* stops you paying the
timeout to rediscover it on every single request.
