# 03 — Architecture: every file, and what it is responsible for

## 1. The map

```
ModelMux/
├── app/                      THE APPLICATION
│   ├── main.py               FastAPI app, all 7 endpoints, request orchestration
│   ├── config.py             config.yaml loading, .env loading, cost maths
│   ├── schemas.py            Pydantic request/response contracts
│   ├── router.py             tier selection — the decision itself
│   ├── tokens.py             tiktoken wrapper, loaded once and warmed
│   ├── cache.py              Redis semantic cache + inversion guard
│   ├── resilience.py         retry, fallback, circuit breakers
│   ├── db.py                 SQLite schema, WAL, never-fails-a-request logging
│   ├── metrics.py            aggregates, exact percentiles
│   ├── ratelimit.py          per-caller token bucket
│   ├── classifier/
│   │   ├── signals.py        feature extraction from raw text
│   │   ├── heuristic.py      weighted scoring + floors + discount
│   │   └── embedding.py      MiniLM k-NN against labelled examples
│   └── providers/
│       ├── base.py           ABC + the 4-error taxonomy + ProviderResult
│       ├── groq.py           Groq adapter (OpenAI-compatible)
│       ├── google.py         Gemini adapter
│       ├── anthropic.py      Claude adapter (content blocks)
│       └── mock.py           deterministic fake — the testing backbone
│
├── eval/                     MEASUREMENT (never ships, always runs)
│   ├── prompts.json          50 prompts — TUNED AGAINST (training score)
│   ├── labelled.json         60 examples — the k-NN reference data
│   ├── holdout.json          32 prompts — NEVER tuned on (the honest number)
│   ├── cache_pairs.json      15 equivalent + 20 deliberately confusable
│   ├── run_routing_check.py  Stage 2 misroute report
│   ├── run_classifier_eval.py  4 modes × 2 sets + overfitting gap
│   ├── tune_cache_threshold.py threshold sweep with the guard
│   ├── run_eval.py           routed vs baseline; refuses mock data
│   ├── grade_quality.py      reads back the blind spot-check
│   └── explain.py            explain ONE decision, costs nothing
│
├── tests/                    131 tests, no network, no API key
├── dashboard/index.html      the whole dashboard, one file
├── config.yaml               tiers, prices, thresholds, limits
├── specs.md                  the authoritative spec
├── DECISIONS.md              35 decisions with reasoning AND costs
├── learnings/                reference library, keyed by technology
└── learning/                 this study guide
```

## 2. Separation of concerns — why each boundary exists

| Boundary | What it buys |
|---|---|
| `router.py` never calls a provider | Routing is a **pure function of the prompt**. That's why `explain.py` and every routing test run with no network and no key. |
| `resilience.py` never knows about tiers' *meaning* | It walks a list and handles failure. Swapping providers needs no change here. |
| `providers/*` share one ABC | Three wildly different HTTP APIs become one interface. Adding a provider is one file. |
| `config.py` owns *where settings come from* | Both `config.yaml` **and** `.env`. This is why moving `load_env()` here fixed the eval scripts (D29/D31). |
| `db.py` swallows its own errors | A logging failure must never fail a request. |
| `eval/` is outside `app/` | Measurement code never ships, and never accidentally becomes a dependency. |

> **Interview question you will be asked:** *"Why is routing a pure function?"*
> Because it makes the expensive part testable for free. 131 tests run with no
> network, no key, and no cost — and `explain.py` can show the full decision for
> any prompt without spending anything.

## 3. The provider abstraction

Every adapter implements one method:

```python
class Provider(ABC):
    async def complete(self, prompt: str, model: str, max_tokens: int) -> ProviderResult
```

Returning:

```python
@dataclass
class ProviderResult:
    text: str
    tokens_in: int      # from the provider's OWN usage block, never estimated
    tokens_out: int
    model: str
```

### The three API shapes it hides

| Provider | Request shape | Response shape | Gotcha |
|---|---|---|---|
| **Groq** | OpenAI-compatible | `choices[0].message.content` | **Reasoning models** return a separate `reasoning` field; content can be empty |
| **Google** | `contents[].parts[]` | `candidates[0].content.parts[0].text` | Returns **200 with no parts** on a safety block |
| **Anthropic** | `messages[]`, `x-api-key` header | `content[]` **blocks**, not a string | Must concatenate text blocks; 529 "overloaded" is non-standard |

> **The recurring lesson: a 200 whose body isn't the shape you expect is still
> a failure.** All three providers have a way of returning HTTP 200 and nothing
> useful. Checking status alone is not enough.

### Billing comes from the provider, never from your own count

`tiktoken` is used for *pre-call estimates only*. The authoritative token
counts come back in the response's `usage` block. For reasoning models these
differ by the entire internal chain of thought — which is billed but never
appears in the text you got back.

## 4. The database row — the source of every number

One table, one row per request, **including every failure path**.

| Column | Holds | Why it's there |
|---|---|---|
| `id` | request id | correlate logs |
| `timestamp` | ISO time | windowed stats |
| `prompt_hash` | hash | dedupe without storing the prompt |
| `prompt_preview` | truncated text | debugging |
| `complexity_score` | 0.0-1.0 | audit the classifier |
| **`signals_json`** | the full signals object | **explainability — never dropped** |
| `tier` / `provider` / `model` | what was chosen | attribution |
| `cache_hit` | bool | hit rate |
| `escalated` | bool | how often confidence was low |
| `fallback_fired` | bool | provider health over time |
| `attempts` | int | retry pressure |
| `tokens_in` / `tokens_out` | provider's counts | cost, and the D3 bias check |
| `cost_usd` | actual | the numerator |
| `cost_if_large_usd` | counterfactual | the savings claim |
| `latency_ms` | end to end | p50/p95 |
| `classify_ms` | classification only | the 20ms budget |
| `cache_lookup_ms` | lookup only | the 30ms budget |
| `status` | success / error | filter percentiles |
| `error_message` | why it failed | **must name the cause, not the symptom** |

> **`signals_json` and `cost_if_large_usd` are the two columns that make this
> more than a proxy.** One makes every decision auditable after the fact; the
> other is the entire savings claim. Both are in the row rather than computed
> later, because prices change and a recomputed historical cost would be wrong.

## 5. The API surface

| Endpoint | Purpose | Notes |
|---|---|---|
| `POST /v1/chat` | Route a prompt and answer it | The product |
| `POST /v1/compare` | Run **both** router and baseline | Audits the savings claim |
| `GET /health` | Liveness | **Never calls a provider** |
| `GET /health/providers` | Circuit state + last error per provider | Empty = healthiest state |
| `GET /v1/stats?window=24h` | Cost saved, hit rate, p50/p95, tier mix | Carries `savings_caveat` |
| `GET /v1/requests?limit=50` | Recent request log | Dashboard feed |
| `GET /dashboard` | Single-page dashboard | One static file |

### `/v1/compare` — the endpoint that audits its own project

The headline savings number rests on one assumption: **that the baseline would
have produced the same number of output tokens.** It's an assumption because
measuring it means paying for the expensive call.

`/v1/compare` runs both and reports the real ratio:

```json
"comparison": {
  "cost_saved_usd": 0.000365,
  "savings_pct": 95.67,
  "output_token_ratio": 1.04,
  "d3_bias_note": "... >1 means live savings are UNDERstated, <1 OVERstated."
}
```

**It is deliberately not cached and not counted in `/v1/stats`** — it costs two
provider calls, and letting it into the statistics would corrupt the very
numbers it exists to audit.

> **Measured at 1.04×** across the held-out set: the baseline was 4% more
> verbose, so the savings figure understates slightly. Approximately unbiased —
> and now a number rather than a caveat.

### `/health` never calls a provider

A liveness check that depends on a third party reports *their* outage as
*your* death — and load balancers kill pods over it. Provider health is a
separate endpoint.

## 6. Rate limiting

Per-caller **token bucket**, 60/minute, keyed by IP.

Three deliberate choices:

1. **Checked before classification and before the cache**, so a rejected
   request costs nothing.
2. **Keyed by IP, and `X-Forwarded-For` is deliberately NOT trusted** — there's
   no authentication yet, and a caller-controlled header would let anyone pick
   their own bucket.
3. Returns **429 with `retry_after_seconds` and a `Retry-After` header**.

**Known limitation, stated up front:** state is per process. With several
workers the effective limit is `workers × rate`. The fix is Redis-backed
counters; the cost of not doing it is understood and written down.

## 7. Configuration over code

Everything that varies by environment or gets tuned by experiment lives in
`config.yaml`: tiers, model names, prices, thresholds, timeouts, limits.

`${VAR}` expansion is implemented in `config.py` so paths like
`${LOCALAPPDATA}/ModelMux/modelmux.db` work, and `MODELMUX_DB_PATH` overrides
it entirely for tests.

**Validation happens at startup, not on first use.** Lazy validation turns a
config typo into a 3am production error instead of a failed deploy.

> **A useful test for what belongs in config:** *if you'd change it to run an
> experiment, it's config.* Thresholds, prices, model names — yes. Business
> logic — no.
