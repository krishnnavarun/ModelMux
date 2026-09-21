# 09 — The whiteboard version, and how it scales

*"Design a system that routes LLM requests to the cheapest capable model."*

This is a real system-design question, and you have actually built it. This
file is how to present it in 30-45 minutes.

---

## 1. Clarify first (2 minutes — do not skip)

Asking these signals seniority before you draw anything:

| Ask | Why it changes the design |
|---|---|
| **Traffic volume?** | 10 rps vs 10,000 rps changes everything downstream |
| **Latency budget?** | Decides whether classification can be synchronous |
| **Is there repetition in the traffic?** | Decides whether a cache is the main lever or a footnote |
| **Do we control the prompts, or is it open user input?** | Open input means adversarial cache-poisoning matters |
| **What's the price spread between tiers?** | **This bounds the whole value proposition** |
| **Is quality regression acceptable, and who measures it?** | The real success metric |

> **The one that impresses:** *"What's the price spread? If the models are
> within 2× of each other, a perfect router saves almost nothing and we should
> talk about caching instead."*

---

## 2. The design, drawn

```
                    ┌──────────┐
   client  ────────►│   API    │
                    └────┬─────┘
                         │
                 ┌───────▼────────┐
                 │  rate limiter  │  token bucket, per caller
                 └───────┬────────┘  BEFORE everything: rejects cost nothing
                         │
                 ┌───────▼────────┐
                 │ semantic cache │◄──────► Redis
                 └───────┬────────┘  embed → cosine → threshold → guard
                    miss │           HIT: $0, ~11ms, done
                         │
                 ┌───────▼────────┐
                 │   classifier   │  NO LLM CALL. < 20ms.
                 │ heuristic+k-NN │  signals → weights → floors → reconcile
                 └───────┬────────┘
                         │ tier
                 ┌───────▼────────┐
                 │    dispatcher  │  retry → fallback → escalate
                 │   + breakers   │
                 └───────┬────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
      ┌───────┐     ┌────────┐     ┌────────┐
      │ small │     │  mid   │     │ large  │
      └───────┘     └────────┘     └────────┘
                         │
                 ┌───────▼────────┐
                 │  log every row │──► SQLite / Postgres
                 └────────────────┘    incl. every failure path
```

**Order matters and is defensible at every step:**

1. **Rate limit first** — a rejected request must cost nothing.
2. **Cache before classify** — why classify something you won't send?
3. **Classify before dispatch** — obviously.
4. **Log everything, always** — including failures, or the metrics lie.

---

## 3. The key design decisions to volunteer

### "The classifier must not call an LLM"

> State this early and unprompted. It's the constraint that makes the problem
> interesting, and it rules out the lazy answer. Consequence: heuristics plus a
> **small local** embedding model, CPU, single-digit milliseconds.

### "Cache and classifier share an embedding model"

One model load, two uses. Keeps memory and startup cost down.

### "A tier is a list, not a model"

Within-tier fallback for free. Adding a provider is a config entry.

### "Cost is attributed to whoever actually answered"

Not to the tier's first entry. **This was a real bug** — pricing a fallback at
the price of the model it replaced inflated figures 40×.

### "Every row is written, including failures"

If failures don't write rows, your error rate is unknowable and your latency
percentiles are computed on a biased sample.

---

## 4. Scaling: what breaks, in order

### At ~100 rps — per-process state

**Problem:** the rate limiter and circuit breakers are in-process. With N
workers, the effective rate limit is N× intended, and each worker has its own
opinion of whether a provider is healthy.

**Fix:** move both to Redis. Rate limiting becomes an atomic
`INCR`+`EXPIRE` or a Lua token bucket. Breaker state becomes a shared key with
a TTL.

**Tradeoff:** a Redis round-trip on every request, and Redis becomes a hard
dependency in the request path rather than an optimisation.

### At ~500 rps — classification on the event loop

**Problem:** 12ms of synchronous CPU per request blocks the loop. One worker
saturates near 80 rps and then adds latency to *every* concurrent request.

**Fix:** move the embedding forward pass to a thread pool
(`run_in_executor`) or a separate inference process. Or: **skip the embedding
entirely when the heuristic is confident** — most prompts don't need both
classifiers, and this is the cheapest win available.

### At ~10⁵ cache entries — the linear scan

**Problem:** brute-force cosine is 11.2ms at 10k and scales linearly. At 100k
it's ~110ms and blows the 30ms budget.

**Fix:** an ANN index — Redis Search with HNSW, or FAISS. **Tradeoff: you
accept approximate recall.** For a cache that's fine; a missed hit costs a
model call, not a wrong answer.

### At any real write volume — SQLite

**Problem:** single writer.

**Fix:** Postgres. Nothing to port — no ORM, plain SQL. Then partition the
requests table by time, because it's append-only and queried by window.

### Always — the provider is the bottleneck

Nothing above matters next to a 2-second model call. **The real scaling
levers are the cache hit rate and routing more traffic to faster models** —
which is what the system already does.

---

## 5. What I'd change if I started again

Say these unprompted; they read as reflection rather than defensiveness.

1. **Build quality measurement first.** The entire thesis is "cheaper routing
   is acceptable" and I built every measurement except the one that tests it.
2. **Make the cost function shared from day one.** The server and the eval
   computed cost separately, and they drifted — by 40×.
3. **Put the rate limiter and breakers in Redis immediately.** Per-process
   state is a known-wrong answer I knowingly shipped; better to pay early.
4. **Separate the "is this hard?" signal from the "which tier?" decision.**
   Right now the thresholds are baked into scoring. A difficulty *score* plus a
   separate tier *policy* would let the policy change per customer without
   retuning the classifier.
5. **Version the cache keys by model.** If the model behind a tier changes,
   cached answers from the old one are silently still served.

---

## 6. Extensions they might push on

| "What if…" | Answer |
|---|---|
| **…you need streaming?** | SSE was deliberately cut (D24). It breaks the cache-then-classify flow — you can't cache a stream until it completes, and you must decide the tier before the first token. You'd stream through and write to cache on completion. |
| **…a user needs a guaranteed model?** | An override in the request that bypasses routing. The row still logs that routing was bypassed, so the stats stay honest. |
| **…you need per-user budgets?** | The data is already there — every row has a cost. It's an aggregate query plus a pre-flight check, in the same place as the rate limiter. |
| **…quality regresses in production?** | Today you wouldn't know, which is the honest answer. You'd need sampled grading, or a feedback signal (thumbs, retries, follow-up rate) fed back to the routing thresholds. **A user rephrasing immediately is a strong implicit signal the answer was bad.** |
| **…a provider leaks between tenants via cache?** | Real risk of shared semantic caching. Partition the cache by tenant, at the cost of hit rate. That's a security/economics tradeoff, and it should be the tenant's choice. |
| **…you want to route on cost *and* latency?** | The signals already exist. Make the tier choice a policy function over (difficulty, latency SLA, budget remaining) rather than difficulty alone. |

---

## 7. The closing summary

> *"The core insight is that classification is cheap and model calls are
> expensive — twelve milliseconds versus two seconds, and a fraction of a cent
> versus multiple cents. That asymmetry is what makes routing worth doing at
> all.*
>
> *But the measurement taught me the limit: **savings are bounded by the price
> spread you're given.** With a compressed ladder I measured 6.6%, and with the
> intended ladder the same routing projects to 37%. The classifier was
> identical in both. That's the number I'd want a team to understand before
> committing to this architecture — and the latency win, 2.5× at the median,
> turned out to be the more robust result."*
