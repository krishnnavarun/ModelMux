# 01 — What the project is, and one request end to end

## 1. The problem, precisely stated

An application wired to a single frontier model pays premium prices for
trivial work.

| Prompt | Needs | Usually gets |
|---|---|---|
| "What is the capital of Japan?" | the cheapest model alive | Claude Opus 5 |
| "Prove that every finite integral domain is a field." | a frontier model | Claude Opus 5 |

Both cost the same. One of them shouldn't.

**Why not just let developers pick the model per call?** Because it pushes a
guess onto the caller at request time, and the safe guess is always "the big
one". Manual selection degrades to "everything expensive" within a sprint.

**So: remove the decision.** The caller sends a prompt. The router decides.

## 2. The three levers

ModelMux saves money three ways, in descending order of impact:

| Lever | Mechanism | Ceiling |
|---|---|---|
| **Routing** | Send easy prompts to cheap models | The price spread between tiers |
| **Caching** | Serve repeated/rephrased questions from Redis | The repetition rate of your traffic |
| **Failing well** | Retry, fall back, break circuits | Prevents outages, doesn't save money |

> **The routing ceiling is the one people miss.** If your tiers are 2× apart,
> perfect classification saves at most a bit under 50%. If they're 33× apart,
> the same classifier saves far more. **The classifier's quality and the
> ladder's spread multiply.** This project measured exactly that — see `05`.

## 3. One request, end to end

```
POST /v1/chat  {"prompt": "Explain why quicksort is O(n log n) on average"}
   │
   ▼
┌──────────────────────────────────────────────────────────────┐
│ 1. RATE LIMIT          ratelimit.py                          │
│    Token bucket, keyed by IP, 60/min.                        │
│    Checked BEFORE everything, so a rejected request is free. │
│    Over limit → 429 + Retry-After                            │
└──────────────────────────────────────────────────────────────┘
   │
   ▼
┌──────────────────────────────────────────────────────────────┐
│ 2. VALIDATE            schemas.py (Pydantic)                 │
│    max_prompt_chars 100,000. Malformed → 422                 │
└──────────────────────────────────────────────────────────────┘
   │
   ▼
┌──────────────────────────────────────────────────────────────┐
│ 3. CACHE LOOKUP        cache.py           BUDGET: < 30ms     │
│    Embed prompt → cosine-compare against stored vectors      │
│    → best 3 candidates → similarity ≥ 0.88?                  │
│    → lexical inversion guard (catches "is" vs "is not")      │
│    HIT  → return stored answer, cost $0, done                │
│    MISS → continue                                           │
└──────────────────────────────────────────────────────────────┘
   │ miss
   ▼
┌──────────────────────────────────────────────────────────────┐
│ 4. CLASSIFY            router.py + classifier/  BUDGET: <20ms│
│    NO LLM CALL — this is a hard constraint                   │
│                                                              │
│    a) signals.py   extract: token count, reasoning markers,  │
│                    code blocks, multi-question, lookup shape │
│    b) heuristic.py weighted score + floors + lookup discount │
│    c) embedding.py k-NN (k=5) vs 60 labelled examples        │
│    d) reconcile    hybrid: escalate on disagreement          │
│                                                              │
│    → complexity_score 0.0-1.0  →  small ≤0.33 <mid ≤0.66 <large │
└──────────────────────────────────────────────────────────────┘
   │
   ▼
┌──────────────────────────────────────────────────────────────┐
│ 5. DISPATCH            resilience.py + providers/            │
│    Walk the tier's provider LIST in order:                   │
│      circuit open?  → skip                                   │
│      call it        → retry transient errors (2×, jittered)  │
│      still failing? → next provider in tier (fallback)       │
│      tier exhausted?→ escalate one tier UP                   │
└──────────────────────────────────────────────────────────────┘
   │
   ▼
┌──────────────────────────────────────────────────────────────┐
│ 6. COST                config.cost_for_provider()            │
│    Priced by the provider that ACTUALLY answered             │
│    cost_if_large_usd = same tokens at baseline tier rates    │
└──────────────────────────────────────────────────────────────┘
   │
   ▼
┌──────────────────────────────────────────────────────────────┐
│ 7. CACHE WRITE + LOG   cache.py, db.py                       │
│    Store vector + answer (TTL 24h, max 10,000 entries)       │
│    Write ONE database row — including every failure path     │
└──────────────────────────────────────────────────────────────┘
   │
   ▼
Response, with the full routing decision attached
```

### What comes back

```json
{
  "response": "Quicksort partitions around a pivot...",
  "routing": {
    "tier": "mid",
    "provider": "groq",
    "model": "openai/gpt-oss-120b",
    "cache_hit": false,
    "complexity_score": 0.48,
    "escalated": false,
    "fallback_fired": false,
    "latency_ms": 2140,
    "classify_ms": 12,
    "cost_usd": 0.00031,
    "cost_if_large_usd": 0.00820,
    "signals": { "reasoning_markers": 1, "token_count": 11, "...": "..." }
  }
}
```

**The `signals` object is never removed.** That is a spec-level
non-negotiable. If you cannot see *why* a prompt went where it did, the
classifier is a black box no matter how accurate it is.

## 4. The five non-negotiables

These are spec section 2, and every one of them shaped the code.

| # | Constraint | Why it exists | What it forbids |
|---|---|---|---|
| 1 | **No LLM call in the classification path** | Calling a model to choose a model defeats the purpose — cost *and* latency | No GPT-based classifier, no "just ask Haiku" |
| 2 | **Classification < 20ms, cache < 30ms** | Routing overhead must disappear against a 2-second model call | No large embedding models, no network in the hot path |
| 3 | **Every decision explainable** | A router you can't audit is one you can't trust with spend | The `signals` object can never be dropped |
| 4 | **Nothing silently dropped** | Missing rows make the metrics lie | Every failure path writes a row too |
| 5 | **Configuration over code** | Tiers/prices/thresholds change weekly | No hardcoded model names or prices |

> **Interview gold:** constraint 1 is the one that makes the project
> interesting. Anyone can classify prompts with an LLM. Doing it in 12ms on
> CPU with no network call is the actual engineering problem.

## 5. The three classifier modes

Set by one line: `classifier.mode` in `config.yaml`.

| Mode | How it decides | Held-out accuracy | Character |
|---|---|---|---|
| `naive_tokens` | Token count alone, linear to a 600-token saturation point | **31%** | The deliberate strawman |
| `heuristic` | Weighted signals + floors + lookup discount | **88%** | Most accurate |
| `embedding` | k-NN (k=5) against 60 labelled examples | **72%** | Most cautious |
| `hybrid` | Both, escalating on disagreement | **72%** | **Shipped** |

### Why the naive mode exists at all

Stage 2 deliberately built a bad router **and catalogued how it was bad**.
Token count measures *length*, and length is not *difficulty*:

- "Prove Fermat's Last Theorem." — 6 tokens, needs a frontier model
- A 900-token pasted log ending "what does this say?" — long, trivially easy

The naive router got **20 of 50 prompts too cheap**. That failure catalogue is
what justified building the real classifier — and the tests that assert the
naive router's wrong behaviour are marked `stage2_failure` on purpose, so that
when Stage 3 landed and they broke, the break *was the signal it worked*.

### Why the less accurate mode ships

This is the single best thing to talk about in an interview. Full treatment in
`06-decisions-defended.md` (D15), but the shape:

|  | accuracy | too cheap | too expensive |
|---|---|---|---|
| heuristic | **88%** | **4** | 0 |
| hybrid | 72% | **0** | 9 |

Hybrid is 16 points worse and ships, because **the two error types are not
equally bad**. Routing too cheap means a hard question answered badly by a weak
model — a trust failure. Routing too expensive means you overpaid by a fraction
of a cent — a money failure. The project's whole premise is that trust is worth
more than the money.

> **A single accuracy number would have ranked the heuristic first and hidden
> that it sends hard prompts to weak models.** The direction of an error
> matters more than its frequency.

## 6. Where the money actually went

Measured live, 2026-09-21, 32 held-out prompts, 0 failures, $0.027 total:

| | ModelMux | baseline (all to top tier) | |
|---|---|---|---|
| p50 latency | **2,803 ms** | 6,973 ms | **2.5× faster** |
| p95 latency | 9,037 ms | 9,019 ms | unchanged |
| cost / 1k, run 1 | $0.4121 | $0.4411 | 6.6% |
| cost / 1k, run 2 | $0.4181 | $0.4253 | **1.7%** |

**Why two different answers?** Same prompts, same config, same routing. The
only thing that changed is how long the models chose to answer. **The A/B
measurement is noise-dominated** — the real routing effect, measured by
repricing the *same* tokens at baseline rates, is **3-4%**.

**And why is even that small?** Only one API key ever worked, so the large tier
falls back to `gpt-oss-120b` — the same model the mid tier uses. The ladder
compresses to 2×, and 16 of 32 prompts route to "large", where routed and
baseline are byte-for-byte the same call. The 9 prompts that do route cheap
produce only 5-8% of all output tokens, and save half of that. (D36)

**p95 is unchanged, and that's correct.** The tail is the hard prompts, which
route large either way. Routing improves the *typical* request and leaves the
worst case alone.

> **The finding worth quoting: a router's savings are bounded by the price
> spread it is given.** Perfect classification earns nothing on a flat ladder.
> That is a property of the economics, not a defect in the classifier — and it
> only became visible by measuring rather than projecting.
