# Project Timeline

The chronological record: what was built, in what order, and every mistake made
along the way. The technology explanations live in files 02–14; this file is
*what happened*.

---

## Stage 1 — Bare proxy

**Goal:** prove one request can travel from a caller, through our service, to a
real LLM provider, and back — before adding routing, caching, or anything
clever.

### Built

```
requirements.txt   .env / .env.example   config.yaml
app/main.py        FastAPI app, POST /v1/chat, GET /health
app/config.py      config.yaml load + startup validation
app/schemas.py     request/response contract
app/db.py          SQLite schema, WAL, logging
app/providers/     base ABC + 4-error taxonomy, groq, mock
```

### Verified

- `GET /health` → 200
- Empty prompt → 400 with request_id
- 100,001-char prompt → 400, provider never called
- Real call → 502 (the Groq key returns 401)
- **Three database rows written, one per failure path**

### Mistakes

**1. Dumped a secret to the terminal.** Ran `od -c .env` to inspect byte
encoding; the redaction pattern failed and most of the API key printed.
*Lesson:* to validate a secret, check **properties** — length, prefix,
whitespace — never dump the value.

**2. Priced input tokens only.** `cost = tokens_in * rate_in`. Output tokens
bill at a different and higher rate (5x on the large tier). This does not just
understate spend, it understates it *unevenly across tiers* — corrupting the
exact comparison the project exists to make. → DECISIONS.md D6.

**3. Mapped `ProviderBadRequest` to HTTP 400.** Looks obviously right. It is
wrong: that error fires when *our* key is dead, so a 400 blames the caller for
our misconfiguration. Caught when the dead key produced
`400 {"error": "groq 401: Invalid API Key"}` — self-evidently incoherent.
→ DECISIONS.md D2.

---

## Interlude — Spec alignment

`specs.md` arrived after Stage 1 was written, and disagreed with it in eight
places. Four were serious: config hardcoded in Python, no database, one error
type instead of four, and the cost bug above.

**The lesson worth keeping:** *"it runs"* and *"it matches the spec"* are
different claims. Stage 1 passed the first and failed the second.

### Amended the spec itself

- **400 vs 422 was unresolvable as written.** Pydantic constraints fail in a
  layer that runs *before* our handler, returning 422 — so a length limit
  written that way could neither return 400 nor write a database row. Length
  checks moved into the handler.
- **OneDrive upgraded from annoyance to correctness risk.** Stage 1 put a SQLite
  database in a sync-watched folder. WAL means three files that must stay
  mutually consistent. → database moved to `%LOCALAPPDATA%`, DECISIONS.md D7.
- **Redis has no native Windows build** — not mentioned in the spec, hard-blocks
  Stage 4. → WSL2, DECISIONS.md D9.

---

## Interlude — Testing and settling open questions

### The Python version reversal

I recommended rebuilding the venv on Python 3.11, reasoning that
`sentence-transformers` needs PyTorch and new Python versions often lack wheels.
Every part of that reasoning is a real pattern. **It was still wrong here.**

One PyPI query settled it:

```
torch-2.14.0-cp313-cp313-win_amd64.whl        exists
sentence-transformers 6.0.1                    pure-Python wheel
```

**Stayed on 3.13.** I was one step from having the user install a second Python
on the strength of a plausible generalisation I had not checked.

*Lesson:* a base rate tells you what to check. It is never a substitute for
checking. → DECISIONS.md D8.

### The mock provider

The Groq key was dead, so nothing could be verified end to end. Built
`providers/mock.py` early (spec puts it at Stage 6) — canned text, configurable
delay, five failure modes, call counting.

**This is the adapter boundary paying for itself.** `main.py` only ever calls
`Provider.complete()`, so a test swaps in the mock with one line and the
endpoint cannot tell. Result: 36 tests passing with no network and no API key.

### Verifying a test was not vacuous

`test_mock_counts_calls` is `async def`, and no async pytest plugin was
installed. An async test with no plugin can silently never execute and still
report green. Checked rather than assumed:

```
PASSED tests/test_providers.py::test_mock_counts_calls[asyncio]
```

The `[asyncio]` suffix proved the anyio plugin ran it.

*Lesson:* when a test passes for a reason you cannot name, find the reason. A
suite's value is entirely in its failures.

---

## Stage 2 — Two more providers, and a router built to fail

**Completion criterion (unusual):** *a written list of misroutes.* The
deliverable is not a working router — it is evidence about how a deliberately
bad one fails.

### Built

```
app/router.py           naive rule: token count -> 0-1 score -> tier
app/tokens.py           tiktoken, loaded once and warmed
app/providers/google.py, anthropic.py, __init__.py (registry)
eval/prompts.json       50 hand-labelled prompts
eval/run_routing_check.py
```

### The measurement

```
Prompts: 50
  correct         17  (34%)
  TOO CHEAP       28  (56%)   <- quality risk
  too expensive    5  (10%)   <- cost waste

Tier distribution chosen: {'small': 45, 'mid': 5}
```

**Three findings:**

1. **The failure is asymmetric and backwards.** SPEC 8.4 rule 3 says fail
   *towards quality*. The rule fails towards cost, 28:5.
2. **15/15 where length and difficulty agree; 0/32 where they diverge.** Not a
   rule approximating difficulty badly — a rule measuring a *different quantity*
   that correlates only in easy cases.
3. **The large tier was never selected once.** Not designed into the experiment;
   it fell out of running it. Reaching `large` needs >396 tokens; nothing
   realistic is that long. The tier every `cost_if_large_usd` is priced against
   is dead code under this rule.

**Deliberately not fixed by retuning `saturation_tokens`.** That would improve
the number without improving the mechanism, and destroy the evidence Stage 3 is
built from. → DECISIONS.md D12.

### The boundary held

Three genuinely different API shapes — model in the URL path, three auth
schemes, three response structures — and **zero changes to `main.py` or
`router.py`**. That was the real test of Stage 1's design.

### Mistake: a test fixture that would have spent money

The Stage 1 fixture mocked one provider:

```python
test_client.app.state.providers["groq"] = mock_provider
```

Complete when everything routed to `groq`. Stage 2 made routing real, so a long
prompt resolved to `google` — the **real adapter** — and the suite attempted a
live HTTPS call.

It surfaced as a confusing `KeyError: 'routing'` only because there is no
`GOOGLE_API_KEY`. **With valid keys in `.env`, that test would have passed while
quietly making paid API calls on every single run.**

*Two lessons, second is bigger:*
1. Adding a code path silently widens what your test doubles must cover.
2. **Green is not proof of isolation.**

---

## Housekeeping pass — two silent bugs

A "just clean the folder" task surfaced two real defects. Both failed silently,
which is why 56 passing tests never caught them.

### `with sqlite3.connect(...)` does not close the connection

It commits or rolls back the transaction. That is all. Verified directly:

```python
with sqlite3.connect(p) as conn:
    conn.execute('CREATE TABLE IF NOT EXISTS t (x)')
conn.execute('SELECT 1')   # -> works fine. Still open.
```

Every logged request leaked one connection. On Windows that also holds a file
handle, which is what blocked deleting the temp database.

Fix needs **both** context managers:

```python
with closing(sqlite3.connect(path)) as conn, conn:
```

`closing()` closes; the bare `conn` commits.

### `pytest_sessionfinish` never fired

pytest honours initialisation hooks — `sessionstart`, `sessionfinish`,
`configure`, `addoption` — **only from the rootdir conftest or installed
plugins**. Ours lives in `tests/`, a subdirectory. The function was collected,
looked correct, and did nothing. Eight stray databases had accumulated.

Replaced with a session-scoped autouse fixture, which works from any conftest.

*Lesson, shared by both:* code that looks right and does nothing is harder to
catch than code that crashes.

---

## Current state

| Stage | Criterion | State |
|---|---|---|
| 1 — Bare proxy | curl returns an answer **and** row in SQLite | ⚠️ Partial — no live answer yet |
| 2 — Naive routing | written misroute list | ✅ Complete (D12) |
| 3 — Classifier | accuracy for heuristic/embedding/hybrid | ⬜ Not started |
| 4 — Cache | hit rate + justified threshold + failure case | ⬜ Not started |
| 5 — Resilience | keeps serving with a provider down | ⬜ Not started |
| 6 — Metrics/eval | README table with real numbers | ⬜ Not started |

**56 tests passing. No adapter has ever spoken to a live server.**

The single blocker on the critical path is a working API key — Stage 6's
criterion is *"real measured numbers, no placeholders"*, and that cannot be
faked or mocked.

---

## Stage 3 — The real classifier

**Completion criterion:** accuracy figures for heuristic, embedding and hybrid.

### Built

```
app/classifier/signals.py     8 pure signal functions
app/classifier/heuristic.py   weighted scorer + floors
app/classifier/embedding.py   MiniLM + k-NN over labelled examples
eval/labelled.json            60 reference examples (embedding training data)
eval/holdout.json             32 prompts, NEVER used for tuning
eval/run_classifier_eval.py   all 4 modes x both sets
eval/explain.py               full decision breakdown for any prompt
```

### The result

Held-out (n=32), the only honest column:

| mode | accuracy | too cheap | too expensive |
|---|---|---|---|
| naive_tokens | 31% | 20 | 2 |
| heuristic | **88%** | 4 | 0 |
| embedding | 72% | 0 | 9 |
| **hybrid (shipped)** | 72% | **0** | 9 |

**31% → 88%.** Classification at 11-12ms against a 20ms budget.

### The methodological mistake, caught mid-stream

Partway through I noticed I had been **tuning weights against the same 50
prompts I was reporting accuracy on**. That is a training score, not an accuracy
estimate, and the spec explicitly requires a held-out split.

I stopped, wrote `holdout.json` after tuning was finished, and now report both
columns plus the gap between them.

*This is the most valuable thing that happened today.* The number I would have
quoted was not wrong by accident — it was measuring the wrong thing entirely.

### What made the heuristic work, in order of impact

1. **Graded markers.** `prove`/`derive` (HARD) vs `compare`/`debug` (MEDIUM).
   Counting alone gave 0/11 on short-but-hard prompts.
2. **Floors instead of a pure average.** "Prove Fermat's Last Theorem" scored
   0.18 under averaging, because four near-zero components diluted the one that
   mattered. **Difficulty is not an average** — a prompt is hard if ANY single
   strong indicator fires.
3. **`floor + (1-floor) x weighted`, not `max(weighted, floor)`.** Blending
   keeps the floor's guarantee while letting length still count. +4 points.
4. **`question_context_ratio` scaling the length term.** Fixed
   `trap_long_but_easy` from 0/5 to 4/5. Not in the spec — discovered by
   measuring.
5. **Signals measured on the QUESTION, not the whole prompt.** This bit
   **twice**: once for reasoning markers, once for `multi_question`. A pasted
   recipe or email thread contains "compare" and question marks belonging to the
   context, not the request.

### The finding I did not expect

**Hybrid is 16 points LESS accurate than the heuristic alone, and ships anyway.**

```
heuristic   88%   4 too cheap,  0 too expensive
hybrid      72%   0 too cheap,  9 too expensive
```

It eliminates every quality risk by over-routing 28% of prompts. SPEC 8.4 rule 3
says a wrong cheap answer costs more in trust than a wrong expensive one costs
in money — so by the project's own values, that trade is correct.

**It also proves accuracy alone is the wrong scoreboard.** A single percentage
would have ranked heuristic first and hidden that it sends hard prompts to weak
models.

### The environment blocked the work

Installing PyTorch took `.venv` to **1.1 GB / 36,030 files** inside a
OneDrive-synced folder. Cold `import torch` measured **57-76 seconds**; a `du` of
the directory timed out after 5 minutes.

Moved the venv to `C:\dev\modelmux-venv`. Source stays in OneDrive and stays
backed up; a gigabyte of regenerable binaries does not. Import dropped to ~8s.

**I got the diagnosis wrong, and this is the more useful half of the story.**

I blamed OneDrive and moved the venv on that basis. Checked properly afterwards:

```
tasklist /FI "IMAGENAME eq OneDrive.exe"
INFO: No tasks are running which match the specified criteria.
```

**OneDrive was not running.** It could not have been syncing anything. And my
own third measurement had already shown the answer — 57s, then 76s, then **8.7s
once the OS file cache warmed.** The problem was cold-disk first-read cost, and
it was resolving itself.

The move was still the right practice: 1.1 GB of regenerable binaries should not
sit in a folder a sync client watches, and SPEC 13 says so. But it was
**hygiene, not a performance fix**, and I presented it as urgent.

> **The lesson: I noted that the OneDrive process check came back empty, called
> it a caveat, and then drew the conclusion anyway.** A caveat that does not
> change your conclusion is not a caveat — it is decoration. When the evidence
> for a cause is absent, the honest move is to say the cause is unknown, not to
> proceed with a confident story and a footnote.

Cost of the mistake: ten minutes of reinstall, and a changed path. Nothing was
lost, because a venv is regenerable by definition — which is exactly why it was
safe to move on a bad diagnosis.

### Mistakes

**1. The `\r` escape, twice.** Writing `eval\run_eval.py` in a non-raw Python
string put a carriage return mid-line and mangled `CLAUDE.md`. I had *already
written this up* in `learnings/14` from the first occurrence, and did it again
the same day. Fixed by using forward slashes, which PowerShell accepts.

**2. Piping masked a failed install.** `pip install ... | tail -6` reported exit
code 0 while pip had actually failed on a timed-out torch download. The pipe's
exit code is the last command's. Re-ran capturing the real status.

**3. `max()` discarded information.** The first floor implementation made a
194-token multi-part architecture question and a 10-token "compare A and B"
score identically at 0.45. Blending fixed it.

---

## Stage 4 — The semantic cache

**Completion criterion:** a measured hit rate, and a threshold justified in
DECISIONS.md with its failure case documented.

### Built

```
app/cache.py                    Redis semantic cache, inversion guard, mirror
eval/cache_pairs.json           15 equivalent + 20 dangerous prompt pairs
eval/tune_cache_threshold.py    the threshold sweep
tests/test_cache.py             17 tests, including the required safety test
```

### The result

```
HIT RATE (rephrasings): 12/15 = 80%
FALSE HITS (dangerous):  0/20 =  0%
lookup at 10,000 entries: 11.2ms   (budget 30ms)
74 tests passing
```

### The finding that shaped the whole stage: no threshold is safe

Sweeping similarity thresholds against labelled pairs:

| threshold | hit rate | false hits |
|---|---|---|
| 0.92 | 47% | 1 |
| **0.98** | **7%** | **1** |

Even at 0.98 — where the cache has stopped earning its keep — this still hits:

```
0.9903  "Convert 32 Fahrenheit to Celsius."
     vs "Convert 32 Celsius to Fahrenheit."
```

Opposite operations. Answers 0 and 89.6.

**Embeddings encode topic and vocabulary, not logical direction.** Negation,
antonyms and argument order are what they represent worst, because the two
sentences share nearly every token. No single cosine threshold can separate
them — this is structural, not a tuning failure.

Fixed with a second, purely lexical gate: antonym swaps, and **exact positional
exchange** (two words landing in each other's slots). With the guard, every
threshold from 0.84 up has zero false hits.

**Chose 0.88, not the lowest safe 0.84.** The highest dangerous pair surviving
the guard scores 0.8156; 0.88 leaves a +0.064 margin where 0.84 leaves +0.024.
Taking the lowest value that works on my own 20 pairs would have been the Day 1
overfitting mistake in a new place.

### The scan that did not scale

SPEC said to report it if a linear scan exceeded 30ms. It did — **74.4ms at
1,000 entries**, extrapolating to ~650ms at the configured 10,000 ceiling.

The cost was the **round trip** (1.5MB of float32 per lookup), not the matmul.
Mirroring the vectors in process, invalidated by a Redis version counter, took
10,000 entries to **11.2ms** — flat with size, no new dependency, still the same
linear scan.

### Mistakes

**1. The cache broke test isolation.** The mock provider returns identical text
for every prompt, so a live cache turned the second request in a test run into a
HIT. `mock.calls` stopped counting provider calls, and
`test_provider_failure_returns_502` got a 200 because a cached answer arrived
before the deliberately-failing provider was reached.

*This is the Day 1 provider-fixture failure in a new place:* **adding a code
path silently widened what the test doubles had to cover.** I had written that
lesson down and still walked into it.

**2. And the first fix was wrong.** `test_cache.py` popped the disable flag at
module import — but **pytest imports every test module before running any
test**, so it re-enabled the cache for `test_api.py` too. The opt-in had to move
inside the fixture.

**3. The suite went from 5 seconds to 9m46s.** `embedding.init()` loads an 80MB
model in ~10-15s, and it runs from the app lifespan — which a function-scoped
`client` fixture enters *once per test*. Made `init()` idempotent: **9m46s →
57s.** A suite that slow stops being run, which costs more than any bug it
catches.

**4. A latency test that measured the wrong thing.** `classify_ms` came in at
31ms against a 20ms budget. The prompt was `"hello " * 200` — 1200 characters
with no sentence boundaries, so `question_part()` had nothing to clip to. That
is a pathological input, not representative traffic. Split into two tests: a
realistic prompt against the 20ms budget, and the pathological case pinned at a
stated worst-case bound. Also capped embedding input at 400 chars (D18), chosen
on what the classifier needs rather than on what made the test pass.

### The environment fought back

Redis answered `PONG` inside WSL while Windows got ConnectionRefused. Measured:
five consecutive refusals, then five successes right after any `wsl` command
woke the instance. **WSL2 localhost forwarding drops when the VM idles.**

Two mitigations: Redis binds `0.0.0.0` inside WSL, and `cache.init()` falls back
to discovering WSL's IP. The IP changes on every restart, so it is discovered,
never hardcoded. **The fallback has since fired in a real run** — not theory.

---

## Stage 5 + 6a — Resilience and metrics

**Completion criterion:** the system keeps serving with one provider forced to
fail.

### Built

```
app/resilience.py        retry, fallback, circuit breaker, dispatch()
app/metrics.py           get_stats(), get_recent(), exact percentiles
GET /health/providers    per-provider circuit state and last error
GET /v1/stats?window=    cost saved, hit rate, p50/p95, tier distribution
GET /v1/requests?limit=  recent rows for the live feed
tests/test_resilience.py 19 tests, every circuit transition
tests/test_metrics.py    19 tests
```

### The gate, measured end to end

```
1. all healthy        200  tier=small  provider=groq       attempts=1
2. groq FORCED DOWN   200  tier=large  provider=anthropic  attempts=7  escalated
3. /health/providers  groq: open, 6 recent failures, last="mock 500"
4. everything down    502  attempts=3  tried=3 providers
5. /v1/stats          3 requests, 33% errors, 2 fallbacks, 48.9% saved
```

Step 2 is the gate. Step 4 shows the breaker earning its keep: **3 attempts,
not 7**, because groq's circuit was already open and got skipped rather than
re-paying the timeout to rediscover a known failure.

**113 tests passing.**

### The stage where Stage 1's design paid off again

`providers/base.py` defined four error types on Day 1 with no immediate use.
This is where they became control flow rather than documentation — the taxonomy
*is* the retry policy. And `MockProvider.fail_with`, also written on Day 1,
is why every circuit transition is tested deterministically in under a second
instead of waiting for a real outage.

### Mistakes

**1. The circuit let TWO probes through instead of one.** The
`OPEN -> HALF_OPEN` transition returned `True` without setting
`probe_in_flight`, so the next caller fell through to the HALF_OPEN branch,
found the flag unset, and was also allowed past.

That is exactly the **thundering herd the breaker exists to prevent** — two
requests hitting a recovering provider at its most fragile moment.

*Lesson:* a state machine with a side effect on a transition needs that side
effect on **every path** that performs the transition, not just the one you had
in mind. Caught only because the spec named that transition explicitly and I
wrote a test for it.

**2. A test that conflated retry with fallback.**
`test_bad_request_is_never_retried` expected 1 call and got 2 — and the code
was right. Our config puts small AND mid on Groq with different models, so
after a `ProviderBadRequest` on one model, trying the other is legitimate
fallback. Fixed by disabling escalation in that test so it measures retry
alone.

**3. A Redis client leaked across event loops.** `test_cache.py` left its
connected client in a module global; a later module's app shutdown tried to
close it from a different loop and got `RuntimeError: Event loop is closed`.
**A redis.asyncio client is bound to the loop it was created on.**

**4. An ordering-dependent test.**
`test_stats_on_an_empty_window_returns_zeros` took only `db_path`, never ran
`init_db()`, and passed only when an earlier test happened to create the table.

> Mistakes 3 and 4 make this the **third and fourth** test-isolation bug in the
> project — after the provider fixture (Day 1) and the cache (Day 2). Every one
> had the same shape: **module-level state that one test populates and another
> silently inherits.** Four occurrences is a pattern, not bad luck: any module
> global in a test-touched path is a hazard by default.

---

## Stage 6 — Evaluation, dashboard, README

**Completion criterion:** the README results table has real measured numbers and
no placeholders.

**Status: partially met, and the gap is the point.**

### Built

```
eval/run_eval.py         routed vs baseline; refuses to run on mock data
dashboard/index.html     one static file, served at /dashboard
README.md                results rewritten -- zero TBD remaining
```

**113 tests passing.**

### The blocker never cleared

The Groq key returned 401 on day one and returns 401 today. Every provider
adapter is verified against synthetic responses; **none has ever spoken to a
live server.**

So cost and latency are still unmeasured, and the honest move was to say so in
the README rather than fill the table from mock data.

### The harness refuses to lie

`run_eval.py` will not run against mock providers unless `--simulated` is
passed, and prefixes every line of such a run with `SIMULATED`:

```
REFUSING TO RUN.

MODELMUX_MOCK_PROVIDERS=1 is set, so every provider is fake. This script
produces the numbers that go in the README, and arithmetic over invented token
counts is indistinguishable from a real measurement once it is in a table.
```

That refusal is the most important thing built today. Every other guard in this
project protects the user from a bug; this one protects the *result* from me.
A plausible-looking fake would defeat the project more thoroughly than having no
number at all, and the failure mode is silent — a table of numbers looks
identical whether or not anything was measured.

`eval/results/` is gitignored for the same reason: a SIMULATED run must never
sit in version control where it can be mistaken for evidence.

### The blind spot-check

Quality can be graded **today, with no API key**. `run_eval.py` exports pairs of
answers with which tier produced which **withheld from the grader** and written
to a separate `_KEY.json`.

Blinding is the whole point. A grader who knows "A is the cheap one" finds what
they expect to find, and the exercise becomes theatre.

### The D3 bias became measurable

`cost_if_large_usd` assumes the baseline would produce the same number of output
tokens. In `run_eval.py` the baseline **actually runs**, so the ratio can be
compared directly and the report states which direction the live savings figure
is wrong in. An assumption became a measurement.

### Scope cut, taken deliberately

SPEC wanted React + Vite + Recharts. Shipped: one 200-line HTML file, no build
step, served by FastAPI at `/dashboard`.

PLAN.md compressed nine spec-days into four, and the dashboard is the most
cuttable item in the project — **the numbers are the deliverable; the dashboard
only shows them off.** A build toolchain for four fetch calls would have cost a
day that Stage 6's real work needed. Recorded as D24 with what was given up.

### D23 exists because a pointer led nowhere

SPEC says *"Quality scoring is TBD — see DECISIONS.md for the approach."*
DECISIONS.md had no such entry. I had also started referencing "D23" from
`run_eval.py` and the README before writing it.

Rather than invent a decision the project owner never made, D23 records the
three candidate approaches, the recommendation, and the cost of leaving it open.
**An open decision documented as open is honest; a fabricated one is not.**

### Mistake

**A second single-sample latency test.** `test_lookup_is_within_budget` failed
at 40.8ms against a 30ms budget on a busy machine, while steady state is ~11ms
even at 10,000 entries.

I had already diagnosed and fixed exactly this for the classification budget
tests — median of several samples rather than one draw from a noisy
distribution — **and did not apply the same fix to the cache test at the same
time.** Fixing one instance of a class of bug and leaving its siblings is its
own small failure.

### Final state

| Stage | Criterion | State |
|---|---|---|
| 1 — Bare proxy | curl returns an answer AND a row in SQLite | ⚠️ row logging verified; no live answer |
| 2 — Naive routing | written misroute list | ✅ D12 |
| 3 — Classifier | accuracy for all modes | ✅ D15 |
| 4 — Cache | hit rate + justified threshold + failure case | ✅ D16 |
| 5 — Resilience | keeps serving with a provider down | ✅ D20 |
| 6 — Metrics/eval | README table with real numbers | 🟡 built; **numbers blocked on a key** |

Everything that can be measured without a working credential, is. Everything
that cannot, says so.

---

## Closing pass — the two things the spec required and nobody had built

Found by **auditing the spec's endpoint and error tables against `main.py`**,
not by anything failing. Both were security- or evidence-relevant, and both
were buildable without an API key.

```
  BUILT    /health            BUILT    /v1/stats
  BUILT    /health/providers  BUILT    /v1/requests
  MISSING  /v1/stream         <- cut deliberately (D24)
  MISSING  /v1/compare        <- GAP
  MISSING  rate limiting      <- GAP (SPEC 10), and no 429 path at all
```

### Built

```
app/ratelimit.py       token bucket, per caller, idle eviction
POST /v1/compare       routed vs baseline, measures the D3 bias
tests/test_ratelimit.py  17 tests
```

**130 tests passing.**

### Rate limiting protects money, not correctness

Every other limit in this project guards a result. This one guards the account:
one caller in a retry loop empties the budget in minutes, and no amount of
clever routing matters once the credit is gone.

**Token bucket, not fixed window.** A fixed window permits 60 requests at
11:59:59 and 60 more at 12:00:00 — double the intended rate at the worst
possible moment, and at a *predictable* instant that scheduled clients
synchronise on.

**Checked before classification and before the cache.** A rejected request must
cost nothing; checking after routing would still burn ~11ms of embedding per
rejection, which is most of what the limit exists to prevent.

**X-Forwarded-For deliberately ignored.** It is caller-controlled — honouring it
without a verified proxy would let anyone bypass the limit by inventing a
header. *A security control keyed on caller-supplied data is not a control: if
the attacker picks the key, they pick their own bucket.*

### /v1/compare turns the project's biggest caveat into a number

D3 has been the largest unquantified assumption since Stage 1:
`cost_if_large_usd` **assumes** the baseline emits the same number of output
tokens as the routed tier. Measuring it means paying for the expensive call —
the very thing the router avoids.

`/v1/compare` runs both, and reports `output_token_ratio`: the factor by which
the live savings figure is wrong, and in which direction.

The honest position moved from *"we know this number is biased and cannot say
by how much"* to *"here is how much."* **An assumption you have quantified is a
finding; the same assumption unquantified is a footnote nobody acts on.**

It is deliberately not cached and not logged as a normal request — it costs two
provider calls, and letting it into `/v1/stats` would corrupt the very numbers
it exists to audit.

### What this pass shows about the rest

Both gaps had been sitting there through four days of building, past 113
passing tests, because **tests verify what you wrote, not what you failed to
write.** The only thing that found them was reading the spec's own tables back
against the code, line by line.

Worth doing once per project, near the end, and it is not the same activity as
testing.

---

## Verification pass — the number that was wrong in our favour

Two things closed, both found by checking claims rather than by anything
failing.

### The large tier was priced at a previous generation's rate

It had carried `UNVERIFIED` since Stage 1. Checked against the Anthropic model
and pricing reference, and it was **wrong in two ways at once**:

```
was:  claude-sonnet-5   0.003 / 0.015 per 1K
is:   claude-opus-5     0.005 / 0.025 per 1K
```

`0.003 / 0.015` is Claude **Sonnet 4.6** pricing ($3/$15 per 1M) — a previous
generation. Sonnet 5 is $2/$10. So the figure matched neither the model named
in the config nor any current price: the spec's placeholder numbers had been
inherited and a guessed model name attached to them.

**This is the baseline every savings figure is measured against.** For a
representative 500-in / 300-out exchange:

```
large (old, wrong)   $0.006000   -> savings would read 97.9%
large (verified)     $0.010000   -> savings actually  98.7%
```

The error *understated* the gap — but a number that is wrong in your favour is
still wrong, and it was off by 40% on the baseline.

> **The lesson: a known-unverified number in a load-bearing position is a bug
> with a comment on it.** The flag was honest and it sat there for two weeks
> while every savings figure quietly depended on it. The comment does not make
> the number less wrong; it only makes it easier to find once somebody finally
> looks.

### The blind spot-check nothing could read

`run_eval.py` had exported a blind grading file since the day it was written,
and **no code could read it back.** Quality was ungradeable even by hand.

`eval/grade_quality.py` closes it: joins the graded file to its key, unblinds
in exactly one place, and reports routed / baseline / tie.

Three things it does deliberately:

- **Headlines "not worse", not "wins."** Routing is justified when the cheap
  answer is *as good*, not only when it beats the expensive one.
- **Refuses to let a small sample look like a measurement.** Below n=20 it says
  so, with what a single item is worth as a percentage.
- **Prints its own caveats every run** — not independent, and a pairwise
  preference rather than a quality score.

> **Half a mechanism is a gesture at the problem, not a solution to it.** The
> export looked like quality was handled. It was not, and nothing about the
> output would have revealed that.

### No LLM-as-judge, deliberately

It needs the `anthropic` SDK — not on SPEC section 3's approved list — and a
key this project has never had. **Adding a dependency is a conversation, not a
default taken while the owner is away.** The human path needs neither, which is
why D23 recommended building it first.

---

## The day the key worked — first live measurement

**2026-09-21.** The project's oldest blocker was never a code problem and never
a key problem. It was one environment variable.

### The 401 that lasted three keys and several weeks

A newly created Groq key returned `401 Invalid API Key`. So had the two before
it. The working assumption each time was that the key was bad.

The Groq console said: **0 API Calls · Last Used: Never.**

> A rejected key still records an attempt. **Zero attempts means that key was
> never sent.** The evidence did not point at a bad key — it ruled the key out
> of the story entirely.

Commands run, and what they returned:

```powershell
[Environment]::GetEnvironmentVariable('GROQ_API_KEY','User')   # ...PxcK  <- the dead key
# .env held                                                    # ...yfoX  <- the new one
```

`load_dotenv()` **does not overwrite a variable already present in the
environment.** A `GROQ_API_KEY` had been set at Windows **User** scope at some
point — persistent across reboots and terminals, invisible from inside the
project folder. Every run read `.env`, saw the variable already set, and kept
the corpse.

Cleared with:

```powershell
[Environment]::SetEnvironmentVariable('GROQ_API_KEY',$null,'User')
```

**Stage 1's last open criterion — a live `200` — closed the same minute.**

### The fix was not removing the variable

That fixes today. So `.env` loading moved into `config.load_env()`, which
compares the two and **warns on stderr** whenever the environment shadows a
differing `.env` value, printing the last four characters of each and the exact
command that clears it.

> It prints the last four characters and **never the key.** Enough to tell two
> secrets apart, useless in a log. That rule was written after an actual leak
> earlier in this project: verify a secret by its *properties* — length,
> prefix, quoting, whitespace — never by printing it.

Recorded as **D29**.

### Then four more things broke, in order

**1. A 200 with an empty answer.** `openai/gpt-oss-*` are reasoning models:
they think in a separate `reasoning` field, billed as completion tokens, never
returned in `content`. With `max_tokens=20` the budget went entirely to
reasoning. A paid-for, successful, empty response.

`groq.py` now raises `ProviderServerError` instead of returning `""`.

> An empty string is indistinguishable from an answer to every layer above. It
> would be **cached**, **logged as a success**, and **counted as a cheap win**.
> The cheapest possible answer is the one that says nothing — a cost-optimising
> router that treats silence as success has its incentives pointing the wrong
> way. (**D32**)

**2. A cache hit from a ghost.** The first live `/v1/chat` came back
`status: cached`, 0 tokens, $0.00. Earlier `MODELMUX_MOCK_PROVIDERS=1` runs had
written **canned mock answers into the real Redis.** Flushed with
`redis-cli FLUSHDB` before every real run thereafter.

> The mock provider prints a loud banner. The cache it fills does not. A safety
> mechanism that announces itself at write time and stays silent at read time
> is only half a mechanism.

**3. `KeyError: 'routing'` on the first hard prompt.** It classified `large` →
anthropic → no key → no response. The held-out set sends **16 of 32** there, so
the eval could not finish at all.

The large tier now lists **two** providers — anthropic first, `gpt-oss-120b`
second. The provider *list* (D4, SPEC §7) had existed since Stage 2 and had
never held more than one entry. (**D30**)

**4. `no successful pairs (32 attempted)`.** `run_eval.py` imports `config`,
never `main` — and `.env` loading lived in `main`. **The eval harness had never
loaded environment variables in its life.** Fixed by D29's move; all four
`eval/` entry points now call `config_module.load_env()`.

### The mistake that matters most: I published wrong numbers

The first completed run reported **$11.51 routed / $17.97 baseline per 1k**.
Both were wrong by roughly **40x**.

`run_eval.py` priced by **tier** — `providers[0]`, which is Anthropic — while
every single call was answered by Groq via fallback. It was pricing
`gpt-oss-120b` output at **Claude Opus 5 rates**.

The true figures: **$0.4121 / $0.4411**.

**What caught it:** the projection computed afterwards came out *identical* to
the measurement. Two numbers that differ by construction cannot agree exactly.

> The savings **percentage** was barely affected — both columns were inflated
> by nearly the same factor — so nothing about the headline looked wrong. **A
> ratio can survive a bug that destroys both of its terms.**

> This project is a claim about cost. **A bug in the thing that measures cost
> is worse than a bug in the thing that spends it.** A routing bug shows up as
> a bad answer; a pricing bug shows up as a confident number in a README that
> nobody re-derives.

`run_eval.py` already refused to run against mock providers without
`--simulated`, on the principle that a plausible fake beats no number. It then
produced a plausible fake out of *real* data, through a path nobody guarded.

Fixed with `config.cost_for_provider(tier, provider_name, ...)`, which prices
by the provider that actually answered. Server and eval now share it.
(**D31**)

### What the first live run actually measured

32 prompts, 0 failures, total spend **$0.027**.

| | ModelMux | baseline | |
|---|---|---|---|
| p50 latency | **2,803 ms** | 6,973 ms | **2.5x faster** |
| p95 latency | 9,037 ms | 9,019 ms | unchanged |
| cost / 1k measured | $0.4121 | $0.4411 | 6.6% |
| cost / 1k projected | $11.49 | $18.25 | 37.1% |

> **A router's savings are bounded by the price spread it is given.** With one
> key, `large` *is* `gpt-oss-120b` (D30) — the same model as `mid`. The router
> sent 16 of 32 prompts there, where routed and baseline are byte-for-byte the
> same call. Half the traffic had nothing to save; the rest had a 2x spread to
> save from. Perfect classification earns nothing on a flat ladder.

> **The honest headline from the first live run is speed, and speed was never
> the goal.** 2.5x at the median, from real calls, with no repricing and no
> assumptions. p95 is unchanged because the tail is the hard prompts, which
> route large either way — routing improves the *typical* request and leaves
> the worst case alone.

**D3's assumption, finally a number:** the baseline emitted **1.04x** the
routed tier's output tokens (22,713 vs 21,844). Approximately unbiased; the
savings figure understates by about 4%. (**D33**)

### Adding a provider broke a test, and the test was right

`test_provider_failure_still_writes_a_row` began failing. The row was still
written, the status was still `error` — but the message had become:

```
all providers failed; last error: circuit open -- skipped
```

Both statements true; only one useful. `dispatch()` reported `trail[-1].error`,
and with a longer chain the last entry is always a skip *after* the breaker
tripped — the symptom, recorded once the cause had scrolled past.

`Attempt` gained a `skipped` flag; the reported error is now the last provider
actually **called**. A boolean, not a match on the message string, because the
string is for humans and should stay free to change.

> The tempting fix was to relax the assertion — the contract held, after all.
> But it was testing something real: **that a failure explains itself.** A row
> recording the wrong reason is a quieter version of dropping it. (**D34**)

### One failure left open, with the wrong guess recorded too

`test_classification_is_within_budget` intermittently fails in the full suite
at a median of **85ms** against a 20ms budget — while passing at 11-12ms alone.

Not sampling noise: every one of the seven samples was 5-11x the isolated
figure. Not the HTTP path (it reads the server's own `classify_ms`). Not test
ordering. Not this session's changes.

I guessed torch thread contention and wrote a probe to measure it. **The probe
was wrong** — it generated load with pure-Python busy loops, which contend on
the GIL rather than on torch's thread pool.

It ran long enough that I assumed it had hung and killed it. One of its two
runs had actually finished:

```
idle          median   11.2 ms
under load    median 76105.1 ms
```

**76 seconds is not a finding.** Torch pinned to one thread, every core running
a GIL-bound loop — the measured thread was starved of the interpreter lock.
That number describes my load generator. The idle 11.2ms, measured outside
pytest entirely, does independently confirm the 11-12ms figure.

> **So the cause is recorded as unknown.** Writing the untested hypothesis into
> the project as the explanation would be the same category of error as the
> pricing bug above: a confident claim nobody re-derived. (**D35**)

> And a second lesson from the same hour: **a failed experiment mistaken for a
> result is worse than no experiment.** "76 seconds under load" would read as a
> dramatic finding to anyone who did not know how it was produced — and I
> nearly recorded the run as having produced nothing at all, which was equally
> untrue.

### Files touched

| File | Change |
|---|---|
| `app/config.py` | `load_env()` + shadow warning; `cost_for_provider()` |
| `app/main.py` | uses `load_env()`; prices per provider |
| `app/providers/groq.py` | empty-content guard |
| `app/resilience.py` | `Attempt.skipped`; root-cause error selection |
| `config.yaml` | large tier: anthropic + groq fallback |
| `eval/*.py` | all four entry points load the environment |
| `tests/test_resilience.py` | regression test for the reported error |
| `README.md` | measured results replace "Not measured" |

**131 tests** (130 + 1 added). The suite goes green except for D35's open flake.

### The lesson worth keeping

> Four of the five bugs found today were in the **measurement** path, not the
> product: no environment in the eval harness, wrong prices in the eval
> harness, a cache full of fakes, and a test whose timing nobody trusts. The
> router itself was fine.
>
> **The instrument gets less scrutiny than the thing it measures, and it is the
> instrument that decides what you believe.**

### Postscript: `learnings/` vanished from disk, minutes after the commit

Immediately after committing, `learnings/` was gone. `git status` showed all
**19 files deleted**, and an empty directory named `learning/` — singular —
had appeared in its place, timestamped 19:25.

Nothing in this session renamed it. The project folder is under **OneDrive**,
and the most likely explanation is a sync-side rename or a partially-applied
folder operation.

Recovery was one command, because the work had been committed minutes earlier:

```bash
git checkout -- learnings/      # 19 files back, this session's edits intact
```

> **This is the argument for committing at the end of a unit of work, stated as
> an incident rather than a principle.** Had the same thing happened fifteen
> minutes earlier, the entire session's documentation — the timeline, the
> index, two extended learnings files — would have existed only on a disk that
> had just stopped holding it.

It is also the second time OneDrive has shaped a decision here. **D7** moved
the SQLite database to `%LOCALAPPDATA%` because OneDrive syncing a locked
database file can corrupt it, and SPEC 13 says regenerable binaries do not
belong in a synced folder. Source files were assumed to be the safe case.

The empty `learning/` directory was left in place rather than deleted: it is
untracked and harmless, and deleting things in a folder that had just lost 19
files unasked is not the moment to start guessing.
