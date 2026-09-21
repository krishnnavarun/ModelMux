# 05 — Every number, limit, threshold and price

This is the reference sheet. **If you are asked "what were the constraints?",
everything here is a valid answer.** Each number has a *reason*, and the reason
is what gets you marks — not the digit.

---

## 1. The hard performance budgets (spec §2)

| Budget | Limit | Measured | Headroom |
|---|---|---|---|
| **Classification** | < 20 ms | **11-12 ms** p50 | ~40% |
| **Cache lookup** | < 30 ms | **11.2 ms** at 10,000 entries | ~60% |

**Why these numbers exist:** routing overhead has to disappear against a
~2,000ms model call. If deciding where to send a prompt costs 200ms, you've
spent 10% of the latency budget on bookkeeping.

⚠️ **Caveat you must state:** the 11-12ms figure was measured on an **idle
process**. The test guarding it is intermittently flaky in the full suite
(reporting ~85ms), for reasons never explained. Don't quote it bare.

---

## 2. Request limits (`config.yaml → limits`)

| Setting | Value | Why |
|---|---|---|
| `max_prompt_chars` | **100,000** | Bounds memory and embedding cost. Rejected at validation, before any work. |
| `max_tokens_default` | **1,000** | Default output cap when the caller doesn't specify |
| `rate_limit_per_minute` | **60** | Per-caller token bucket, keyed by IP |

### The `max_tokens` trap — a real bug from this project

With reasoning models (`openai/gpt-oss-*`), **`max_tokens` is a budget for
internal reasoning AND the answer**. Set at 20, the whole budget went to
reasoning and `content` came back **empty** with HTTP 200 — a fully billed,
"successful" response containing nothing.

> **Reasoning tokens are billed as output tokens and never appear in the text
> you get back.** Any cost model that counts returned characters understates
> spend.

---

## 3. Classifier thresholds (`config.yaml → classifier`)

| Setting | Value | Meaning |
|---|---|---|
| `mode` | `hybrid` | One line to switch to heuristic / embedding / naive |
| `thresholds.small_max` | **0.33** | score ≤ 0.33 → small |
| `thresholds.mid_max` | **0.66** | 0.33 < score ≤ 0.66 → mid; above → large |
| `escalate_on_low_confidence` | `true` | Escalate rather than guess |
| `confidence_threshold` | **0.60** | Below 3/5 neighbours agreeing → escalate |
| `naive.saturation_tokens` | **600** | Naive mode only: token count mapping to score 1.0 |

**The naive mode's effective behaviour** (for the Stage 2 strawman):
≤198 tokens → small, ≤396 → mid, >396 → large.

## 4. Heuristic internals (`app/classifier/heuristic.py`)

### Weights — must sum to 1.0

```python
WEIGHTS = {
    "reasoning":      0.34,   # strongest single predictor of difficulty
    "length":         0.20,   # still matters, just not alone
    "multi_question": 0.14,
    "code":           0.12,
    "output_length":  0.10,
    "language":       0.10,
}
```

### Floors and the discount

| Constant | Value | Effect |
|---|---|---|
| `FLOOR_HARD_REASONING` | **0.75** | clears `mid_max` → forces **large** |
| `FLOOR_MEDIUM_REASONING` | **0.45** | clears `small_max` → forces **mid** |
| `FLOOR_MULTI_QUESTION` | **0.42** | forces mid |
| `LOOKUP_DISCOUNT` | **0.45** | multiplier on confirmed short factual lookups |
| `REASONING_SATURATION` | **3** | a 4th reasoning marker adds nothing |

> **Why floors at all?** A weighted *average* dilutes a strong signal. *"Prove
> Fermat's Last Theorem."* is 6 tokens — the length component drags the average
> down and it routes cheap. The floor lets the strongest single indicator
> override the average. **This is the single most important design idea in the
> classifier.**

> **Why a multiplier for the discount, not a subtraction?** A multiplier can't
> drag a genuinely complex prompt below zero. And it's **skipped entirely for
> multi-part questions** — five easy questions in one prompt is not an easy
> prompt.

## 5. Embedding classifier (`app/classifier/embedding.py`)

| Constant | Value | Why |
|---|---|---|
| `MODEL_NAME` | `all-MiniLM-L6-v2` | 384 dims, CPU, ~10ms |
| `DEFAULT_K` | **5** | odd number → majority vote can't tie 50/50 |
| `EMBED_MAX_CHARS` | **400** | Both a perf fix (55ms → ~10ms) and a correctness fix |
| labelled examples | **60** | the k-NN reference set, `eval/labelled.json` |
| dimensions | **384** | small enough for a fast brute-force scan |

---

## 6. Cache settings (`config.yaml → cache`, `app/cache.py`)

| Setting | Value | Why |
|---|---|---|
| `similarity_threshold` | **0.88** | ⚠️ **The most dangerous setting in the project.** Measured, not guessed. |
| `ttl_seconds` | **86,400** (24h) | Bounds *staleness* |
| `max_entries` | **10,000** | Bounds *memory* and keeps the scan inside 30ms |
| `MAX_CANDIDATES` | **3** | Only the top 3 go through the guard |
| `LENGTH_RATIO_MIN` | **0.5** | A prompt half the length is probably a different question |
| `DTYPE` | `float32` | Halves memory vs float64 at no useful precision cost |

**Key spaces:** `mm:vec:` (vectors), `mm:ans:` (answers), `mm:index` (live ids),
`mm:version`. Vector and answer are written in one **pipeline** so they cannot
diverge.

### Why the threshold is dangerous, in one sentence each

- **Too loose** → semantically different prompts share an answer, **across
  users**. A correctness *and* privacy failure.
- **Too tight** → hit rate collapses and the cache earns nothing.

### And why no threshold is fully safe

A measured pair at **0.99 similarity had opposite correct answers.** Embedding
similarity is topical closeness, not semantic equivalence — **negation barely
moves a vector.** Hence the lexical inversion guard, which is a *second
mechanism*, not a better number.

---

## 7. Resilience settings (`config.yaml → resilience`)

| Setting | Value | Why |
|---|---|---|
| `connect_timeout_seconds` | **5** | Can't reach the host = dead. Fail fast. |
| `request_timeout_seconds` | **30** | A model legitimately thinking for 20s is not a failure. |
| `max_retries` | **2** | 3 attempts total |
| `backoff_base_seconds` | **0.5** | Exponential, **jittered** |
| `circuit_failure_threshold` | **5** | failures **within the window** |
| `circuit_window_seconds` | **60** | 5 failures over a month ≠ unhealthy now |
| `circuit_cooldown_seconds` | **30** | Before allowing one probe |
| `escalate_on_tier_exhausted` | `true` | A **switch**, because it converts an outage into a cost spike |

**Backoff:** `base × 2^attempt × jitter`. Without jitter every client that
failed together retries together, re-triggering the overload.

**Which errors retry:**

| Error | Retry? |
|---|---|
| `ProviderTimeout` | ✅ transient |
| `ProviderRateLimited` | ✅ honour `Retry-After` |
| `ProviderServerError` | ✅ their problem, may pass |
| `ProviderBadRequest` | ❌ **never** — deterministic (bad key, unknown model) |

---

## 8. Prices — per 1,000 tokens

⚠️ **Dated snapshot, not a source of truth.** Prices drift; verify before
quoting.

| Tier | Model | Input | Output | Verified |
|---|---|---|---|---|
| small | `openai/gpt-oss-20b` | $0.000075 | $0.0003 | 2026-09-08 |
| mid | `openai/gpt-oss-120b` | $0.00015 | $0.0006 | 2026-09-08 |
| **large** | `claude-opus-5` | **$0.005** | **$0.025** | 2026-09-21 |
| large (fallback) | `openai/gpt-oss-120b` | $0.00015 | $0.0006 | reachable today |
| *alternative* | `claude-sonnet-5` | $0.002 | $0.010 | if "good enough" beats "most capable" |
| *mid, intended* | `gemini-2.0-flash` | $0.00030 | $0.00060 | ⚠️ unverified, commented out |

**The spread that matters:** small → large is **67× on input, 83× on output**.
That spread is the entire savings ceiling.

### The pricing bug worth telling

The large tier was `claude-sonnet-5` at `0.003 / 0.015` — which is **Sonnet
4.6** pricing. So it was *neither the model named nor any current rate*. It sat
flagged `UNVERIFIED` for two weeks while every savings figure depended on it.

> **A known-unverified number in a load-bearing position is a bug with a
> comment on it.** The comment doesn't make it less wrong.

---

## 9. The evaluation sets — never confuse these

| File | Size | Role | Quote it? |
|---|---|---|---|
| `eval/prompts.json` | **50** | **Tuned against.** A training score. | ❌ No |
| `eval/labelled.json` | **60** | The k-NN reference examples | ❌ Not a test set |
| `eval/holdout.json` | **32** | **Never used for tuning** | ✅ **This one** |
| `eval/cache_pairs.json` | **15 + 20** | equivalent + deliberately confusable | ✅ for cache |

> **Why this matters:** weights were once tuned on the same 50 prompts accuracy
> was reported on. That's a training score dressed as a result. The held-out
> set was written *after* tuning finished. `run_classifier_eval.py` prints both
> and **the gap between them is the size of the overfitting**, as a number.

---

## 10. Results — the honest columns

### Classifier accuracy, held-out (n=32)

| mode | accuracy | too cheap | too expensive |
|---|---|---|---|
| `naive_tokens` | 31% | **20** | 2 |
| `heuristic` | **88%** | 4 | 0 |
| `embedding` | 72% | **0** | 9 |
| **`hybrid` (shipped)** | 72% | **0** | 9 |

### Cache (35 labelled pairs)

| Metric | Value |
|---|---|
| Hit rate on rephrasings | **80%** |
| False hits on confusable pairs | **0%** |
| Lookup at 10,000 entries | **11.2 ms** |

### Live run, 2026-09-21 — 32 prompts, 0 failures, **$0.027 total**

| | ModelMux | baseline | |
|---|---|---|---|
| p50 latency | **2,803 ms** | 6,973 ms | **2.5× faster** |
| p95 latency | 9,037 ms | 9,019 ms | unchanged |
| cost / 1k, **measured** | $0.4121 | $0.4411 | **6.6%** |
| cost / 1k, *projected* | $11.49 | $18.25 | *37.1%* |

**Tier distribution:** small 9 · mid 7 · large 16
**D3 output-token bias:** **1.04×** (22,713 vs 21,844) — approximately unbiased

### Project totals

**131 tests** · **35 decisions** · **19 reference files** · **9 dependencies**

---

## 11. The numbers to memorise

If you remember nothing else from this file:

| | |
|---|---|
| Classification budget / actual | **20ms / 11-12ms** |
| Cache budget / actual | **30ms / 11.2ms** |
| Cache threshold | **0.88**, measured |
| Tier thresholds | **0.33 / 0.66** |
| Heuristic accuracy (held-out) | **88%** |
| Shipped (hybrid) accuracy | **72%, with zero too-cheap** |
| Cache hit rate | **80%**, 0% false |
| Latency win | **2.5×** at p50 |
| Cost saved, measured | **6.6%** (and *why* it's small) |
| Rate limit | **60/min**, per IP |
| Circuit breaker | **5 failures / 60s → open, 30s cooldown** |
| Held-out set size | **n=32** — one prompt is 3 points |
