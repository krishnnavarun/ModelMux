# ModelMux

Cost-aware inference routing. ModelMux sits between your application and multiple LLM providers, classifies every incoming request, and dispatches it to the cheapest model tier that can actually handle it.

> **Status:** in development. Stages 1-5 complete; Stage 6 partial.
> Cost and latency remain unmeasured because no API key has ever worked --
> see Results. Routing and cache figures below are real and reproducible
> without a key.

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

### What is measured

These numbers come from `eval/run_classifier_eval.py` and
`eval/tune_cache_threshold.py`, and need no API key — routing and cache
matching are pure functions of the prompt.

**Classifier accuracy**, held-out set (n=32), never used for tuning:

| mode | accuracy | routed too cheap | routed too expensive |
|---|---|---|---|
| token count only (the naive baseline) | 31% | 20 | 2 |
| heuristic | **88%** | 4 | 0 |
| embedding (k-NN) | 72% | 0 | 9 |
| **hybrid — shipped** | 72% | **0** | 9 |

**Hybrid is less accurate than the heuristic and ships anyway.** It eliminates
every too-cheap misroute, and a wrong cheap answer costs more in trust than a
wrong expensive one costs in money. A single accuracy figure would have ranked
the heuristic first and hidden that it sends hard prompts to weak models.

**Cache**, measured on 35 labelled prompt pairs:

| | |
|---|---|
| Hit rate on rephrasings | **80%** |
| False hits on confusable pairs | **0%** |
| Lookup at 10,000 entries | **11.2 ms** (budget 30 ms) |
| Classification | **11–12 ms** p50 (budget 20 ms) |

### What is NOT measured, and why

| Metric | Status |
|---|---|
| Cost per 1,000 requests | **Not measured** |
| p50 / p95 latency, end to end | **Not measured** |
| Answer quality | **Not measured** |

**No API key has ever worked in this project.** Every provider adapter is
verified against synthetic responses; none has spoken to a live server. Cost and
latency figures require real calls, and a plausible-looking number produced from
mock data would be worse than no number at all — so there isn't one.

`eval/run_eval.py` **refuses to run** against mock providers unless
`--simulated` is passed, and prefixes every line of such a run with
`SIMULATED`. The moment a working key exists, one command fills this table:

```bash
python eval/run_eval.py --set holdout.json
```

**Quality scoring is an undecided design question**, not a missing
implementation — see `DECISIONS.md` D23. `run_eval.py` exports a blind
spot-check file (which answer came from which tier is withheld from the grader
and written to a separate key file) so quality can be graded by hand with no API
access at all.

### The savings figure carries a caveat

`cost_if_large_usd` reprices **the token counts actually observed** at baseline
rates. That is *"same tokens, baseline prices"* — not what the large tier would
truly have cost, since a larger model usually answers at a different length.

The caveat travels in the `/v1/stats` payload itself (`savings_caveat`) so no
dashboard can drop it by accident, and `run_eval.py` **measures** the bias
directly by comparing output-token counts from both tiers. See `DECISIONS.md`
D3.

### Limitations worth stating plainly

- **Every accuracy number is measured against hand-labels written by one
  person** — the same person who built the classifier. Treat them as an upper
  bound.
- **n=32 held out.** One prompt is three percentage points. These figures have
  wide error bars.
- **35 cache pairs cannot map the space of confusable prompts.** Zero false hits
  *there* is not zero false hits in production.
- **Semantic caching can serve one user's answer to another.** That is inherent
  to shared caching, not a bug awaiting a fix. Mitigated by a measured
  threshold, a lexical inversion guard, and a `bypass_cache` flag — and
  demonstrated by a test that deliberately produces a wrong answer at a loose
  threshold.
- **Circuit breaker state is per process.** With several workers each keeps its
  own view; the cost is uneven traffic to a failing provider, never incorrect
  answers.

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
    provider: groq
    model: <model-name>
    cost_per_1k_input: 0.00005
  mid:
    provider: google
    model: <model-name>
    cost_per_1k_input: 0.00030
  large:
    provider: anthropic
    model: <model-name>
    cost_per_1k_input: 0.00300

cache:
  similarity_threshold: 0.92
  ttl_seconds: 86400

routing:
  escalate_on_low_confidence: true
  confidence_threshold: 0.6
```

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
dashboard/           React frontend
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

**Quality scoring is an open decision, not a missing implementation** — see
`DECISIONS.md` D23 for the three candidate approaches, the recommendation, and
why publishing a savings figure without a quality column would be half a
result.

`run_eval.py` exports a **blind** spot-check file — which answer came from
which tier is withheld from the grader and written to a separate key file — so
quality can be graded by hand today, with no API key.

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
