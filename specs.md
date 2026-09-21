# ModelMux — Build Specification

This is the authoritative technical spec. Claude Code should read this before
writing any code. If something here conflicts with a conversation, this file wins
unless I say otherwise.

---

## 1. What we are building

A service that sits between an application and several LLM providers. For every
incoming request it:

1. Checks whether a semantically similar prompt has been answered before
2. If not, estimates how difficult the prompt is
3. Sends it to the cheapest model tier that can handle it
4. Retries or fails over if the provider misbehaves
5. Logs the decision, the cost, and the latency
6. Returns the answer plus a full explanation of the routing decision

We do not train or host models. All inference is remote.

---

## 2. Non-negotiable constraints

These shape every design decision. Do not violate them.

- **The routing decision must be cheaper than the request it routes.** No LLM call
  in the classification path. Classification is heuristics + a local CPU embedding
  model only.
- **The routing decision must be fast.** Target under 20ms for classification,
  under 30ms for cache lookup.
- **Every routing decision must be explainable.** No black-box scoring. Every
  component of a complexity score must be individually inspectable.
- **Nothing is silently dropped.** If a request fails, the caller gets a clear
  error, and a row is still written to the database.
- **Configuration over code.** Tiers, thresholds and provider settings live in
  `config.yaml`, not in Python.

---

## 3. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | **Running 3.13, and that is fine** -- verified, see section 13. |
| Web framework | FastAPI + uvicorn | async throughout |
| HTTP client | httpx.AsyncClient | one shared client, connection pooling on |
| Database | SQLite (dev) → PostgreSQL (later) | stdlib `sqlite3`, no ORM |
| Cache | Redis | `redis.asyncio`. No native Windows build -- **use WSL2**, see section 13. |
| Embeddings | sentence-transformers, `all-MiniLM-L6-v2` | 384 dims, CPU, ~10ms |
| Token counting | tiktoken | OpenAI's tokenizer; approximate for other providers. Only needed for *pre-call* estimates -- providers return real counts in `usage`. |
| Config | PyYAML | |
| Frontend | React + Vite + Recharts | built last |
| Load testing | k6 | against mock providers only |
| Testing | pytest | `tests/`, with `providers/mock.py`. No network, no API key. |

**Do not add dependencies beyond this list without asking.**

---

## 4. File structure

```
modelmux/
  app/
    __init__.py
    main.py              FastAPI app, endpoints, startup/shutdown
    config.py            loads and validates config.yaml
    schemas.py           pydantic request/response models
    router.py            tier selection — the core logic
    classifier/
      __init__.py
      heuristic.py       rule-based scoring
      embedding.py       embedding-based scoring
      signals.py         individual feature extractors
    cache.py             semantic cache over Redis
    providers/
      __init__.py
      base.py            Provider abstract base class
      groq.py
      google.py
      anthropic.py
      mock.py            fake provider for load testing
    resilience.py        retry, fallback, circuit breaker
    db.py                schema + logging
    metrics.py           aggregate queries for the dashboard
  eval/
    prompts.json         evaluation set
    labelled.json        classifier training/test set
    run_eval.py
  tests/
  dashboard/
  config.yaml
  .env
  CLAUDE.md
  DECISIONS.md
  SPEC.md                this file
  README.md
```

---

## 5. API contract

### `POST /v1/chat`

Request:
```json
{
  "prompt": "string, required, 1-100000 chars",
  "max_tokens": 1000,
  "force_tier": null,
  "bypass_cache": false
}
```

`force_tier` and `bypass_cache` exist for testing and for the dashboard's
comparison view. They are not part of normal operation.

Response (200):
```json
{
  "response": "the model's answer",
  "routing": {
    "request_id": "uuid",
    "tier": "small",
    "provider": "groq",
    "model": "the-model-name",
    "cache_hit": false,
    "complexity_score": 0.12,
    "signals": {
      "token_count": 8,
      "has_code": false,
      "reasoning_markers": 0,
      "multi_question": false,
      "embedding_tier": "small",
      "embedding_confidence": 0.87
    },
    "escalated": false,
    "fallback_fired": false,
    "attempts": 1,
    "tokens_in": 8,
    "tokens_out": 3,
    "cost_usd": 0.00004,
    "cost_if_large_usd": 0.00180,
    "latency_ms": 287,
    "classify_ms": 11,
    "cache_lookup_ms": 6
  }
}
```

The `signals` object is what makes the system explainable. Never remove it.

### Other endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness — returns 200 always |
| `GET /health/providers` | per-provider circuit state and last error |
| `GET /v1/stats?window=24h` | aggregates for the dashboard overview |
| `GET /v1/requests?limit=50` | recent request log for the live feed |
| `GET /v1/stream` | Server-Sent Events, pushes each new request |
| `POST /v1/compare` | runs the same prompt through router and large tier, returns both |

### Error responses

| Status | When | Body |
|---|---|---|
| 400 | prompt empty or over length limit | `{"error": "...", "request_id": "..."}` |
| 422 | body is not valid JSON, or a field has the wrong type | FastAPI's own shape. Unavoidable -- fires before our code. |
| 429 | caller exceeded local rate limit | includes `retry_after_seconds` |
| 502 | all providers in tier failed | includes `attempts` and last error |
| 503 | all providers circuit-open | includes estimated recovery time |

Every error path still writes a database row with `status` set accordingly.

**400 vs 422.** Length and emptiness are checked *in the handler*, not by a
pydantic constraint. A pydantic failure returns 422 from a layer that runs
before our code -- so it could neither return 400 nor write a database row.
See DECISIONS.md D1.

**Provider failures are never 400.** A dead API key or a wrong model name raises
`ProviderBadRequest`, but that is our misconfiguration, not the caller's bad
prompt. All provider errors return 502. The four-way error taxonomy governs
*retry policy*, not status codes. See DECISIONS.md D2.

---

## 6. Database schema

Single table. SQLite for development.

```sql
CREATE TABLE IF NOT EXISTS requests (
  id                 TEXT PRIMARY KEY,          -- uuid4
  timestamp          TEXT NOT NULL,             -- ISO8601 UTC
  prompt_hash        TEXT NOT NULL,             -- sha256 of prompt
  prompt_preview     TEXT,                      -- first 80 chars, for the dashboard
  complexity_score   REAL,
  signals_json       TEXT,                      -- the signals object, serialised
  tier               TEXT,                      -- small | mid | large
  provider           TEXT,
  model              TEXT,
  cache_hit          INTEGER NOT NULL DEFAULT 0,
  escalated          INTEGER NOT NULL DEFAULT 0,
  fallback_fired     INTEGER NOT NULL DEFAULT 0,
  attempts           INTEGER NOT NULL DEFAULT 1,
  tokens_in          INTEGER,
  tokens_out         INTEGER,
  cost_usd           REAL,
  cost_if_large_usd  REAL,
  latency_ms         INTEGER,
  classify_ms        INTEGER,
  cache_lookup_ms    INTEGER,
  status             TEXT NOT NULL,             -- success | error | cached
  error_message      TEXT
);

CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp);
CREATE INDEX IF NOT EXISTS idx_requests_tier ON requests(tier);
```

**`prompt_preview` is a privacy decision, not a convenience.** We store 80
characters for the dashboard feed and hash the rest. See section 10.

`cost_if_large_usd` is computed at request time from the baseline tier's
configured price, so savings can be summed directly without reconstructing
pricing later.

**Cost must count input and output tokens separately.** Output is billed at a
higher rate -- 5x on the large tier in our own config -- and the ratio differs
per tier. Pricing input alone would corrupt the exact comparison this project
exists to make. See DECISIONS.md D6.

**`cost_if_large_usd` carries a known bias.** It reprices the token counts we
actually observed at baseline-tier rates. A larger model answering the same
prompt usually produces a different output length, so this is "same tokens,
baseline prices", not a true counterfactual. It is the only affordable option --
the alternative is calling the large model every time. Every savings figure must
carry this caveat, and Stage 6 should measure the bias via `/v1/compare` on a
sample. See DECISIONS.md D3.

---

## 7. Configuration

`config.yaml`:

```yaml
tiers:
  small:
    providers:
      - name: groq
        model: <current-groq-model>
        cost_per_1k_input: 0.00005
        cost_per_1k_output: 0.00008
  mid:
    providers:
      - name: google
        model: <current-gemini-model>
        cost_per_1k_input: 0.00030
        cost_per_1k_output: 0.00060
  large:
    providers:
      - name: anthropic
        model: <current-model>
        cost_per_1k_input: 0.00300
        cost_per_1k_output: 0.01500

classifier:
  mode: hybrid              # heuristic | embedding | hybrid
  thresholds:
    small_max: 0.33
    mid_max: 0.66
  escalate_on_low_confidence: true
  confidence_threshold: 0.60

cache:
  enabled: true
  similarity_threshold: 0.92
  ttl_seconds: 86400
  max_entries: 10000

resilience:
  request_timeout_seconds: 30
  connect_timeout_seconds: 5
  max_retries: 2
  backoff_base_seconds: 0.5
  circuit_failure_threshold: 5
  circuit_window_seconds: 60
  circuit_cooldown_seconds: 30

limits:
  max_prompt_chars: 100000
  rate_limit_per_minute: 60
```

Each tier takes a **list** of providers, so fallback within a tier is possible.
Build it as a list from day one even when there's only one entry.

---

## 8. Module specifications

### 8.1 `classifier/signals.py`

Pure functions, no state, each independently testable. Each returns a number or
boolean, never a decision.

| Signal | Returns | Notes |
|---|---|---|
| `token_count(prompt)` | int | via tiktoken |
| `has_code(prompt)` | bool | fenced blocks, or high density of `{};()=` |
| `reasoning_markers(prompt)` | int | count of: "explain why", "step by step", "prove", "derive", "compare", "analyse", "debug", "optimise" |
| `multi_question(prompt)` | bool | more than one `?`, or enumerated sub-parts |
| `output_length_request(prompt)` | float | "write an essay", "in detail", "briefly", "one word" |
| `is_lookup(prompt)` | bool | starts with what/when/where/who + short |
| `language_complexity(prompt)` | float | avg sentence length, rare-word ratio |

### 8.2 `classifier/heuristic.py`

Combines signals into a score in `[0, 1]` using explicit weights defined at the
top of the file as a dict. Weights must be readable and changeable in one place.

Returns `(score: float, signals: dict)`. Always returns the signals so the API can
expose them.

**Known limitation to handle:** short prompts can be hard. "Prove Fermat's last
theorem" is 6 tokens. `reasoning_markers` and `language_complexity` are what stop
length from dominating. Weight accordingly.

### 8.3 `classifier/embedding.py`

- Loads `all-MiniLM-L6-v2` once at startup, never per request
- Embeds `eval/labelled.json` examples once at startup, holds vectors in memory
- On request: embed prompt, cosine-similarity against all labelled vectors, take
  k=5 nearest, majority tier wins
- Returns `(tier: str, confidence: float)` where confidence is the fraction of the
  k neighbours agreeing

### 8.4 `router.py`

The core. Keep it readable — this is the file I have to defend.

```
select_tier(prompt, config) -> RoutingDecision
```

Logic:
1. Run heuristic scorer → score + signals
2. If mode is hybrid or embedding, run embedding classifier → tier + confidence
3. Reconcile: in hybrid mode, if the two disagree, take the higher tier
   (fail towards quality, not cost)
4. If `escalate_on_low_confidence` and confidence < threshold, bump one tier up
5. Map to tier via thresholds, pick first healthy provider in that tier's list
6. Return the decision object with every intermediate value populated

Rule 3 is a deliberate choice: a wrong cheap answer costs more in trust than a
wrong expensive one costs in money. Record this in DECISIONS.md.

### 8.5 `cache.py`

Redis keys:
- `mm:vec:{id}` → the embedding, as bytes
- `mm:ans:{id}` → JSON `{prompt_hash, response, tier, model, tokens_out, created_at}`
- `mm:index` → a Redis list/set of live ids for the linear scan

Lookup: embed prompt → compare against all stored vectors → if best cosine
similarity ≥ threshold, return that answer.

A linear scan is fine at 10k entries and is the honest simple choice. Do not add a
vector database. If scan time exceeds 30ms, say so and we'll discuss.

Write on every successful non-cached response. Evict oldest when over
`max_entries`.

**The similarity threshold is the most dangerous setting in this project.** Too
loose and semantically different prompts share an answer. There must be a test in
`tests/` that demonstrates a wrong-answer case at a deliberately loose threshold.

### 8.6 `providers/base.py`

```python
class Provider(ABC):
    name: str
    async def complete(self, prompt: str, model: str,
                       max_tokens: int) -> ProviderResult: ...
```

`ProviderResult` carries: `text`, `tokens_in`, `tokens_out`, `raw_latency_ms`.

Each provider adapter translates to and from its own API shape and normalises
errors into: `ProviderTimeout`, `ProviderRateLimited`, `ProviderServerError`,
`ProviderBadRequest`. The resilience layer only ever sees these four.

`mock.py` returns canned text after a configurable sleep. It exists so load tests
never touch a real provider.

### 8.7 `resilience.py`

Three mechanisms, applied in this order:

**Retry** — on timeout, rate limit, or 5xx. Up to `max_retries`. Exponential
backoff: `backoff_base * 2^attempt`, plus jitter. Never retry a `BadRequest` —
it will fail identically.

**Fallback** — after retries exhaust, try the next provider in the same tier. If
the tier has no other provider, escalate one tier up rather than failing outright.

**Circuit breaker** — per provider, three states:
- `closed`: normal
- `open`: `circuit_failure_threshold` failures within `circuit_window_seconds`;
  provider is skipped entirely
- `half_open`: after `circuit_cooldown_seconds`, allow exactly one probe request.
  Success closes the circuit; failure reopens it and restarts the cooldown.

State lives in memory. Single-process is acceptable and should be noted as a
limitation.

### 8.8 `db.py`

- `init_db()` on startup, creates table and indexes if absent
- `log_request(record)` — one call, one row, never batched
- Writes must not block the response. Use FastAPI `BackgroundTasks`.
- **A failed database write must never fail the request.** Catch, log to stderr,
  continue.

### 8.9 `metrics.py`

Aggregate queries only. Nothing here computes routing.

- `get_stats(window)` → cost saved, cache hit rate, p50/p95 latency, request count,
  tier distribution, fallback count, error rate
- `get_recent(limit)` → recent rows for the feed

Percentiles: compute in SQL where possible, in Python otherwise. Do not
approximate silently.

---

## 9. Testing plan

`tests/`, using pytest.

**Unit**
- Each signal function against hand-written prompts with known expected values
- Heuristic scorer: verify a short-but-hard prompt scores above a long-but-easy one
- Cosine similarity correctness
- Circuit breaker state machine: closed → open → half_open → closed, and
  half_open → open on probe failure
- Cost calculation against known token counts

**Integration** (with `mock.py`)
- Full request path returns a well-formed response
- Cache hit returns without calling any provider — assert the mock was not invoked
- Forced provider failure triggers retry, then fallback
- All providers failing returns 502 and still writes a row

**Safety**
- Loose cache threshold serves a wrong answer — this test documents the risk
- Oversized prompt returns 400, not a crash
- Empty prompt returns 400

**Evaluation** (`eval/run_eval.py`, not pytest)
- Runs the evaluation set through router and baseline, reports cost, latency,
  quality side by side

---

## 10. Security and privacy

Not optional, and mostly not obvious. Address these as you build, not at the end.

**Cache leakage is the real risk in this architecture.** The semantic cache stores
prompts and answers. If one user pastes something sensitive and another user sends
a semantically similar prompt, the first user's answer is served to the second.
Mitigations required:
- Store only `prompt_preview` (80 chars) and a hash, never the full prompt, in the
  request log
- Support a `bypass_cache` flag on requests
- Document this clearly in the README as a known limitation of shared caching

**Key handling** — keys only from environment, never in config.yaml, never logged,
never in an error message returned to the caller. `.env` in `.gitignore` before
the first commit.

**Input limits** — enforce `max_prompt_chars` before any embedding or provider
call. An unbounded prompt is a cost attack.

**Rate limiting** — per-caller, in-memory token bucket. Prevents one caller
draining the API budget.

**Error messages** — provider errors are normalised before being returned. Never
pass a raw provider response to the caller; it may contain internal detail.

---

## 11. Build stages

Do not start a stage until the previous one's completion check passes.

### Stage 1 — Bare proxy (days 1–3)
One endpoint, one provider, no routing, no cache. Plus the database and logging.

**Done when:** a curl request returns an answer and the row is visible in SQLite.

### Stage 2 — Two providers, naive rule (days 4–5)
Add a second provider. Route on token count alone. Run 50 varied prompts and
record where it chooses badly.

**Done when:** a written list of misroutes exists in DECISIONS.md.

### Stage 3 — Real classifier (days 6–8)
Signals, heuristic scorer, labelled dataset, embedding classifier, hybrid
reconciliation. Measure accuracy of each mode on a held-out split.

**Done when:** accuracy figures for heuristic, embedding, and hybrid are recorded.

### Stage 4 — Cache (days 9–10)
Redis, embedding storage, similarity lookup, threshold tuning, eviction.

**Done when:** measured hit rate exists, and the threshold choice is justified in
DECISIONS.md with the failure case documented.

### Stage 5 — Resilience (days 11–12)
Timeouts, retry, fallback, circuit breaker. Test by deliberately breaking things.

**Done when:** the system keeps serving with one provider forced to fail.

### Stage 6 — Metrics, dashboard, evaluation (days 13–14)
Stats endpoints, SSE stream, dashboard views, evaluation script, load test against
mocks.

**Done when:** the README results table has real measured numbers and no
placeholders.

**Day 15 is buffer.** Something will break unexpectedly.

---

## 12. Working agreement with Claude Code

- Build one piece at a time. Stop after each and let me run it.
- Explain what you wrote — I have to defend this code.
- No dependency outside section 3 without asking.
- No ORM, no vector database, no framework beyond FastAPI.
- Prefer explicit over clever. If a junior reader would need to think twice, rewrite it.
- When you make a design choice I did not specify, say so explicitly so I can
  record it in DECISIONS.md.
- Do not write the dashboard before stage 6, however tempting.

---

## 13. Things that will go wrong

Flagged in advance so they are not surprises.

- **Model names change.** Every provider renames and deprecates models. Check
  current docs rather than trusting any name in this file or in training data.
- **Free tiers rate limit.** Load test against `mock.py` only.
- **The embedding model download is ~80MB** on first run and needs internet.
- **OneDrive is a correctness risk, not just a sync annoyance.** The project
  currently lives at `C:\Users\krish\OneDrive\Documents\ModelMux`. As of Stage 1
  there is a **SQLite database** in that folder. OneDrive syncing a file while
  SQLite holds a lock can corrupt it, and WAL mode adds two companion files
  (`-wal`, `-shm`) that must stay consistent with it. `.venv` syncing (thousands
  of small files) is the milder version of the same problem.
  **Recommended: move the project to a non-synced path such as `C:\dev\ModelMux`.**
  `*.db`, `*.db-wal`, `*.db-shm` are gitignored, but git ignoring a file does
  not stop OneDrive from syncing it.

- **Redis has no official native Windows build.** Stage 4 is blocked until one of:
  Docker Desktop (`docker run -p 6379:6379 redis`), WSL2 + `apt install redis`,
  or Memurai (Windows-native, Redis-compatible). Decide before Stage 4 starts.

- **~~Python 3.13 will break Stage 3~~ -- CHECKED, IT WILL NOT (2026-09-08).**
  The concern was that `sentence-transformers` pulls in PyTorch, and that the
  newest Python often lacks wheels. Verified against PyPI:
  `torch-2.14.0-cp313-cp313-win_amd64.whl` exists, and `sentence-transformers`
  6.0.1 ships a pure-Python wheel. **Staying on 3.13. No venv rebuild.**
  Kept here as a worked example: the risk was real in general and false in this
  specific case, and one API call settled it. Check before rebuilding anything.

- **Redis source on Windows: WSL2.** Docker Desktop is not installed; WSL2 with
  Ubuntu is. Before Stage 4: `wsl -e sudo apt install redis-server`, then
  `wsl -e redis-server --daemonize yes`. WSL2 forwards localhost, so
  `redis://localhost:6379` works unchanged from Windows.
- **Windows path handling** — use `pathlib`, never string concatenation.
- **Cost figures drift.** Prices in config.yaml are a snapshot; date them.