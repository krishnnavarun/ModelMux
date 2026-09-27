# ModelMux

Cost-aware inference routing. ModelMux sits between your application and multiple LLM providers, classifies every incoming request, and dispatches it to the cheapest model tier that can actually handle it.

> **Status:** all six stages built. Cost and latency were measured live on
> 2026-09-21, twice. Only one provider key exists, so the large tier is served
> by a Groq fallback, which flattens the tier ladder to 2x. **The latency win
> (2.5x at the median) reproduces; the cost saving does not** -- it came out
> 6.6% then 1.7% on identical prompts, because output-length noise is larger
> than the routing effect on this configuration. **Answer quality is now
> graded**: on the 8 pairs where routing actually changed the model, the
> cheaper route was not worse in 8 of 8. See Results.

---

## The problem

An application that sends every request to its most capable model pays premium prices for trivial work. "What is 15% of 240?" and "Refactor this 400-line authentication module" cost the same, take similar round trips, and only one of them needs a frontier model.

Manual model selection doesn't solve this either. It pushes the decision onto the developer, who has to guess at request time and usually defaults to the biggest model to be safe.

ModelMux removes the decision. The caller sends a prompt. The router decides where it goes.

---

## How it works

```
Request
   |
   v
Cache lookup ---- hit ----> return stored response (~10ms, no model call)
   |
  miss
   |
   v
Classifier  -->  complexity score
   |
   +--> small tier   (cheap, fast)
   +--> mid tier     (balanced)
   +--> large tier   (slow, expensive)
   |
   v
Log, cache, return
```

Every request that resolves from cache costs nothing and returns in milliseconds. Every request routed down a tier saves the difference between what it cost and what it would have cost on the largest model.

---

## Features

- **Complexity classification** — heuristic and embedding-based scoring to estimate how capable a model a prompt needs
- **Semantic caching** — embeds prompts and serves stored responses for semantically equivalent requests, not just exact string matches
- **Tiered dispatch** — routes to small, mid, or large model tiers across multiple providers
- **Fallback and retry** — automatic failover when a provider times out or errors
- **Circuit breaking** — stops sending traffic to a provider that has been failing, and probes it periodically before restoring
- **Full request logging** — every routing decision, cost, and latency recorded for analysis
- **Live dashboard** — cost savings, cache hit rate, latency percentiles, and a real-time request feed

---

## Results

Measured **2026-09-21** on the 32-prompt held-out set, live against Groq,
**twice** — because the first run's cost figure turned out not to reproduce.
Both runs completed 32/32 pairs with zero failures.

Reproduce with `python eval/run_eval.py --set holdout.json`.

### Latency — the clearest win

| | ModelMux | Baseline (everything to the top tier) |
|---|---|---|
| p50 latency | **2,803 / 2,815 ms** | 6,973 / 6,891 ms |
| p95 latency | 9,037 / 8,958 ms | 9,019 / 10,256 ms |

*(two independent runs of the same 32 prompts)*

**2.5× faster at the median, and it reproduced.** 2.49× on the first run,
2.45× on the second — unlike the cost figure below, this result is stable
across runs, because it comes from *which model answered* rather than from how
many tokens it chose to emit.

p95 is unchanged, and that is expected — the tail is dominated by the hard
prompts, which route to the big model either way. **Routing improves the
typical request and leaves the worst case alone.**

### Cost — and why the measured number is noise

**Run the same 32 prompts twice and the cost saving comes out 6.6%, then
1.7%.** That is not a typo, and it is the most useful thing this evaluation
produced.

| run | ModelMux | Baseline | "Saved" | baseline/routed output tokens |
|---|---|---|---|---|
| 18:40 | $0.4121 / 1k | $0.4411 / 1k | 6.6% | 1.040 |
| 20:05 | $0.4181 / 1k | $0.4253 / 1k | 1.7% | 0.974 |

Identical prompts, identical configuration, identical routing (9 small / 7 mid
/ 16 large both times). **The only thing that changed is how long the models
chose to answer**, and that moved the headline by a factor of four.

#### The real routing effect: 3-4%

Comparing routed cost against the *same tokens* repriced at baseline rates —
which removes output-length noise entirely — gives **3.0% and 4.2%** across the
two runs. That is the actual, reproducible effect of routing on this
configuration.

**Why so small:**

- 9 of 32 prompts route to the cheap tier
- but those 9 produce only **5-8% of all output tokens** — short questions get
  short answers
- the small tier is exactly **2×** cheaper than the tier above it
- so the ceiling is about half of 5-8%, which is **3-4%** ✓

The A/B measurement swings ±5 points around a 3-4% signal. **The noise is
larger than the effect**, so any single A/B run of this set is not evidence of
anything.

#### Why the ladder is flat

Only a Groq key exists, so the large tier falls back to `gpt-oss-120b` — the
same model the mid tier uses. The ladder compresses to a 2× spread, and 16 of
32 prompts route to a tier where routed and baseline are **byte-for-byte the
same call**. Half the traffic has nothing to save and the rest has 2× to save
from.

| | ModelMux | Baseline | Saved |
|---|---|---|---|
| **Projected** (configured ladder) | $11.49 / 1k req | $18.25 / 1k req | **37.1%** |

The projection reprices measured token counts at the configured tiers
(`gpt-oss-20b` → `gpt-oss-120b` → `claude-opus-5`, a 33× spread). It is
arithmetic on real measurements, not a simulation — but no Anthropic call was
ever made, so it is a projection and labelled as one.

> **Two lessons, and the second only appeared by running it twice:**
>
> 1. **A router's savings are bounded by the price spread it is given.**
>    Perfect classification earns nothing on a flat ladder.
> 2. **When the effect is smaller than the variance, a single measurement is
>    not a result.** The first run's 6.6% was quoted here as a finding. It was
>    a draw from a noisy distribution, and re-running it is what revealed that.

**Cost figures on this configuration should be quoted as the same-token
routing effect (3-4%), not as an A/B difference.** With the configured ladder
the spread is 33× instead of 2× and the signal would clear the noise easily —
but that run has not been made.

### Where the prompts went

`small: 9 · mid: 7 · large: 16` — the classifier sends half the held-out set to
the top tier, which is what a set built around hard reasoning prompts should
produce.

### Classifier accuracy, held-out (n=32)

| mode | accuracy | routed too cheap | routed too expensive |
|---|---|---|---|
| token count only (naive baseline) | 31% | 20 | 2 |
| heuristic | **88%** | 4 | 0 |
| embedding (k-NN) | 72% | 0 | 9 |
| **hybrid — shipped** | 72% | **0** | 9 |

**Hybrid is less accurate than the heuristic and ships anyway.** It eliminates
every too-cheap misroute, and a wrong cheap answer costs more in trust than a
wrong expensive one costs in money. A single accuracy figure would have ranked
the heuristic first and hidden that it sends hard prompts to weak models.

### Cache

| | |
|---|---|
| Hit rate on rephrasings | **80%** |
| False hits on confusable pairs | **0%** |
| Lookup at 10,000 entries | **11.2 ms** (budget 30 ms) |
| Classification | **11–12 ms** p50 (budget 20 ms) — on an *idle* process; see below |

### The counterfactual bias, measured

`cost_if_large_usd` assumes the baseline would emit the same number of output
tokens as the routed tier. `/v1/compare` and `run_eval.py` now check that
instead of assuming it.

**Measured ratio: 1.040, then 0.974** across two runs of the same prompts —
the baseline was 4% more verbose, then 3% less. It straddles 1.0, so the
assumption is approximately unbiased *on average*.

But the spread is the real finding: **that ±4% swing in output length is
exactly the noise that moved the headline cost saving from 6.6% to 1.7%.**
Repricing observed tokens (which is what `cost_if_large_usd` does) is *more*
stable than an A/B comparison, because it holds tokens constant instead of
letting both arms vary. See `DECISIONS.md` D3 and D36.

### Answer quality — graded, and the honest n is 8

The project's oldest gap. SPEC is explicit that *"cost savings mean nothing if
the cheaper answers are worse"*, and that column was empty until 2026-09-27.

The 30-item blind spot-check was graded by **Claude Opus 5 as an LLM-as-judge**,
reading only the blind file — the key was opened after every verdict was
written.

```
routed better        5     17%
baseline better      0      0%
indistinguishable   25     83%
ROUTED NOT WORSE:   30/30 = 100%
```

**Do not quote the 100%.** With one API key the large tier falls back to the
mid tier's model, so most pairs compared **a model with itself**:

| group | n | routed | baseline | tie |
|---|---|---|---|---|
| `large` → same model | 15 | 4 | 0 | 11 |
| `mid` → same model | 7 | 1 | 0 | 6 |
| **`small` → different model** | **8** | **0** | **0** | **8** |

> **The result: on the 8 pairs where routing actually changed which model
> answered, routed was not worse in 8/8 — with zero cases where the cheap
> model lost.**

Real, and small. n=8, so one item is 12%. And those 8 are by construction the
*easy* prompts — they routed small because the classifier judged them easy. So
this measures "the cheap model handles what we send it", which is the right
question for a router and a narrower one than 100% suggests.

`grade_quality.py` now performs this split itself and labels the meaningful
subset *"quote THIS one"*, because a harness that can print a misleading
headline eventually will.

**Bias checks, since all five preferences landed on the routed arm:** blinding
held (routed was in slot A for 14 of 30 items); no positional bias (picks split
2 A / 3 B); no verbosity bias (the longer answer won only 2 of 5, and twice the
shorter one won). At n=5 the sweep is most likely chance. Usefully, **17 of the
22 same-model pairs were judged identical** — a consistency check on the method.

**What this is not:** not independent (the grader is the agent that wrote the
router), not the human spot-check D23 also asked for, and not a quality
*score* — it is a pairwise preference.

### Limitations worth stating plainly

- **One API key.** No adapter has spoken to Google or Anthropic; the large tier
  is served by a Groq fallback. The Anthropic *prices* are verified, the
  *adapter* is not.
- **The A/B cost measurement is noise-dominated on this configuration.** The
  routing effect is 3-4%; run-to-run variance is ±5 points. Quote the
  same-token figure, not the A/B difference — and note that two runs is not a
  variance estimate either, it is two points.
- **Groq's on-demand tier rate-limited the second run heavily** (8,000 tokens
  per minute on `gpt-oss-120b`). Retry and backoff absorbed every one and the
  run still completed 32/32, but the latency figures include that waiting.
- **Every accuracy number is measured against hand-labels written by one
  person** — the same person who built the classifier. Treat as an upper bound.
- **The quality grade has n=8 and a non-independent grader.** Only 8 of the 30
  graded pairs compared different models; the grader is the agent that wrote
  the router, and the human half of D23's recommendation is still unfilled.
- **n=32 held out.** One prompt is three percentage points.
- **35 cache pairs cannot map the space of confusable prompts.** Zero false hits
  *there* is not zero false hits in production.
- **Semantic caching can serve one user's answer to another.** Inherent to
  shared caching, not a bug awaiting a fix — mitigated by a measured threshold,
  a lexical inversion guard, and `bypass_cache`, and demonstrated by a test that
  deliberately produces a wrong answer at a loose threshold.
- **Circuit breaker and rate limiter state are per process.** With several
  workers each keeps its own view.
- **The 11–12 ms classification figure was measured on an idle process, and
  the test that guards it is flaky in the full suite** — intermittently
  reporting a median of 85 ms, five to eleven times the budget. The cause is
  unknown: it is not sampling noise, not the HTTP path, and not test ordering.
  It is recorded as open in `DECISIONS.md` D35 rather than tuned away, because
  a busy process is exactly what production looks like.

The whole evaluation run cost **$0.027**.

---

## Quick start

**Requirements:** Python 3.11+, Redis, and API keys for at least two providers.

```bash
git clone https://github.com/<your-username>/modelmux.git
cd modelmux

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # add your API keys

uvicorn app.main:app --reload
```

The API is now at `http://localhost:8000`.

### Making a request

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is the capital of Japan?"}'
```

```json
{
  "response": "Tokyo.",
  "routing": {
    "tier": "small",
    "provider": "groq",
    "cache_hit": false,
    "complexity_score": 0.12,
    "latency_ms": 287,
    "cost_usd": 0.00004,
    "cost_if_large_usd": 0.00180
  }
}
```

Every response includes the routing decision. Nothing about the choice is hidden from the caller.

### Dashboard

No build step. Start the API and open:

```
http://localhost:8000/dashboard
```

One static file, served from the API's own origin so it needs no CORS setup.
The spec called for React + Vite + Recharts; that was cut deliberately —
see `DECISIONS.md` D24.

---

## Configuration

Tiers are defined in `config.yaml`:

```yaml
tiers:
  small:
    providers:
      - name: groq
        model: openai/gpt-oss-20b
        cost_per_1k_input: 0.000075
        cost_per_1k_output: 0.0003
  large:
    # A tier holds a LIST. The first reachable provider answers; the rest are
    # within-tier fallback (DECISIONS.md D4). Anthropic is preferred here and
    # fails fast without a key, so Groq serves the tier today.
    providers:
      - name: anthropic
        model: claude-opus-5
        cost_per_1k_input: 0.005
        cost_per_1k_output: 0.025
      - name: groq
        model: openai/gpt-oss-120b
        cost_per_1k_input: 0.00015
        cost_per_1k_output: 0.0006

cache:
  similarity_threshold: 0.88   # measured, not guessed -- DECISIONS.md D16
  ttl_seconds: 86400

classifier:
  mode: hybrid
  escalate_on_low_confidence: true
  confidence_threshold: 0.60
```

Cost is attributed to the provider that **actually answered**, not to the
tier's first entry — otherwise a fallback would be billed at the price of the
model it replaced. See `config.cost_for_provider()`.

The cache similarity threshold is the most consequential setting in the project. Too loose and semantically different prompts share an answer; too tight and the hit rate collapses. See `DECISIONS.md` for how the current value was chosen.

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat` | Route a prompt and answer it |
| `POST /v1/compare` | Run the same prompt through the router **and** the baseline tier |
| `GET /health` | Liveness. Never calls a provider. |
| `GET /health/providers` | Per-provider circuit state and last error |
| `GET /v1/stats?window=24h` | Cost saved, hit rate, p50/p95, tier distribution |
| `GET /v1/requests?limit=50` | Recent request log |
| `GET /dashboard` | Single-page dashboard |

`GET /v1/stream` (SSE) was cut deliberately — see `DECISIONS.md` D24.

### `/v1/compare` — auditing the savings figure

The headline savings number rests on one assumption: that the baseline tier
would have produced the same number of output tokens. That is an assumption
because measuring it means paying for the expensive call.

`/v1/compare` runs both and reports the actual ratio:

```json
"comparison": {
  "cost_saved_usd": 0.000365,
  "savings_pct": 95.67,
  "output_token_ratio": 1.0,
  "d3_bias_note": "... >1 means live savings are UNDERstated, <1 OVERstated."
}
```

It is deliberately not cached and not counted in `/v1/stats` — it costs two
provider calls, and letting it into the statistics would corrupt the very
numbers it exists to audit.

### Rate limiting

Per-caller token bucket, `limits.rate_limit_per_minute` in `config.yaml`.
Exceeding it returns **429** with `retry_after_seconds` and a `Retry-After`
header.

Checked *before* classification and *before* the cache, so a rejected request
costs nothing. Keyed by IP, because there is no authentication yet;
`X-Forwarded-For` is deliberately **not** trusted, since a caller-controlled
header would let anyone pick their own bucket.

State is per process — with several workers the effective limit is
`workers x rate`.

---

## Architecture

```
app/
  main.py            FastAPI application and endpoints
  router.py          Tier selection logic
  classifier/
    heuristic.py     Rule-based complexity scoring
    embedding.py     Embedding-based classification
  cache.py           Semantic cache over Redis
  providers/         One adapter per provider
  resilience.py      Retry, fallback, circuit breaker
  db.py              Request logging
eval/
  prompts.json       Evaluation set
  run_eval.py        Cost and quality benchmarking
dashboard/
  index.html         The whole dashboard: one file, no build step (D24)
```

### Request logging

Every request writes one row:

| Column | Description |
|---|---|
| `id` | Request identifier |
| `timestamp` | When it arrived |
| `prompt_hash` | Hash of the prompt |
| `complexity_score` | Classifier output |
| `tier` | Tier selected |
| `provider` | Provider actually called |
| `cache_hit` | Whether it was served from cache |
| `tokens_in` / `tokens_out` | Token counts |
| `cost_usd` | Actual cost |
| `cost_if_large_usd` | Counterfactual cost on the largest tier |
| `latency_ms` | End-to-end time |
| `fallback_fired` | Whether a fallback was triggered |
| `status` | Success or failure |

This table is the source of every dashboard metric, and the training data for the learned classifier.

---

## Evaluation

```bash
python eval/run_eval.py
```

Runs the evaluation set through both ModelMux and a baseline that sends everything to the large tier, then reports cost, latency, and quality for each.

### Quality grading

`run_eval.py` exports a **blind** spot-check file — which answer came from
which tier is withheld from the grader and written to a separate key file.
`grade_quality.py` reads it back, unblinds in exactly one place, and reports
routed / baseline / tie. Neither needs an API key.

```bash
python eval/grade_quality.py eval/results/spotcheck-<stamp>.json
```

It headlines **"not worse"** rather than "wins", because routing is justified
when the cheap answer is *as good*, not only when it beats the expensive one.
It refuses to let a sample below n=20 look like a measurement, prints its own
caveats every run, and — since D37 — **splits out the pairs where routed and
baseline resolved to the same model**, which cannot test routing at all.

D23 recommended a human spot-check **and** LLM-as-judge as separate columns,
never averaged. The LLM-as-judge column is filled (see Results); **the human
one is not**, and that is the single remaining gap in the evaluation.

---

## Roadmap

- [ ] Learned router trained on logged routing outcomes
- [ ] Confidence-based escalation from small to large tier
- [ ] Streaming response support
- [ ] Per-user budget caps
- [ ] Prometheus metrics endpoint

---

## Prior art

ModelMux is a from-scratch implementation of a pattern that already exists in production tooling. Reading these shaped the design:

- [RouteLLM](https://github.com/lm-sys/RouteLLM) — LMSYS research framework for training and evaluating routers
- [LiteLLM](https://github.com/BerriAI/litellm) — widely used LLM proxy, now with auto-routing
- [semantic-router](https://github.com/aurelio-labs/semantic-router) — semantic decision layer for LLMs and agents

The classifier, cache, and resilience logic here are built independently rather than imported, so that every routing decision is one I can explain and defend.

---
