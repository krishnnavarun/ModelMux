# 07 — War stories (the highest-value file in this folder)

> *"Tell me about a bug you're proud of finding."*
> *"Tell me about a time you were wrong."*
> *"Walk me through something that went wrong on a project."*

These questions decide interviews. **A story about a real bug with a real
diagnosis beats any amount of textbook knowledge.**

Each story below is in **STAR** form — Situation, Task, Action, Result —
followed by *the line that lands*.

---

## Story 1 — The 401 that lasted weeks and was never the key ⭐

**Use for:** debugging methodology, persistence, reading evidence correctly.
**This is your best story.**

**Situation.** Every API key the project ever had returned `401 Invalid API
Key` — three different keys across several weeks. It blocked the one thing that
needed a live server: the cost and latency measurements.

**Task.** Each time, the conclusion was "the key is bad" — regenerate and try
again. It kept not working, so that conclusion had to be wrong.

**Action.** I looked at the provider console instead of the error. It showed:

> **0 API Calls · Last Used: Never**

That reframed everything. **A rejected key still records an attempt.** Zero
attempts means the key was never *sent* — so the problem wasn't the credential
at all, it was what the process actually read.

Checking the environment rather than the file:

```powershell
[Environment]::GetEnvironmentVariable('GROQ_API_KEY','User')   # ...PxcK ← dead key
# .env contained                                                # ...yfoX ← the new one
```

**`load_dotenv()` does not overwrite a variable already set in the
environment.** A Windows **User-scope** variable had been set months earlier —
permanent, surviving reboots and every new terminal, and completely invisible
from inside the project folder. Every run loaded `.env`, saw the variable
present, and kept the corpse.

**Result.** Removing it produced a live `200` immediately, closing a criterion
that had been open since Stage 1. But deleting a variable only fixes *today* —
so `.env` loading moved into `config.load_env()`, which now compares the two
and **warns loudly** whenever the environment shadows a differing `.env` value.

It prints the **last four characters** of each — never the key. That rule came
from an earlier leak on this same project: **verify a secret by its properties
— length, prefix, quoting, whitespace — never by printing it.**

Moving it also fixed a second bug nobody had connected: the eval scripts import
`config` but never `main`, where loading used to live — so **the entire
measurement harness had never loaded environment variables in its life.**

> **The line that lands:** *"I'd been reading the error for what it suggested
> instead of what it ruled out. '401' suggests a bad key. '0 API calls' rules
> the key out of the story entirely — and that's a much smaller place to
> search."*

### Postscript — the warning caught it again, weeks later

Running the classifier eval later, the warning fired:

```
[env] WARNING: GROQ_API_KEY is set in the environment AND in .env, and they differ.
[env]   environment wins: ...PxcK   (.env has ...yfoX)
```

Checking the scopes: **User `NOT SET`, Machine `NOT SET`, Process `...PxcK`.**

The permanent fix had held — but the *terminal session* had been started
**before** the variable was deleted, so it still carried the old value in its
own process environment, and every child process it spawned inherited it.

> **Removing a persistent environment variable does not clean up processes
> that already read it.** Environment is copied to a child at spawn time, not
> shared. Existing shells, running servers and IDE terminals keep the old value
> until they restart — which is a genuinely confusing way for a "fixed" bug to
> come back.

**And this is the mitigation justifying itself.** Without the warning this
would have been another silent 401 and another hour spent on the wrong
hypothesis. With it, the diagnosis took one command.

---

## Story 2 — I published numbers that were wrong by 40× ⭐

**Use for:** intellectual honesty, measurement rigour, catching your own
mistakes. **Use this one when asked about a mistake.**

**Situation.** After the key finally worked, the first evaluation run produced
the project's first real cost figures: **$11.51 routed vs $17.97 baseline** per
1,000 requests. I reported them.

**Task.** Then I computed what the *configured* production ladder would give,
as a separate projection.

**Action.** The projection came out **identical to the measurement**. Two
numbers that differ by construction cannot agree exactly — that's not a
coincidence, it's a shared bug.

`run_eval.py` priced by **tier**, taking `providers[0]` — Anthropic — while
every single call had actually been answered by **Groq** via fallback. It was
pricing `gpt-oss-120b` output at **Claude Opus 5 rates**, roughly 40× too high.

The real figures: **$0.4121 / $0.4411**.

**Result.** Fixed with `config.cost_for_provider()`, which prices by the
provider that actually answered. Server and eval now share it, so they're
consistent **by construction** rather than by assertion.

> **The line that lands:** *"The savings **percentage** barely moved, because
> both columns were inflated by nearly the same factor. A ratio can survive a
> bug that destroys both of its terms — which is exactly why the headline
> looked fine. This project is a claim about cost, so a bug in the thing that
> measures cost is worse than a bug in the thing that spends it."*

**The follow-up they'll ask:** *"How would you prevent it?"*
> *"Sharing the function makes them consistent by construction, which is
> stronger than a test but weaker than both. There's still no test asserting
> that eval pricing matches server pricing, and I've written that down as
> outstanding rather than pretending it's covered."*

---

## Story 3 — HTTP 200 with an empty answer

**Use for:** API design, understanding a domain deeply, incentive design.

**Situation.** The very first live call returned **HTTP 200** — and `content:
""`.

**Task.** Work out whether that was a bug in my code or in my understanding.

**Action.** It was my understanding. `openai/gpt-oss-*` are **reasoning
models**: they generate an internal chain of thought in a separate `reasoning`
field, which is **billed as output tokens** but never returned in `content`.
`max_tokens` was 20 — the entire budget went to reasoning, leaving nothing for
the answer. A valid, fully-paid-for, "successful" empty response.

**Result.** The adapter now raises `ProviderServerError` naming the likely
cause, instead of returning `""`.

> **The line that lands:** *"Returning the empty string would have been
> catastrophically quiet. It's indistinguishable from a real answer to every
> layer above — it would have been **cached**, poisoning that prompt for 24
> hours, **logged as a success**, and **counted in the savings figure as a
> cheap win**. The cheapest possible answer is the one that says nothing, so a
> cost-optimising router that treats silence as success has its incentives
> pointing exactly the wrong way."*

Raising instead routes it into machinery that already existed: retry, fallback,
then a logged failure row. **Nothing silently dropped.**

---

## Story 4 — No cache threshold was safe

**Use for:** knowing when a parameter can't solve a problem; ML intuition.

**Situation.** The semantic cache needed a similarity threshold. Too loose and
different prompts share an answer **across users**; too tight and the cache
earns nothing.

**Task.** Find the right number — and justify it, not guess it.

**Action.** Built `tune_cache_threshold.py` to sweep it against 35 labelled
pairs: 15 genuine rephrasings, 20 deliberately confusable.

The sweep didn't find a safe value. **A pair at 0.99 cosine similarity had
opposite correct answers.**

**Result.** The answer wasn't a better number — it was a **second mechanism**.
A lexical inversion guard catches antonym and negation flips the embedding
misses, plus a length-ratio check and a candidate cap. The shipped threshold is
0.88, measured, with the failure documented rather than hidden.

There's also a test that **deliberately produces a wrong answer** at a loose
threshold, so the failure mode is demonstrated rather than described.

> **The line that lands:** *"Embedding similarity measures topical closeness,
> not semantic equivalence — negation barely moves a vector. Once I understood
> that, I stopped looking for a threshold that would fix it, because no number
> on that axis can separate 'X is true' from 'X is not true'."*

---

## Story 5 — The connection that leaked because the API looked finished

**Use for:** depth, reading past the intuitive API.

**Situation.** A Windows temp file wouldn't delete during test teardown.

**Action.** SQLite still had it open:

```python
with sqlite3.connect(path) as conn:
    conn.execute(...)
# commits on exit — and leaves the connection OPEN
```

`sqlite3`'s context manager is a **transaction** manager, not a resource
manager. It commits; it doesn't close.

**Result.** Explicit `close()` in a `finally`.

> **The line that lands:** *"On Linux this leak is invisible until you run out
> of file descriptors. On Windows it fails immediately, because the OS won't
> let you delete an open file. The platform I'd been treating as the awkward
> one is the one that caught it."*

---

## Story 6 — I recommended a rebuild, then checked, then reversed it

**Use for:** "tell me about being wrong"; verification over confidence.

**Situation.** The project ran on Python 3.13. `torch` and
`sentence-transformers` were needed, and my instinct was that 3.13 was too new
— I recommended rebuilding the environment on 3.11.

**Action.** Before acting, I checked PyPI. `torch` publishes a `cp313
win_amd64` wheel; `sentence-transformers` is pure Python.

**Result.** Reversed the recommendation before anyone spent an hour on it. No
rebuild.

> **The line that lands:** *"The instinct was reasonable and wrong, and it cost
> nothing because I checked before acting rather than after. I wrote it down as
> a decision — including the fact that I'd initially got it backwards — because
> a decisions log that only records the correct calls isn't a decisions log."*

**Related, same shape:** the virtualenv was moved out of the OneDrive folder,
originally justified by 60-80 second cold imports blamed on sync. **That
diagnosis was wrong** — OneDrive wasn't even running, and imports dropped to
~8s on the third run purely from OS file caching. The move was still right (1.1
GB of regenerable binaries don't belong in a synced folder) but **it was
hygiene, not a performance fix**, and the correction is recorded next to the
original claim.

---

## Story 7 — Adding a provider broke a test, and the test was right

**Use for:** observability, listening to tests.

**Situation.** Adding a second provider to the large tier broke
`test_provider_failure_still_writes_a_row`. The row was still written and the
status was still `error` — only the *message* had changed:

```
all providers failed; last error: circuit open -- skipped
```

**Task.** Decide whether to relax the assertion or fix the code.

**Action.** Both statements were true; only one was useful. `dispatch()`
reported `trail[-1].error`, and with a longer provider chain the last entry is
always a **skip** — recorded *after* the real cause had scrolled past. An
operator reading that row learns that ModelMux declined to make a call. They
don't learn that the provider was returning 500s.

**Result.** `Attempt` gained a `skipped` flag; the reported error is now the
last provider **actually called**. A boolean rather than matching the message
string, because the string is for humans and should stay free to change.

> **The line that lands:** *"The tempting fix was to relax the assertion — the
> contract held, the row existed. But the assertion was testing something real:
> that a failure explains itself. A row that records the wrong reason is a
> quieter version of dropping it."*

---

## Story 8 — A failed experiment I nearly recorded as a result

**Use for:** scientific honesty; knowing what your own data can't support.

**Situation.** A performance test intermittently failed in the full suite —
reporting ~85ms against a 20ms budget, while passing at 11-12ms in isolation.
Not noise: every sample was 5-11× the isolated figure.

**Task.** Find the cause.

**Action.** I hypothesised CPU contention in torch's thread pool and wrote a
probe to measure it. It ran long, I assumed it had hung, and killed it.

It had actually **finished**, and reported:

```
idle          median   11.2 ms
under load    median 76105.1 ms
```

**76 seconds.** Which is nonsense — my load generator used pure-Python busy
loops that contend on the **GIL**, not on torch's thread pool. With torch
pinned to one thread and every core spinning on the interpreter lock, the
measured thread was simply starved. **That number described my load generator,
not the classifier.**

**Result.** The cause is recorded as **unknown**. The hypothesis is written
down as untested, the invalid measurement is written down as invalid, and the
correct experiment is described for whoever picks it up.

> **The line that lands:** *"'76 seconds under load' would have read as a
> dramatic finding to anyone who didn't know how it was produced. A failed
> experiment mistaken for a result is worse than no experiment — so it's in
> the log as a bad experiment, and the 20ms test is still failing rather than
> quietly retuned until it went green."*

---

## Story 9 — Half a mechanism that looked like a finished one

**Use for:** completeness; the gap between building and delivering.

**Situation.** `run_eval.py` had exported a blind quality-grading file since
the day it was written. Blind grading is the right methodology — which tier
produced which answer is withheld from the grader and written to a separate key
file.

**Action.** **Nothing could read it back.** There was no tool to join the
graded file to its key and produce a result. Quality was ungradeable even by
hand, and nothing about the output revealed that.

**Result.** `eval/grade_quality.py` — unblinds in exactly one place, reports
routed/baseline/tie, headlines **"not worse"** rather than "wins" (routing is
justified when the cheap answer is *as good*), refuses to let a sample below
n=20 look like a measurement, and prints its own caveats every run.

> **The line that lands:** *"The export looked like quality was handled. Half
> a mechanism is a gesture at the problem, not a solution to it — and it had
> been sitting there for weeks looking complete."*

---

## The meta-story: where the bugs actually were

**Use this to close, or when asked what you learned overall.**

> *"Counting them up at the end: most of the serious bugs weren't in the
> router. They were in the things that **measure** the router — the eval
> harness with no environment loaded, the eval harness pricing every call
> wrong, a cache full of mock answers contaminating a real run, and a timing
> test nobody could trust.*
>
> *The product code was comparatively fine. **The instrument gets less scrutiny
> than the thing it measures, and it's the instrument that decides what you
> believe.**"*

---

## Quick index by question

| If they ask… | Tell story |
|---|---|
| "A bug you're proud of finding" | **1** (the 401) |
| "A time you were wrong" | **2** (the 40× numbers) or **6** (Python 3.13) |
| "A time you disagreed with a spec" | D24 — cutting React (`06`) |
| "How do you debug?" | **1** — evidence that rules out, not suggests |
| "Tell me about testing" | **7** (test was right) or `04` (mock provider) |
| "Something about API design" | **3** (empty 200) |
| "A time a parameter couldn't fix it" | **4** (cache threshold) |
| "How do you handle uncertainty?" | **8** (the bad experiment) |
| "Something you shipped incomplete" | **9** (half a mechanism), D23 (quality) |
