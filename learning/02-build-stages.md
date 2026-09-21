# 02 — The six build stages

The spec defines six stages, each with a **completion criterion** — a thing
that must be *demonstrably true*, not a feeling that the code looks done.

> **Interview framing:** "Each stage had an exit criterion written before the
> code. Stage 2's was *a written list of misroutes* — the stage was only done
> when I could show how the naive router failed, not when it ran."

| Stage | Built | Completion criterion | Status |
|---|---|---|---|
| 1 | Bare proxy | A curl returns an answer **and the row is in SQLite** | ✅ closed 2026-09-21 |
| 2 | 2 providers + naive rule | **A written list of misroutes** exists | ✅ D12 |
| 3 | Real classifier | Accuracy for heuristic / embedding / hybrid | ✅ D15 |
| 4 | Semantic cache | Measured hit rate + justified threshold + **documented failure case** | ✅ D16 |
| 5 | Resilience | System keeps serving with a provider **forced to fail** | ✅ D20 |
| 6 | Metrics, eval, dashboard | README has **real numbers, no placeholders** | ✅ D33 |

---

## Stage 1 — Bare proxy

**Built:** FastAPI app, one provider (Groq), request/response schemas, SQLite
logging with WAL mode, config loading with startup validation.

**The point of the stage:** prove the skeleton end to end before adding
intelligence to it. One provider, no routing, no cache — but a real database
row for every request from day one.

**What it taught:**

- **WAL mode** (`PRAGMA journal_mode=WAL`) lets readers and a writer work
  concurrently — necessary because the dashboard reads while requests write.
- **The database lives outside the project folder**, at `%LOCALAPPDATA%`. This
  project is under OneDrive, and OneDrive syncing a SQLite file while SQLite
  holds a lock on it can corrupt it — plus WAL adds `-wal` and `-shm` companion
  files that must stay consistent with each other. (D7)
- **Logging must never fail a request.** A database error is logged and
  swallowed; the caller still gets their answer.

**Why it stayed open for weeks:** the criterion required a *live* 200, and no
API key worked. See `07-war-stories.md` #1 — the cause was an environment
variable, not the key.

---

## Stage 2 — Two more providers, and a router built to fail

**Built:** Google and Anthropic adapters, the provider registry, the 4-error
taxonomy, and a **deliberately bad** router.

**The naive rule:** complexity = token_count / 600, capped at 1.0. With
thresholds 0.33/0.66 that means ≤198 tokens → small, ≤396 → mid, else large.

**The criterion was a list of failures, not a working router.** Results, naive
mode on the 50-prompt tuned set:

| | count | meaning |
|---|---|---|
| correct | 17 (34%) | — |
| **too cheap** | **28** | hard prompt → weak model. Quality risk. |
| too expensive | 5 | easy prompt → strong model. Money wasted. |

On the held-out set (n=32) it scores **31%**, with **20 too cheap**. Reproduce
either with `eval/run_classifier_eval.py`, which prints all four modes against
both sets.

**The prompt set was designed with traps** — categories where length and
difficulty point in opposite directions:

- short + hard: *"Prove there are infinitely many primes."*
- long + easy: a 900-token pasted log ending *"what does this say?"*

> **Building something bad on purpose, and measuring exactly how bad, is what
> justified the next stage.** Without the misroute list, "we need a better
> classifier" is an opinion.

**The error taxonomy** built here carried the whole project:

| Exception | HTTP | Retry? | Why |
|---|---|---|---|
| `ProviderTimeout` | 504 | **Yes** | transient by definition |
| `ProviderRateLimited` | 429 | **Yes**, honour `Retry-After` | transient |
| `ProviderServerError` | 502 | **Yes** | their problem, may pass |
| `ProviderBadRequest` | 502 | **Never** | deterministic — bad key, unknown model |

> Retrying a dead API key produces three identical 401s and triples your
> latency. **The taxonomy exists to make "is this worth retrying?" a property
> of the error type, not a guess at the call site.**

---

## Stage 3 — The real classifier

**Built:** `signals.py` (feature extraction), `heuristic.py` (weighted scoring),
`embedding.py` (k-NN over labelled examples), and the hybrid reconciliation.

### The signals

| Signal | Detects |
|---|---|
| `token_count` | size (the only one the naive router had) |
| `reasoning_markers` | prove, derive, critique, compare, explain why… |
| `reasoning_depth` | 0 none / 1 medium / 2 hard |
| `is_lookup` | short factual question shape |
| `multi_question` | several questions in one prompt |
| `has_code` | code blocks or code-like density |
| `question_context_ratio` | how much is question vs pasted context |
| `output_length_request` | "write 2000 words", "briefly" |
| `language_complexity` | vocabulary and sentence structure |

### The weights, and why they are what they are

```python
WEIGHTS = {
    "reasoning":      0.34,   # the strongest single predictor of difficulty
    "length":         0.20,   # still matters — just not alone
    "multi_question": 0.14,
    "code":           0.12,
    "output_length":  0.10,
    "language":       0.10,
}
```

### The floors — the most important design idea in the classifier

A weighted **average** dilutes a strong signal. *"Prove Fermat's Last
Theorem."* is 6 tokens: the length component drags the average down and the
prompt routes cheap — exactly the failure the whole stage exists to fix.

So the strongest single indicator sets a **floor** the average cannot pull
below:

```python
FLOOR_HARD_REASONING   = 0.75   # prove, derive, critique, design a…  → clears mid_max → large
FLOOR_MEDIUM_REASONING = 0.45   # explain why, compare, debug…        → clears small_max → mid
FLOOR_MULTI_QUESTION   = 0.42
```

And the mirror case, a **multiplier** not a subtraction:

```python
LOOKUP_DISCOUNT = 0.45   # confirmed short factual lookup
```

It's a multiplier so it can never drag a genuinely complex prompt below zero,
and it is **skipped entirely for multi-part questions** — five easy questions
in one prompt is not an easy prompt.

> **Interview point:** "An average is the wrong aggregator for evidence of
> difficulty. One emphatic signal should be able to override five weak ones,
> which is what the floors do — and `REASONING_SATURATION = 3` stops a fourth
> marker adding anything, because three distinct cues is already emphatic."

### The embedding classifier

- Model **`all-MiniLM-L6-v2`**: 384 dimensions, CPU, ~10ms
- **k-NN with k=5** against **60 labelled examples** in `eval/labelled.json`
- Confidence = how many of the 5 neighbours agreed
- **`EMBED_MAX_CHARS = 400`** — and this is both a performance fix and a
  correctness fix: embedding a whole 4,000-character prompt cost 55ms *and*
  measured mostly pasted context rather than the question. `question_part()`
  clips to the question first.

### Why hybrid ships despite scoring worse

Covered in `01` and `06` (D15). The one-liner: **it eliminates every too-cheap
misroute, and the two error types are not equally bad.**

### The methodological catch worth telling

Mid-stage, the realisation: the weights had been **tuned on the same 50 prompts
accuracy was being reported on**. That is a training score, not generalisation.

Fix: `eval/holdout.json` — 32 prompts written *after* tuning finished, never
used to set a single constant. **Only the held-out column is ever quoted.**
`run_classifier_eval.py` prints both sets side by side with the gap, because
**the gap between them is the size of the overfitting**, stated as a number.

---

## Stage 4 — The semantic cache

**Built:** Redis-backed cache storing an embedding + answer per entry, cosine
similarity search, a lexical inversion guard, and an in-process mirror.

**Why semantic and not exact-match:** "What's the capital of France?" and
"capital of France?" are the same question. Exact matching serves neither.

**The most dangerous setting in the project:**

```yaml
similarity_threshold: 0.88   # MEASURED, not guessed
```

- **Too loose:** semantically *different* prompts share an answer — across
  users. A correctness and privacy failure.
- **Too tight:** hit rate collapses and the cache earns nothing.

**It was measured**, not chosen: `tune_cache_threshold.py` sweeps it against
35 labelled pairs — 15 equivalent, 20 deliberately confusable.

**And the finding was that no threshold is fully safe.** A pair at 0.99
similarity had *opposite* correct answers. Embedding similarity measures
topical closeness, not semantic equivalence — negation barely moves a vector.

So the threshold is backed by a **lexical inversion guard** that catches
antonym/negation flips the embedding misses, plus `LENGTH_RATIO_MIN = 0.5` and
`MAX_CANDIDATES = 3`.

> **The lesson: knowing when a knob cannot solve the problem.** The honest
> answer wasn't a better threshold, it was a second mechanism — and a test
> that *deliberately produces a wrong answer* at a loose threshold, so the
> failure mode is demonstrated rather than described.

**Measured:** 80% hit rate on rephrasings, 0% false hits on the confusable
set, 11.2ms lookup at 10,000 entries against a 30ms budget.

---

## Stage 5 — Resilience

**Built:** retry with jittered exponential backoff, within-tier fallback,
tier escalation, and a circuit breaker per provider.

**The criterion: the system keeps serving with one provider forced to fail** —
which is why `MockProvider.fail_with` was built on day 1. Deterministic
failure, no real outage needed.

### The circuit breaker state machine

```
        5 failures in 60s
CLOSED ──────────────────► OPEN
  ▲                          │ 30s cooldown
  │ probe succeeds           ▼
  └──────────────────── HALF_OPEN ──┐
                            ▲       │ probe fails
                            └───────┘ (cooldown RESTARTS)
```

Three details people get wrong:

1. **The threshold is N failures *within a window*, not N ever.** A provider
   that failed three times over a month is not unhealthy now.
2. **HALF_OPEN allows exactly one probe.** Letting several through is the
   thundering herd the breaker exists to prevent.
3. **A failed probe restarts the cooldown.** It does not grant another
   immediate attempt. This is the transition people forget — and this project
   shipped it wrong once, letting two probes through.

### Backoff must be jittered

```python
backoff = base * 2**attempt * random_factor
```

> Without jitter, every client that failed together retries together —
> re-triggering the overload they're backing off from.

### Escalation on exhaustion is a *switch*, not a default

`escalate_on_tier_exhausted: true` sends a request one tier *up* when its tier
is exhausted. **During a provider outage that converts an availability problem
into a cost spike**, which should be a deliberate choice — so it's a config
flag, not hardcoded behaviour.

---

## Stage 6 — Metrics, evaluation, dashboard

**Built:** `/v1/stats` aggregates with exact percentiles, `/v1/requests`,
`/v1/compare`, the rate limiter, `run_eval.py`, `grade_quality.py`, and a
single-file dashboard.

**The criterion: real numbers in the README, no placeholders.** It stayed open
longest — see `07` #1.

### The harness protects the result from its author

`run_eval.py` **refuses to run against mock providers** unless `--simulated` is
passed, and prefixes every line of such a run with `SIMULATED`.

> A plausible fake in a results table is worse than no number at all.

That guard still didn't stop it publishing figures inflated ~40× from *real*
data — the eval priced every call at the tier's first provider while Groq
answered all of them. See `07` #3. **Guarding one path doesn't guard the
thing.**

### Two deliberate cuts

`GET /v1/stream` (SSE) and the React/Vite/Recharts dashboard were **cut on
purpose** (D24). The dashboard is one static HTML file served from the API's
own origin — no build step, no CORS setup, no `node_modules`.

> **Knowing what not to build is a senior signal.** The spec asked for React;
> the value was a working chart, and one file delivered it.

### Percentiles computed in Python, from successes only

Failed requests have latencies that mean something different — a 30-second
timeout is not a slow answer, it's no answer. Mixing them makes p95 describe
nothing.
