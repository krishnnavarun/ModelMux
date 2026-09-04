# ModelMux

Cost-aware inference routing. ModelMux sits between your application and multiple LLM providers, classifies every incoming request, and dispatches it to the cheapest model tier that can actually handle it.

> **Status:** in development. Metrics marked `TBD` will be filled in once benchmarking is complete.

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

Measured against a held-out evaluation set of TBD prompts.

| Metric | Baseline (all requests to large tier) | ModelMux |
|---|---|---|
| Cost per 1,000 requests | TBD | TBD |
| p50 latency | TBD | TBD |
| p95 latency | TBD | TBD |
| Cache hit rate | 0% | TBD |
| Answer quality (eval score) | TBD | TBD |

Classifier accuracy on the labeled test set: **TBD**

Quality is reported alongside cost deliberately. Cost savings mean nothing if the cheaper answers are worse, so both numbers are measured on the same evaluation set.

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

```bash
cd dashboard
npm install
npm run dev
```

Opens at `http://localhost:5173`.

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

Quality scoring is TBD — see `DECISIONS.md` for the approach and its limitations.

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
