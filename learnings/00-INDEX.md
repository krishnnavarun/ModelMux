# ModelMux — Learning Library

Reference notes keyed by **technology**, not by build stage. Every file follows
the same four-part shape:

1. **What it is** — the plain definition
2. **How it works** — the technology's own workflow, independent of this project
3. **How we used it in ModelMux** — real code from this repo, start → current phase
4. **Interview questions** — with answers, including the follow-ups a good
   interviewer asks second

---

## The files

| # | File | Covers |
|---|---|---|
| 01 | `01-project-timeline.md` | What was built when, and every mistake made. The chronological record. |
| 02 | `02-python-environments.md` | virtualenv, pip, wheels, ABI tags, requirements pinning, PowerShell execution policy |
| 03 | `03-async-python.md` | event loop, `async`/`await`, coroutines, blocking calls, concurrency vs parallelism |
| 04 | `04-networking-http.md` | TCP, TLS, handshakes, HTTP methods/status codes, connection pooling, timeouts, httpx |
| 05 | `05-fastapi-asgi.md` | WSGI vs ASGI, Uvicorn, Starlette, routing, lifespan, dependency injection, BackgroundTasks |
| 06 | `06-pydantic-validation.md` | schema validation, type coercion, 422 vs 400, validate-at-the-edge |
| 07 | `07-sqlite-persistence.md` | SQLite, ACID, WAL, indexes, connection handling, the context-manager trap |
| 08 | `08-config-and-secrets.md` | YAML, environment variables, dotenv, secret handling, 12-factor |
| 09 | `09-testing-pytest.md` | fixtures, conftest, markers, parametrize, mocks/fakes, test isolation |
| 10 | `10-design-patterns.md` | Adapter, Dependency Injection, Registry, ABC, error taxonomy, characterisation tests |
| 11 | `11-llm-provider-apis.md` | REST APIs of Groq/Google/Anthropic, OpenAI-compatibility, auth schemes, usage metadata |
| 12 | `12-tokenization.md` | tokens, BPE, tiktoken, why counts differ per provider, cost implications |
| 13 | `13-routing-evaluation.md` | classification, scoring, thresholds, labelled sets, held-out splits, measuring a router |
| 14 | `14-git-and-tooling.md` | git, .gitignore, line endings, what belongs in a repo |
| 15 | `15-embeddings.md` | embeddings, cosine similarity, k-NN classification, sentence-transformers, MiniLM |
| 16 | `16-redis-and-caching.md` | Redis types, TTL, pipelining, eviction, semantic caching and its risks |
| 17 | `17-resilience-patterns.md` | retry, backoff, jitter, circuit breakers, fallback, testing failure |
| 18 | `18-rate-limiting.md` | token bucket vs fixed window, 429 + Retry-After, keying, distributed limits |

---

## How to use this for interview prep

**Do not read these front to back.** They are reference, not a textbook.

The strongest thing you have is not knowing what FastAPI is — anyone can read
that. It is being able to say **"here is a decision I made, here is what it
cost, and here is how I found out I was wrong."** That is what separates a
candidate who used a framework from one who understands it.

So for each technology, be ready to answer three questions in order:

1. *What is it?* — the definition. Table stakes.
2. *How did you use it?* — section 3 of each file. Shows real experience.
3. *What went wrong, and what did you do?* — the mistakes. **This is the one
   that gets you hired.**

Every file's section 3 contains real bugs from this project. Learn those. A
story about a silent connection leak that only surfaced when a temp file would
not delete is worth more than a textbook definition of ACID.

## See also: `learning/` — the study guide

This folder is the **reference library, keyed by technology**. The sibling
folder `learning/` is the **study guide, keyed by how you'll be asked**:
project walkthrough, build stages, every constraint and limit, the decisions
as talking points, war stories in STAR form, a question bank, the system-design
version, and a cheat sheet.

Start at `learning/00-START-HERE.md`.

## The mistakes worth telling in an interview

Ranked by how much they demonstrate:

| Mistake | What it shows |
|---|---|
| `sqlite3` context manager commits but does not close → connection leak | Depth: reading past the intuitive API |
| Test fixture mocked one provider; routing change made tests hit a real API | Systems thinking: a change in A widened what B must cover |
| `ProviderBadRequest` → HTTP 400 blamed the caller for our dead key | Design judgement: status codes describe whose fault it is |
| Recommended a Python 3.11 rebuild, then checked PyPI and reversed it | Intellectual honesty + verifying before acting |
| `pytest_sessionfinish` silently never fired from a subdirectory conftest | Debugging: code that looks right and does nothing |
| Cost priced input tokens only, understating spend unevenly per tier | Measurement rigour in the metric being optimised |
| Tuned classifier weights on the same 50 prompts I reported accuracy on | Methodological honesty — caught it mid-stream and built a held-out set |
| Embedding the whole prompt cost 55ms and measured the wrong text | When a perf fix and a correctness fix are the same change |
| No cache threshold was safe — a 0.99-similarity pair had opposite answers | Knowing when a knob cannot solve the problem |
| The cache broke test isolation, repeating the Day 1 fixture bug | Recognising a lesson you already wrote down and still repeated |
| Test suite went 5s → 9m46s from a model reloading per test | Treating test runtime as a feature |
| Circuit breaker let two probes through — the herd it exists to prevent | State machines: side effects belong on every transition path |
| Four test-isolation bugs, all module-level state inherited between tests | Seeing a repeated shape rather than four unrelated bugs |
| Fixed flaky single-sample latency tests, then left an identical one unfixed | Fixing a class of bug, not just the instance in front of you |
| Built a harness that REFUSES to produce numbers from mock data | Protecting the result from yourself, not just the user from bugs |
| Two required features missing for 4 days past 113 green tests | Tests verify what you wrote, not what you failed to write |
| Large-tier price was a previous generation's rate, flagged UNVERIFIED for 2 weeks | A known-unverified number in a load-bearing position is a bug with a comment on it |
| Exported a blind spot-check that nothing could read back | Half a mechanism is a gesture at the problem, not a solution |
| Chased a 401 across three keys for weeks; a User-scope env var was shadowing `.env` | Reading evidence for what it RULES OUT: "0 API calls" means that key was never sent |
| Published cost numbers inflated 40x, from pricing every call at the tier's first provider | A bug in the thing that measures cost is worse than one in the thing that spends it |
| The savings % looked fine because both terms were inflated alike | A ratio can survive a bug that destroys both of its terms |
| A reasoning model returned HTTP 200 with empty content, and it nearly got cached as a win | The cheapest answer is silence — a cost optimiser must not be paid for it |
| Mock-provider runs poisoned the real Redis with canned answers | A guard that announces itself on write and stays silent on read is half a guard |
| The eval harness had never loaded `.env` in its life | The instrument gets less scrutiny than the thing it measures |
| Guessed torch thread contention, wrote a probe that measured the GIL instead, recorded it as unknown | Refusing to promote an untested hypothesis into an explanation |

Full write-ups in `01-project-timeline.md`.
