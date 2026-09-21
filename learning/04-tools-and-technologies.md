# 04 — Every tool and technology: what, why, and what you'd be asked

The full dependency list is nine packages. **That is itself a talking point:**
no ORM, no vector database, no LangChain, no framework beyond FastAPI.

```
fastapi==0.141.1            uvicorn[standard]==0.52.4
httpx==0.28.1               python-dotenv==1.2.3
PyYAML==6.0.3               tiktoken==0.14.0
sentence-transformers==6.0.1  redis==8.1.0
pytest==9.1.1  (dev)
```

> **Why so few?** Every dependency is a thing you must understand, update and
> defend. The rule on this project was that adding one is *a conversation*,
> not a default — which is why LLM-as-judge quality grading was **not** built:
> it needs the `anthropic` SDK, which isn't on the approved list.

---

## FastAPI + uvicorn

**What:** an async Python web framework built on Starlette, with Pydantic
validation and automatic OpenAPI docs. uvicorn is the ASGI server that runs it.

**How it works:** ASGI is the async successor to WSGI. Where WSGI handles one
request per worker thread, ASGI runs an event loop — so a worker waiting on a
2-second model call can serve hundreds of other requests meanwhile.

**Why here:** this application is almost entirely *I/O-bound waiting on
someone else's HTTP call*. That is the exact shape async was built for.

**Interview questions:**

> **Q: Why async for this and not Flask?**
> Nearly all wall-clock time is spent waiting on provider HTTP calls. Under
> sync workers each in-flight request holds a whole worker hostage for
> seconds. With async, one worker handles many concurrent waits. The CPU work
> — classification — is ~12ms, small enough not to starve the loop, though at
> high concurrency it would belong in a thread pool.

> **Q: What's the danger of async here?**
> A blocking call inside a coroutine stalls the entire event loop, not just
> that request. The embedding model's forward pass is CPU-bound and synchronous
> — at scale that's the thing to move to `run_in_executor`.

> **Q: Why does `/health` not check providers?**
> A liveness probe that depends on a third party reports their outage as your
> death, and the orchestrator kills your pods for it. Liveness = "am I
> running". Dependency health is a separate endpoint.

---

## Pydantic

**What:** validation and serialisation from Python type hints.

**Why here:** the request/response contract is enforced at the boundary, so no
handler needs defensive `isinstance` checks. Invalid input returns 422 with a
precise field-level message before any code runs.

> **Q: Where should validation live?**
> At the edge, once. Inside the system, trust your types. Validating repeatedly
> in every function is noise that hides where the real boundary is.

---

## httpx

**What:** an async-capable HTTP client, requests-like API.

**Why here:** `requests` is synchronous and would block the event loop. httpx
gives `AsyncClient` with connection pooling.

**The critical detail — one shared client:**

> Creating a client per request throws away connection pooling and re-does the
> TLS handshake every time. One `AsyncClient` for the app's lifetime, with
> separate **connect (5s)** and **read (30s)** timeouts — they mean different
> things. Failure to connect in 5s means the host is unreachable; a model
> legitimately taking 20s to think is not a failure.

---

## SQLite (stdlib `sqlite3`, no ORM)

**What:** a serverless, single-file relational database.

**Why here:** one file, zero setup, real SQL. The spec's path is SQLite in dev
→ PostgreSQL later, and no ORM means nothing to port but connection handling.

**What it taught:**

- **WAL mode** (`PRAGMA journal_mode=WAL`) allows concurrent readers alongside
  one writer. Without it, the dashboard reading blocks a request writing.
- **The context manager commits — it does not close.**

```python
with sqlite3.connect(path) as conn:    # commits on exit
    conn.execute(...)
# connection is STILL OPEN — this leaks
```

> That bug only surfaced when a temp file refused to delete on Windows. **A
> connection leak on Linux is invisible until you run out of descriptors; on
> Windows it shows up immediately as a locked file.**

- **The database is outside the OneDrive-synced folder** — sync + SQLite locks
  + WAL companion files is a corruption risk (D7).

**Interview questions:**

> **Q: When is SQLite the right choice, and when not?**
> Right: single-writer workloads, embedded use, local tooling, up to
> substantial read concurrency with WAL. Wrong: multiple concurrent writers,
> network access from several machines, or anything needing real user
> management.

> **Q: Why no ORM?**
> One table and a handful of queries. An ORM would add a dependency, a
> migration system and a layer of indirection over SQL I'd have to read
> anyway. **An ORM earns its cost with many entities and relationships; with
> one table it's pure overhead.**

---

## Redis

**What:** an in-memory key-value store. Here: the semantic cache backing store.

**How it's used:** three key spaces — `mm:vec:` (embeddings as float32 bytes),
`mm:ans:` (the answers), `mm:index` (the set of live entry ids) — written
together in a **pipeline** so a vector and its answer can't diverge.

**Why not a vector database?** The spec forbids one, and at 10,000 entries a
brute-force cosine scan over float32 vectors takes **11.2ms** — inside the 30ms
budget. A vector DB is a dependency, an operational surface, and a thing to
explain, bought to solve a problem that doesn't exist at this scale.

> **Interview gold:** *"When would you add one?"* When the linear scan stops
> fitting the budget — roughly 10⁵–10⁶ vectors. Then you want an ANN index
> (HNSW/IVF) via Redis Search, FAISS, or a managed store, and you accept
> *approximate* recall in exchange. **Knowing the crossover point matters more
> than knowing the tool.**

**Operational note:** Redis has no supported native Windows build, so it runs
in **WSL2** (Ubuntu), which forwards `localhost` (D9).

> **Q: TTL vs max_entries — why both?**
> TTL (24h) bounds staleness: a cached answer about a changing world shouldn't
> live forever. `max_entries` (10,000) bounds memory and keeps the scan inside
> its latency budget. They constrain different resources.

---

## sentence-transformers / `all-MiniLM-L6-v2`

**What:** a small transformer producing 384-dimensional sentence embeddings.
Semantically similar text lands close together in vector space.

**Why this model:** 384 dims is small enough for a fast cosine scan; it runs on
CPU in ~10ms; and it's small enough that loading it doesn't dominate startup.
The whole classification budget is 20ms — a larger model blows it immediately.

**Two things it taught this project:**

1. **Embed the question, not the prompt.** Embedding a 4,000-char prompt cost
   55ms *and* measured mostly pasted context. `EMBED_MAX_CHARS = 400` after
   `question_part()` clipping made it both faster and more correct. **The perf
   fix and the correctness fix were the same change.**

2. **Cosine similarity measures topical closeness, not semantic equivalence.**
   A 0.99-similar pair had *opposite* correct answers. Negation barely moves a
   vector. Hence the lexical inversion guard.

> **Q: How does k-NN classification work here?**
> Embed the incoming prompt, cosine-compare against 60 hand-labelled examples,
> take the 5 nearest, majority-vote the tier. Confidence = how many of the 5
> agreed. Below `confidence_threshold: 0.60` the router escalates rather than
> guesses.

> **Q: Why would embeddings beat handwritten rules?**
> They generalise to phrasings you never enumerated. Rules only fire on markers
> you thought of. Here the embedding classifier was *less* accurate overall
> (72% vs 88%) but made **zero** too-cheap errors — a different and more useful
> error profile.

---

## tiktoken

**What:** OpenAI's BPE tokenizer.

**Why here:** pre-call token estimates for the length signal and for cost
projection. Tokens, not characters, are the billing unit.

**The honest caveat:** it's OpenAI's tokenizer. For Anthropic and Google it's
an *approximation* — good enough for routing, never used for billing. **Real
counts come from the provider's `usage` block.**

> **Q: Why count tokens at all if the provider tells you?**
> Because you need the number *before* the call: to pick a tier, to enforce
> `max_prompt_chars`, and to project cost. The provider's count arrives too
> late to route on.

---

## python-dotenv

**What:** loads `.env` into `os.environ`.

**The thing that cost this project weeks:**

> **`load_dotenv()` does not overwrite a variable already in the environment.**

That's correct behaviour — production injects real variables and a stray `.env`
baked into an image must not override them — **but it is silent**. A Windows
User-scope `GROQ_API_KEY` set months earlier shadowed the new key in `.env`,
and every run used the dead one. See `07-war-stories.md` #1.

Fixed by `config.load_env()`, which **warns** when the environment shadows a
differing `.env` value, printing the last four characters of each — never the
key itself.

---

## PyYAML

**What:** YAML parsing. Here: `config.yaml`.

**The gotcha worth knowing:** YAML's type inference bites. Unquoted `no`,
`yes`, `on`, `off` become booleans; a version like `1.10` becomes a float and
loses the trailing zero. **Quote anything that must stay a string.** Use
`safe_load`, never `load`, which can instantiate arbitrary Python objects.

---

## pytest

**131 tests, no network, no API key, no cost.**

**What makes the suite possible:** `providers/mock.py` — a deterministic fake
with a `fail_with` switch. Built on **day 1**, not when testing got hard, which
is why forced-failure resilience testing needed no real outage.

**Four test-isolation bugs, all the same shape:** module-level state (loaded
models, cache clients, circuit breakers, rate-limit buckets) inherited between
tests. Recognising them as *one repeated shape* rather than four unrelated bugs
is the actual lesson.

**Tests that assert wrong behaviour on purpose:** marked `stage2_failure`, they
pin the naive router's misroutes. When Stage 3 landed they broke — **that break
was the signal it worked.**

**The suite went from 5s to 9m46s once** because the embedding model reloaded
per test. Session-scoped fixtures brought it back. **Test runtime is a feature:
a slow suite stops being run.**

> **Q: How do you test code that calls a paid third-party API?**
> Inject the provider. Tests supply a deterministic fake with switchable
> failure modes. You get every error path — timeout, 429, 500, bad request —
> reproducibly, instantly, free. Contract-test the real adapters separately
> against recorded responses.

---

## Git

Beyond the basics, two things this project actually leaned on:

- **Committing at the end of a unit of work is a recovery plan.** When
  `learnings/` vanished from disk mid-session, `git checkout -- learnings/`
  restored 19 files. Fifteen minutes earlier it would have been gone.
- **Commit messages carry the *why*.** This repo's messages explain reasoning
  and cost, because `git log` is the only documentation that can't drift out of
  sync with the code.

---

## The tools that were deliberately NOT used

**This list is as interesting as the one above.**

| Not used | Why |
|---|---|
| **LangChain / LlamaIndex** | The routing logic *is* the project. Importing it would hide the thing being demonstrated. |
| **A vector database** | Brute-force cosine at 10k entries is 11.2ms. A dependency bought to solve a problem that doesn't exist yet. |
| **An ORM** | One table. |
| **React + Vite + Recharts** | Spec'd, then cut (D24). One static HTML file, no build step, no CORS. |
| **Server-Sent Events** | Cut (D24). Real value, real complexity, not required to prove the thesis. |
| **`anthropic` SDK for LLM-as-judge** | Not on the approved dependency list. Adding one is a conversation. |
| **k6 load testing** | Spec'd against mock providers; not reached. |

> **Interview framing:** *"The spec asked for React. I shipped one HTML file
> and wrote down why. The deliverable was a working chart, and a build
> pipeline, a node_modules tree and a CORS configuration weren't part of
> that."*
