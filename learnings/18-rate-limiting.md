# Rate Limiting & Quotas

---

## 1. What it is

**Rate limiting** caps how many requests one caller may make in a period.

Three reasons to do it, and they are not the same:

| Reason | What it protects |
|---|---|
| **Capacity** | your servers, from being overwhelmed |
| **Fairness** | other callers, from one noisy neighbour |
| **Cost** | your budget, when each request spends real money |

The third is the one that applies here and it is the least discussed. When a
request costs money, an unlimited caller is an unlimited invoice.

---

## 2. How it works

### Fixed window

Count requests per clock period; reset at the boundary.

```
11:59:00 - 11:59:59   [60 allowed]
12:00:00 - 12:00:59   [60 allowed]
```

**The boundary problem:** a caller sends 60 at 11:59:59 and 60 more at
12:00:00 — **120 requests in one second**, double the intended rate, at the
worst possible moment. Simple and cheap, but wrong exactly when load spikes.

### Sliding window log

Keep a timestamp per request; count those inside the window. Exact, and costs
memory proportional to the rate — you store every request you allowed.

### Token bucket

A bucket of capacity N refills at a steady rate. Each request takes one token;
no token means rejection.

```
capacity    60 tokens
refill      1 token/second  (= 60/min)

t=0s    [||||||||||] 60   burst of 60 allowed
t=0s    [          ]  0   61st rejected
t=10s   [|         ] 10   ten seconds -> ten tokens
```

Long-run rate is exactly the refill rate, and a short burst up to capacity is
still permitted — which is usually what you want, since real traffic is bursty.
Cost is two numbers per caller.

### Leaky bucket

Requests queue and drain at a fixed rate. Smooths output completely, but adds
latency and needs a queue. Token bucket allows bursts; leaky bucket does not.

### What a 429 should carry

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 20
```

`Retry-After` is what lets a well-behaved client back off correctly instead of
guessing — and a client that guesses usually guesses too short.

---

## 3. How we used it in ModelMux

### Token bucket, for the boundary reason

```python
# app/ratelimit.py
class RateLimiter:
    def __init__(self, per_minute: int):
        self.capacity = float(per_minute)
        self.per_second = per_minute / 60.0
```

A fixed window would have allowed 2x the configured rate across a boundary. For
a limit whose job is protecting a **budget**, "usually right, double at the
worst moment" is not good enough.

### Tokens are floats, deliberately

```python
self.tokens = min(capacity, self.tokens + elapsed * per_second)
```

Rounding down on every check would leak allowance and make the real rate
quietly lower than configured. `test_tokens_are_fractional` pins it.

### Checked before classification and before the cache

A rejected request must cost **nothing**. Checking after routing would still
burn ~11ms of embedding per rejection — most of what the limit exists to
prevent. It is the first thing the handler does after building its log record.

### New callers start full

```python
bucket = Bucket(tokens=self.capacity, ...)
```

Starting empty rejects everyone's first request, which to someone trying the
API for the first time is indistinguishable from the service being down.

### X-Forwarded-For is not trusted

```python
# X-Forwarded-For is deliberately NOT trusted. It is caller-controlled, so
# honouring it without a verified proxy in front would let anyone bypass the
# limit by inventing a header -- turning the protection into decoration.
```

> **The general rule: a security control keyed on caller-supplied data is not a
> control.** If the attacker picks the key, they pick their own bucket.

Behind a real load balancer this has to change — but the change is "trust
exactly N proxy hops", never a blanket trust of the header.

### Idle buckets are swept

One dict entry per distinct IP, forever, is a slow memory leak that an attacker
can accelerate on purpose. Buckets unused for an hour are evicted, at most once
a minute.

### A 429 still writes a database row

SPEC: nothing is silently dropped. A rejected request is still a request that
happened, and an operator investigating "the API keeps refusing me" needs to
see it in the log.

### Limitations, stated rather than hidden

- **Per process.** Several workers means the effective limit is
  `workers x rate`. Sharing state would put Redis on the hot path, for what is
  a safety net rather than a billing boundary.
- **Keyed by IP, because there is no auth.** An IP is shared behind a NAT and
  changes on mobile. Once authentication exists the key becomes the API key.

---

## 4. Interview questions

**Q: Compare fixed window, sliding window and token bucket.**
Fixed window is cheapest but allows 2x the rate across a boundary. Sliding
window log is exact but stores a timestamp per request. Token bucket gives the
exact long-run rate with a bounded burst, for two numbers per caller — usually
the right default.

**Q: Why does the fixed-window boundary problem matter?**
Because the double-rate burst happens at the boundary, not randomly — so every
client that retries on a schedule synchronises there. You get your worst traffic
spike at a predictable instant, which is the opposite of what a limiter is for.

**Q: What should a 429 response include?**
`Retry-After`, so a well-behaved client backs off correctly rather than
guessing. Ideally also the limit, remaining allowance, and reset time via
`RateLimit-*` headers. *Mine returns `retry_after_seconds` in the body and the
`Retry-After` header.*

**Q: How do you rate limit across multiple servers?**
Shared state — Redis with `INCR`+`EXPIRE`, or a token bucket in Lua for
atomicity. The trade is a network round trip on the hot path, plus a dependency
that can itself fail. *Mine is deliberately per process: the effective limit is
workers x rate, documented, because it is a budget safety net rather than a
billing boundary.*

**Q: What do you key the limit on?**
Whatever identifies the party you are protecting the resource from — API key or
account id ideally, IP as a fallback. **Never something caller-supplied**: if
the attacker picks the key, they pick their own bucket. *Mine uses IP because
there is no auth, and explicitly ignores X-Forwarded-For for that reason.*

**Q: Should a rate-limited request be logged?**
Yes. It is a real event, it is what an operator will be asked about, and a
sudden rise in 429s is a signal — either an attack or a limit set too low.
Logging only successes hides both.

**Q: Where in the request pipeline does the check go?**
As early as possible, before any expensive work. *Mine runs before
classification and before the cache lookup — checking afterwards would still
spend ~11ms of embedding on every rejected request, which is most of what the
limit is protecting.*

**Q: How do you stop the limiter itself becoming a memory leak?**
Bound the state. Evict idle entries on a schedule, or use a fixed-size
structure with eviction. One entry per distinct IP held forever is a leak an
attacker can drive deliberately.

**Q: Rate limiting vs a circuit breaker — what is the difference?**
A rate limiter protects *you* from *callers*: too many requests coming in. A
circuit breaker protects *you* from a *dependency*: too many failures going out.
Opposite directions, and both are needed. *This project has both, and they share
no code.*
