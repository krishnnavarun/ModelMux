# 10 — Rebuilding it manually, and what to learn at each step

You asked how you'd learn this if you built it by hand. **This is that path.**

Each step has: what you build, what to learn *before* writing it, how to prove
it works, and the trap waiting for you.

> **The rule that makes this work:** build one piece at a time and **confirm it
> runs before moving on.** Every step below ends with something you can
> execute. If you can't run it, don't start the next step.

---

## Step 0 — Environment (1 hour)

**Build:** a virtualenv, a `requirements.txt`, a `.gitignore`, a git repo.

**Learn first:** what a virtualenv actually is (a directory with its own
interpreter and `site-packages`), and the difference between `pip install X`
and pinning `X==1.2.3`.

**Prove it:** `python -c "import fastapi; print(fastapi.__version__)"`

**The trap:** putting the virtualenv inside a cloud-synced folder. This project
hit it — 1.1 GB and 36,030 files that OneDrive tried to sync. **Regenerable
binaries don't belong in a synced folder.** Source does; `.venv` doesn't.

📖 `learnings/02-python-environments.md`

---

## Step 1 — The bare proxy (half a day)

**Build:** FastAPI app, one endpoint `POST /v1/chat`, one provider adapter,
Pydantic schemas, config loading from YAML.

**Learn first:**
- **ASGI vs WSGI** — why async matters when you're waiting on HTTP
- **Pydantic models** as the request/response contract
- **`async`/`await`** — enough to know that a blocking call in a coroutine
  stalls the whole loop
- **httpx `AsyncClient`** — and why you create *one*, not one per request

**Prove it:** `curl` the endpoint, get a model's answer back.

**The trap:** creating an HTTP client per request. You throw away connection
pooling and redo the TLS handshake every time.

📖 `learnings/03-async-python.md`, `05-fastapi-asgi.md`, `06-pydantic-validation.md`

---

## Step 2 — Persistence (half a day)

**Build:** the SQLite schema, WAL mode, a `log_request()` that can never fail
a request.

**Learn first:**
- Why **WAL mode** allows concurrent reads during a write
- That `with sqlite3.connect()` **commits but does not close**
- Why you write `cost_usd` into the row rather than computing it later

**Prove it:** make a request, then `SELECT * FROM requests` and see the row.

**The trap:** the connection leak above. It's invisible on Linux until you run
out of descriptors; on Windows a temp file refuses to delete and you find it
immediately.

📖 `learnings/07-sqlite-persistence.md`

---

## Step 3 — More providers, and the error taxonomy (1 day)

**Build:** two more adapters behind a shared ABC, a registry, and the four
exception types.

**Learn first:**
- The **three API shapes** — OpenAI-compatible, Gemini's `contents/parts`,
  Anthropic's `content` blocks
- **Which HTTP failures are transient and which are deterministic** — this is
  the whole design
- That **a 200 with the wrong body shape is still a failure**

**Prove it:** the same prompt answered by each provider through one interface.

**The trap:** mapping `ProviderBadRequest` to HTTP 400. That blames the
*caller* for *your* dead API key. It's a 502 — the upstream is misconfigured,
and the client did nothing wrong.

📖 `learnings/04-networking-http.md`, `11-llm-provider-apis.md`

---

## Step 4 — A deliberately bad router (half a day)

**Build:** routing on token count alone. **And a script that reports its
misroutes against hand-labelled expectations.**

**Learn first:** tokenization — why tokens not characters, and that
`tiktoken` is OpenAI's tokenizer and only approximates others.

**Prove it:** the misroute report runs and shows failures, sorted into *too
cheap* and *too expensive*.

**Why build something bad on purpose:** because *"we need a better
classifier"* is an opinion until you have the list. **Write the trap prompts
first** — short-and-hard, long-and-easy — so the failure is designed rather
than discovered.

**The trap:** skipping this step because it feels like wasted work. It's the
step that justifies everything after it.

📖 `learnings/12-tokenization.md`, `13-routing-evaluation.md`

---

## Step 5 — The real classifier (2 days — the hard part)

**Build:** signal extraction, weighted scoring with floors, an embedding k-NN,
and the reconciliation between them.

**Learn first:**
- **What an embedding is** — text → a vector where distance means similarity
- **Cosine similarity**, and why it's the right metric for direction not
  magnitude
- **k-NN** — and why k should be odd
- **The difference between a training score and a held-out score**

**Prove it:** accuracy for all four modes against **two** sets, with the gap
printed.

**The traps, and there are three:**

1. **Tuning and reporting on the same set.** Write the held-out set *after*
   tuning finishes, and never look at it while adjusting weights.
2. **Using a weighted average alone.** "Prove Fermat's Last Theorem" is six
   tokens; the average buries it. You need **floors**.
3. **Embedding the whole prompt.** Slow *and* wrong — you measure pasted
   context instead of the question. Clip to the question first.

📖 `learnings/15-embeddings.md`, `13-routing-evaluation.md`

---

## Step 6 — The semantic cache (1.5 days)

**Build:** Redis storage for vectors and answers, a cosine search, a threshold,
and a guard.

**Learn first:**
- **Redis basics** — keys, TTL, pipelines, why a pipeline makes two writes
  atomic-ish
- **Why you store float32 bytes**, not JSON lists
- **That cosine similarity measures topical closeness, not equivalence**

**Prove it:** a sweep script that reports hit rate and false-hit rate across
thresholds, against labelled equivalent and confusable pairs.

**The trap — and it's the important one:** assuming a threshold can solve this.
**It cannot.** You will find a pair at 0.99 similarity with opposite correct
answers, because negation barely moves a vector. The answer is a *second
mechanism* — a lexical inversion guard — not a better number.

**Build the confusable pairs deliberately:** "is X safe" vs "is X not safe",
"how to enable" vs "how to disable".

📖 `learnings/16-redis-and-caching.md`

---

## Step 7 — Resilience (1 day)

**Build:** retry with jittered backoff, within-tier fallback, tier escalation,
a circuit breaker per provider.

**Learn first:**
- The **circuit breaker state machine**, all four transitions
- **Why backoff needs jitter**
- The difference between **connect** and **read** timeouts

**Prove it:** with one provider forced to fail, the system still answers.

**The trap:** forgetting `HALF_OPEN → OPEN` on a failed probe, and forgetting
that it must **restart** the cooldown. Also: letting more than one probe
through — that's the herd the breaker exists to prevent.

**The thing that makes this testable:** build the mock provider with a
`fail_with` switch **on day 1**, not now. Deterministic failure means you never
need a real outage.

📖 `learnings/17-resilience-patterns.md`

---

## Step 8 — Metrics, rate limiting, dashboard (1 day)

**Build:** `/v1/stats` with exact percentiles, a token-bucket rate limiter, and
one static HTML dashboard.

**Learn first:**
- **Percentiles** — and why you compute them from successes only
- **Token bucket vs leaky bucket** — bursts allowed vs smoothed
- Why you **don't trust `X-Forwarded-For`**

**Prove it:** the dashboard renders live numbers from real rows.

**The trap:** building React. You need a chart, not a build pipeline. One file
served from the API's own origin needs no CORS setup and no `node_modules`.

📖 `learnings/18-rate-limiting.md`

---

## Step 9 — Evaluation (1 day) — *do this earlier than I did*

**Build:** a harness that runs the held-out set through both the router and a
baseline, and reports cost, latency and **quality**.

**Learn first:** what makes a measurement trustworthy — held-out data, blind
grading, stated sample size, stated caveats.

**Prove it:** a results table you'd defend to someone hostile.

**The traps:**

1. **Letting it produce numbers from mock data.** Make it *refuse* unless
   explicitly told to simulate.
2. **Computing cost differently from the server.** Share one function. This
   project's two implementations drifted by 40×.
3. **Building a blind-grading export with nothing that reads it back.** Half a
   mechanism looks finished and isn't.
4. **Leaving quality until last.** It's the only measurement that tests the
   project's actual claim. Build it first.

📖 `learnings/13-routing-evaluation.md`, `09-testing-pytest.md`

---

## The order to learn the concepts

If you want to study the *ideas* rather than follow the build:

| Tier | Concepts |
|---|---|
| **Foundations** | async/await · ASGI · HTTP semantics · REST status codes |
| **Data** | SQLite + WAL · Redis + TTL · why no ORM here |
| **ML, lightly** | embeddings · cosine similarity · k-NN · train/test separation |
| **Distributed systems** | retry · backoff + jitter · circuit breaker · rate limiting · fallback |
| **Engineering judgement** | config vs code · error taxonomies · measurement integrity · knowing what not to build |

**The last row is the one that gets you hired**, and it's the one no tutorial
teaches.

---

## Total: roughly 9-10 focused days

Which is about what this took. **The code is maybe 3 days of it.** The rest is
measuring, being wrong, and writing down why.
