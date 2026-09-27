# Decisions

Choices made that SPEC.md did not specify, or that amend it. Each one is here
because I have to be able to defend it.

Format: what was decided, what the alternatives were, why this one, and what it
costs.

---

## D1 — Prompt length limits are enforced in the endpoint, not by pydantic

**Date:** 2026-09-08 · **Stage:** 1 · **Amends:** SPEC section 5

SPEC requires `400` for an empty or over-length prompt. But a pydantic
constraint (`Field(max_length=...)`) fails inside FastAPI's validation layer,
which runs *before* our handler and always returns **422**.

The two cannot both be satisfied by a pydantic constraint, so `ChatRequest`
declares `prompt: str` with no length rules, and `main.py` checks emptiness and
`config.limits.max_prompt_chars` explicitly.

**Why this way:** the status code is part of the published contract, and 400 is
the correct semantic. More importantly, a pydantic rejection never reaches our
code, so it could never write a database row — and SPEC section 2 says nothing
is silently dropped. Enforcing in the handler is what makes the row possible.

**Cost:** two hand-written checks that a framework could have done, and the
limit now lives in config.yaml rather than next to the field it constrains.

---

## D2 — Provider failures always return 502, never 400

**Date:** 2026-09-08 · **Stage:** 1 · **Clarifies:** SPEC sections 5, 8.6

The error taxonomy in `providers/base.py` has a `ProviderBadRequest` type. It is
tempting to map it to HTTP 400. That is wrong.

`ProviderBadRequest` fires when *our* API key is invalid or *our* configured
model name is unknown. The caller's prompt was fine. Returning 400 would blame
the caller for our misconfiguration and send them debugging a request that was
never the problem.

**Decision:** every `ProviderError` returns 502. 400 is reserved for the two
cases SPEC section 5 lists — empty and over-length prompts.

The four-way taxonomy still earns its place: it governs **retry policy** in
Stage 5, not the status code. `ProviderBadRequest` is the one that must never be
retried, because it fails identically every time.

**I got this wrong first.** The initial implementation mapped it to 400. Caught
when the dead Groq key produced a 400 that implied the caller had sent a bad
prompt.

---

## D3 — `cost_if_large_usd` reuses observed token counts (known bias)

**Date:** 2026-09-08 · **Stage:** 1 · **Amends:** SPEC section 6

SPEC says `cost_if_large_usd` is "computed at request time from the large tier's
configured price." Implemented literally, that means: take the token counts we
actually observed from the small model, and reprice them at large-tier rates.

**This is not a true counterfactual.** A larger model asked the same question
usually produces a *different* number of output tokens — often more. The honest
description of this figure is "same tokens, baseline prices," not "what the
large tier would have cost."

**Why keep it:** the alternative is calling the large model on every request to
find out, which destroys the entire purpose of the project. The approximation is
the only cheap option.

**What it costs:** the headline savings number is biased, and the direction
depends on relative verbosity — it *understates* savings if large models answer
at greater length, which is the common case. Every savings claim in the README
must carry this caveat.

**Mitigation available:** `POST /v1/compare` (SPEC section 5) runs both for
real. Stage 6 should use it on a sample of the eval set to measure the size of
this bias, and report that alongside the headline figure.

---

## D4 — Each tier's providers are a list from day one

**Date:** 2026-09-08 · **Stage:** 1 · **Follows:** SPEC section 7

Even with one provider per tier, `config.yaml` stores a list and
`config.provider_for()` returns `providers[0]`.

**Why:** within-tier fallback (Stage 5) needs a list. Restructuring config,
every reader of it, and the router later is strictly more work than typing four
extra characters now. "First in list" is a complete selection strategy until
health tracking exists.

---

## D5 — `escalate_on_tier_exhausted` is a config switch, not hardcoded

**Date:** 2026-09-08 · **Stage:** 1 · **Amends:** SPEC section 8.7

SPEC says: when a tier's providers are exhausted, "escalate one tier up rather
than failing outright."

That is right for quality, but it has a failure mode worth being able to turn
off: during a Groq outage, *every* small-tier request escalates to the large
tier. An availability incident silently becomes a bill. With no per-user budget
cap yet (that's on the roadmap, not built), nothing bounds it.

**Decision:** the behaviour is SPEC's default (`true`), but it is a config flag
so it can be turned off during an incident without a code change.

---

## D6 — Cost counts input and output tokens separately

**Date:** 2026-09-08 · **Stage:** 1 · **Amends:** SPEC section 6

The original Stage 1 code priced input tokens only. Output tokens are billed at
a different and much higher rate — 5x on the large tier in our own config.

Pricing input alone understates real spend, and understates it *unevenly*
across tiers, which would corrupt the one comparison the whole project exists to
make. `config.cost_usd()` now takes both.

---

## D7 — The SQLite database lives outside the project folder

**Date:** 2026-09-08 · **Stage:** 1 · **Amends:** SPEC section 6, 13

The project sits at `C:\Users\krish\OneDrive\Documents\ModelMux`. OneDrive
syncing a SQLite file while SQLite holds a lock on it can corrupt the database,
and WAL mode adds two companion files (`-wal`, `-shm`) that must stay consistent
with the main file. A sync process copying those three independently is exactly
the failure mode.

**Alternatives considered:**

1. Move the whole project out of OneDrive — correct, but disruptive: it breaks
   the IDE's open files and any absolute paths, and it is the user's call, not
   mine.
2. Exclude the folder from OneDrive sync — not scriptable reliably.
3. **Move only the database.** Chosen.

`config.yaml` now points at `${LOCALAPPDATA}/ModelMux/modelmux.db`, expanded by
`config.py`. LOCALAPPDATA is never synced by OneDrive.

**Cost:** the database is no longer next to the code, which is mildly surprising
when you go looking for it. `MODELMUX_DB_PATH` overrides it, and the tests use
that to redirect to a temp file.

**Note:** `.gitignore` prevents git from committing the `.db` files. It does
nothing to stop OneDrive syncing them. Two different systems, two different
ignore mechanisms — a distinction worth remembering.

---

## D8 — Staying on Python 3.13 (reversing an earlier recommendation)

**Date:** 2026-09-08 · **Stage:** 1 · **Amends:** SPEC section 3

I previously recommended rebuilding the venv on Python 3.11, reasoning that
Stage 3 needs `sentence-transformers` → PyTorch, and that the newest Python
often lacks wheels — forcing a source build that reliably fails on Windows.

**That reasoning was sound in general and wrong here.** Checked against PyPI:

```
torch 2.14.0                 torch-2.14.0-cp313-cp313-win_amd64.whl   EXISTS
sentence-transformers 6.0.1  pure-Python wheel (-none-any.whl)        EXISTS
```

**Decision: stay on 3.13.** No rebuild.

**The lesson, which is the reason this entry exists:** I nearly had the user
download and install a second Python and rebuild their environment on the
strength of a plausible generalisation. One API call to PyPI settled it in
seconds. Verify version-compatibility claims against the index before acting on
them — "newest Python breaks ML libraries" is a real pattern, not a fact about
any particular week.

---

## D9 — Redis will come from WSL2

**Date:** 2026-09-08 · **Stage:** 1 (decided early) · **Amends:** SPEC section 3

Redis has no official native Windows build, which the spec did not mention and
which hard-blocks Stage 4. Checked what is actually on this machine:

- Docker Desktop: **not installed** (a large install, and heavy to run)
- WSL2 with Ubuntu: **already installed**

**Decision: WSL2.** It is already there, so the marginal cost is one apt
install. WSL2 forwards localhost, so `redis://localhost:6379` works unchanged
from the Windows-side Python process.

```bash
wsl -e sudo apt install redis-server
wsl -e redis-server --daemonize yes
```

Not run yet — `sudo` needs an interactive password, and nothing needs Redis
until Stage 4.

**Cost:** WSL must be running for the cache to work, which is a silent
dependency a new contributor will not guess. Must be in the README before
Stage 4 ships.

---

## D10 — A mock provider, added during Stage 1 rather than Stage 5

**Date:** 2026-09-08 · **Stage:** 1 · **Follows:** SPEC section 8.6

SPEC introduces `providers/mock.py` for load testing at Stage 6. It was built
now instead, because the Groq key is invalid and Stage 1 could not otherwise be
verified at all.

With the mock, the full request path, the cost arithmetic, the error mapping and
the database logging are all provable without a network call — 36 tests, no API
key. What remains blocked is only the live call itself.

**This is the adapter boundary paying for itself immediately.** The endpoint
cannot tell the mock from the real thing, because it only ever sees
`Provider.complete()`. Swapping one for the other in a test fixture is a single
line:

```python
test_client.app.state.providers["groq"] = mock_provider
```

**Cost:** a passing suite against a mock proves our logic, not that Groq behaves
as we assume. The 401 remains genuinely unverified ground.

---

## D11 — The naive rule normalises tokens into a 0-1 score

**Date:** 2026-09-08 · **Stage:** 2 · **Follows:** SPEC section 8.4, 11

SPEC Stage 2 says "route on token count alone." Two ways to implement that:

1. Token thresholds directly in config (`small_max_tokens: 198`)
2. **Normalise the token count into a 0-1 score, then reuse the existing
   `classifier.thresholds`.** Chosen.

`score = min(token_count / saturation_tokens, 1.0)`, with
`saturation_tokens: 600`.

**Why:** option 1 works today and must be torn out at Stage 3, when the
heuristic scorer starts producing a real 0-1 score. Option 2 means Stage 3
replaces *how the score is computed* and touches nothing else — not the
thresholds, not the config shape, not `select_tier()`'s signature.

**Why saturation at all:** past a certain length, longer tells you nothing
extra. A 2,000-token prompt and a 20,000-token one are both simply "long".
Without a ceiling, length would dominate any future weighted blend of signals
purely by having an unbounded range.

**Cost:** one more concept (`saturation_tokens`) than strictly needed for a
rule we intend to delete. Worth it for the stable interface.

---

## D12 — Measured misroutes of the naive router

**Date:** 2026-09-08 · **Stage:** 2 · **This is Stage 2's completion criterion
(SPEC section 11).**

50 hand-labelled prompts in `eval/prompts.json`, run through the naive router
via `eval/run_routing_check.py`. `expected_tier` is my judgement of the cheapest
tier that could answer acceptably, labelled before running anything.

### Headline

| Outcome | Count | |
|---|---|---|
| Correct | 17 | 34% |
| **Routed too cheap** | **28** | **56%** — quality risk |
| Routed too expensive | 5 | 10% — cost waste |

### The failure is asymmetric, and in the wrong direction

SPEC 8.4 rule 3 says to fail *towards quality*: "a wrong cheap answer costs more
in trust than a wrong expensive one costs in money."

The naive rule does the exact opposite. It fails towards cost, 28 times to 5 —
a 5.6:1 ratio in the dangerous direction. It does not merely route badly; it
routes badly in precisely the way the spec says is least acceptable.

### By category

| Category | Correct | Note |
|---|---|---|
| lookup | 6/6 | Short and genuinely easy — length and difficulty agree |
| trivial_math | 3/3 | Same |
| code_simple | 4/4 | Same |
| formatting | 2/2 | Same |
| creative | 2/3 | |
| **reasoning** | **0/5** | |
| **summarize** | **0/2** | |
| **multi_question** | **0/2** | |
| **code_mid** | **0/2** | |
| **long_and_hard** | **0/5** | |
| **trap_long_but_easy** | **0/5** | |
| **trap_short_but_hard** | **0/11** | |

The pattern is stark: the rule scores 15/15 where length and difficulty happen
to point the same way, and **0/32** everywhere they diverge. It is not
approximating difficulty badly — it is measuring a different quantity that
correlates with difficulty only in easy cases.

### The worst individual cases

```
#11  large -> small (  9 tk)  Prove Fermat's Last Theorem.
#15  large -> small (  8 tk)  Why does P versus NP remain unresolved?
#50  large -> small ( 11 tk)  Is mathematics discovered or invented?
#36  large -> small (194 tk)  Multi-tenant SaaS data isolation trade-offs
#31  small -> mid   (340 tk)  1,400-word diary entry -> "what day was it?"
```

### The unplanned finding: the large tier is unreachable

**Across all 50 prompts, `large` was selected zero times.**
Tier distribution: `{'small': 45, 'mid': 5}`.

Reaching `large` requires score > 0.66, i.e. > 396 tokens at saturation 600.
Nothing in a realistic prompt set is that long. So the configured large tier —
the tier every `cost_if_large_usd` figure is priced against — is dead code
under this rule.

This was not designed into the experiment. It fell out of running it, and it is
the strongest argument that length-based routing is not a weak version of the
right idea but the wrong axis entirely.

**Deliberately not fixed by retuning `saturation_tokens`.** Lowering it would
move prompts into `large` by length, which is still the wrong reason. Tuning a
rule we are about to replace would only obscure how badly it fails. Pinned as a
test (`test_large_tier_is_effectively_unreachable`).

### What Stage 3 must fix

Ranked by how much of the failure each would address:

1. **`reasoning_markers`** — "prove", "derive", "critique", "compare",
   "step by step", "why". Present in nearly every too-cheap case and absent
   from every correctly-routed lookup. Single highest-value signal.
2. **`is_lookup`** — short, starts with what/when/where/who. Protects the 15
   cases currently correct from being broken by the signals above.
3. **`multi_question`** — 0/2 today.
4. **`has_code`** — separates `code_simple` from `code_mid`.
5. **A question-to-context ratio.** All five `trap_long_but_easy` cases are a
   long pasted block plus one short trivial question. Total length is the wrong
   measure; the *question* is what needs answering. This signal is not in SPEC
   section 8.1 and should be added.

Item 5 is a genuine addition to the spec, discovered by running the experiment.

---

## D13 — Google and Anthropic adapters added together

**Date:** 2026-09-08 · **Stage:** 2 · **Amends:** SPEC section 11

SPEC Stage 2 says "add a second provider." Both Google (mid) and Anthropic
(large) were added.

**Why both:** with only two tiers implemented, a router with three configured
tiers can select one that cannot execute. The misroute experiment needs all
three reachable to mean anything — and `cost_if_large_usd`, the baseline for
every savings claim, is priced off the large tier.

**The boundary held.** Adding two providers with three genuinely different API
shapes required **no change to `main.py` or `router.py`**:

- Groq: OpenAI-compatible, bearer token
- Google: model in the URL path, `x-goog-api-key` header, `contents`/`parts`
  body, `usageMetadata` counts
- Anthropic: `x-api-key` plus a required `anthropic-version` header,
  `max_tokens` mandatory, `content` as a list of typed blocks

All three normalise into `ProviderResult` and the same four errors. The only
new shared code is `providers/__init__.py`, a registry mapping config names to
classes so `main.py` names no provider at all.

That was the real test of Stage 1's design, and it passed.

**Cost:** two adapters that cannot be exercised against a live API — there are
no Google or Anthropic keys. Both are unit-tested against synthetic responses,
so their error mapping and parsing are verified, but neither has spoken to a
real server. Flagged as unverified ground.

---

## Open — decisions still to make

- **Cache similarity threshold.** SPEC calls it the most dangerous setting in
  the project. Must be justified here with a documented failure case (Stage 4).
- **Whether the project itself moves off OneDrive.** The database no longer
  lives there (D7), so the corruption risk is handled. What remains is `.venv`
  sync churn — an annoyance, not a correctness problem. The user's call.
- **Classifier weights.** Stage 3. Must be a single readable dict, and every
  weight must be defensible against the D12 misroute list.
- **Question-to-context ratio as a signal.** Not in SPEC section 8.1; discovered
  by the D12 experiment. All five `trap_long_but_easy` failures are a long
  pasted block plus one short trivial question.

*(Python version → resolved in D8. Redis on Windows → resolved in D9. SQLite in
a synced folder → resolved in D7.)*

---

## D14 — Model names and prices verified against Groq's docs; small+mid both on Groq

**Date:** 2026-09-08 · **Stage:** 2 (correction) · **Amends:** config.yaml

Checked `config.yaml`'s guesses against
https://console.groq.com/docs/models. Three findings, in order of severity.

### 1. The configured model is not available on a developer plan

`llama-3.1-8b-instant` — and `llama-3.3-70b-versatile` — are both listed as
**Enterprise / Contact Sales**. No published price, no published rate limit.
A developer key cannot call them.

The whole small tier pointed at a model we could not have used.

### 2. The output price was 3.75x too low

Groq publishes per 1M tokens; our config is per 1K, so divide by 1000.

| Model | Published (per 1M) | Per 1K in | Per 1K out |
|---|---|---|---|
| `openai/gpt-oss-20b` | $0.075 / $0.30 | 0.000075 | 0.0003 |
| `openai/gpt-oss-120b` | $0.15 / $0.60 | 0.00015 | 0.0006 |

My placeholder was `0.00005 / 0.00008`. Input was in the right region; **output
was understated 3.75x** — and output is the larger share of a real bill.

This is the second time an output-token error has appeared in this project (see
D6, where output was ignored entirely). Output pricing is evidently the part I
get wrong; worth checking twice from here on.

### 3. Mid tier moved to Groq, temporarily

The intended mid tier is Google Gemini, but there is **no GOOGLE_API_KEY** — and
with mid unreachable, the router cannot be exercised end to end for real with
the one key we expect to have.

`openai/gpt-oss-120b` is **2x** the small tier, so tier selection still has
genuine, measurable cost consequences. The google entry is kept commented in
`config.yaml` immediately below it, to swap back when a key exists.

**Cost:** the multi-provider story is temporarily only exercised by the mock and
by unit tests. Adapters for Google and Anthropic still exist and are tested
against synthetic responses; neither has spoken to a live server.

### Resulting ladder

```
small  groq       openai/gpt-oss-20b     0.000075 / 0.0003
mid    groq       openai/gpt-oss-120b    0.00015  / 0.0006
large  anthropic  claude-sonnet-5        0.003    / 0.015     UNVERIFIED

500 in / 300 out:  small $0.000127   mid $0.000255   large $0.006000
                   large/small = 47x     mid/small = 2x
```

**The large tier remains entirely unverified** — no key, and prices never
checked against a pricing page. Every `cost_if_large_usd` figure, and therefore
every savings claim, rests on those three numbers. They must be verified before
any cost figure is published.

### A test got better as a side effect

`test_cost_uses_both_token_directions` hardcoded the old literal prices and
broke on this change — meaning it was testing the config file, not the
arithmetic. Rewritten to read rates from config and assert the real invariant:
that the computed cost strictly exceeds an input-only calculation.

---

## D15 — Stage 3 classifier: results, and why `hybrid` is the default

**Date:** 2026-09-08 · **Stage:** 3 · **This is Stage 3's completion criterion
(SPEC section 11): accuracy figures for heuristic, embedding, and hybrid.**

### Measured accuracy

| mode | tuned (n=50) | **held-out (n=32)** | too cheap | too expensive |
|---|---|---|---|---|
| naive_tokens (Stage 2) | 34% | **31%** | 20 | 2 |
| heuristic | 78% | **88%** | 4 | 0 |
| embedding | 64% | **72%** | 0 | 9 |
| hybrid | 68% | **72%** | **0** | 9 |

Held-out is the honest column. `prompts.json` became a training set the moment
weights were tuned against it.

### Finding 1 — the held-out score is HIGHER than the tuned score

A **negative** overfitting gap (-9% for heuristic). That is not evidence of
excellent generalisation; the more likely explanation is that **the two sets are
not equally hard.** `prompts.json` was built adversarially, stuffed with traps
designed to break length-based routing. `holdout.json` has a more ordinary mix.

So the gap does not cleanly measure overfitting, and I cannot claim it does.
What it does show is no *catastrophic* memorisation. A proper answer needs sets
of matched difficulty, ideally labelled by someone else.

### Finding 2 — hybrid is LESS accurate than the heuristic alone, and we ship it anyway

88% → 72% is a real accuracy loss. But look at the direction of the errors:

```
heuristic   4 too cheap,  0 too expensive
hybrid      0 too cheap,  9 too expensive
```

**Hybrid eliminates every quality risk on the held-out set.** It buys that by
over-routing 28% of prompts one tier.

SPEC 8.4 rule 3 is explicit: *a wrong cheap answer costs more in trust than a
wrong expensive one costs in money.* By the project's own stated values, zero
too-cheap misroutes is worth 16 points of accuracy.

**Decision: `mode: hybrid`.** This is a values judgement, not a technical one,
and it is one config line to reverse if the cost proves unacceptable in
production.

**It also demonstrates why accuracy alone is the wrong scoreboard.** A single
percentage would have ranked heuristic first and hidden the fact that it sends
hard prompts to weak models.

### Finding 3 — the embedding classifier never routes too cheap, and is blunt

0 too-cheap in both sets, 9-15 too-expensive. It over-escalates. That follows
from two deliberate choices: ties break upward, and low neighbour agreement
escalates. Both are the fail-towards-quality instinct, and both cost money.

### What made the heuristic work — in order of impact

1. **Graded markers.** `prove`/`derive`/`critique` (HARD) vs `compare`/`debug`
   (MEDIUM). Counting alone gave 0/11 on `trap_short_but_hard`.
2. **Floors instead of a pure average.** A weighted sum measures how MANY things
   look hard; a prompt is hard if ANY ONE strong signal fires. "Prove Fermat's
   Last Theorem" scored 0.18 under averaging because four near-zero components
   diluted the one that mattered.
3. **`floor + (1-floor) x weighted`, not `max(weighted, floor)`.** Blending
   keeps the floor's guarantee while letting length still count. +4 points.
4. **`question_context_ratio` scaling the length term.** Fixed
   `trap_long_but_easy` 0/5 → 4/5. Not in SPEC 8.1 — discovered by measurement.
5. **Signals measured on the QUESTION, not the whole prompt.** A pasted recipe
   or email thread contains "compare" and question marks that belong to the
   context. This bit twice: once for reasoning markers, once for
   `multi_question`.

### Latency

Classification p50 **11-12ms** against a 20ms budget, after embedding the
question rather than the whole prompt. Before that fix a long prompt cost
**55ms p50 / 84ms p95** — a genuine budget violation.

The fix was principled as well as fast: `all-MiniLM-L6-v2` truncates at 256
tokens, so a long prompt was already being clipped *at an arbitrary point*,
keeping the pasted context and discarding the trailing question. Embedding the
question makes the choice deliberate.

### Honest limitations

- **Both sets were written and labelled by one person** — the same person who
  built the classifier. Unconscious bias towards prompts the signals handle
  cannot be ruled out. Every accuracy number here is an upper bound.
- **n=32 held out.** A single prompt is 3 percentage points. These figures have
  wide error bars and should not be quoted to the percentage point.
- **The marker lists were extended while looking at failures.** Generic verbs
  (`explain`, `describe`, `summarise`) are defensible; the risk of having fitted
  to the eval set is real and is what the held-out set exists to bound.

---

## D16 — Cache similarity threshold: 0.88, and why a threshold alone is not enough

**Date:** 2026-09-10 · **Stage:** 4 · **This is Stage 4's completion criterion
(SPEC section 11): a measured hit rate and a justified threshold with its
failure case documented.**

SPEC 8.5 calls `similarity_threshold` "the most dangerous setting in this
project." It is now chosen by measurement — `eval/tune_cache_threshold.py`
against 15 equivalent pairs (should hit) and 20 dangerous pairs (must not).

### Finding 1 — the distributions overlap, so no threshold is clean

```
equivalent  min 0.7780  median 0.9185  max 0.9938
dangerous   min 0.3677  median 0.7416  max 0.9903
```

The most similar DANGEROUS pair scores **higher** than the least similar
EQUIVALENT pair. Every threshold trades hit rate against wrong answers.

### Finding 2 — with similarity alone, NO threshold is safe

| threshold | hit rate | false hits |
|---|---|---|
| 0.90 | 67% | 2 |
| 0.92 | 47% | 1 |
| 0.98 | **7%** | **1** |

Even at 0.98, where the cache has stopped being worth having, one dangerous
pair still hits:

```
0.9903  "Convert 32 Fahrenheit to Celsius."
     vs "Convert 32 Celsius to Fahrenheit."
```

Opposite operations. Answers 0 and 89.6. **0.99 similarity.**

**The cause is structural, not a tuning failure. Embeddings encode topic and
vocabulary, not logical direction.** Negation, antonyms and argument order are
exactly what they represent worst, because the two sentences share nearly every
token. No value of a single cosine threshold can separate them.

### Decision 1 — a second, lexical gate

`cache.is_inverted()` runs after the threshold and checks two things:

1. **Antonym swap** — one prompt says "encrypt", the other "decrypt", and
   neither says both.
2. **Exact positional exchange** — two words land in each other's slots while
   the rest of the sentence stays put. This is what catches Fahrenheit/Celsius,
   where an antonym list never could: both prompts contain *both* units.

A weaker "did any pair change relative order" rule was tried first and was too
blunt — it also blocked *"How do I reverse a string in Python?"* vs *"In
Python, how can I reverse a string?"*, where "python" merely migrates. That is
a rephrasing, and blocking it costs a legitimate hit. Requiring an **exact**
exchange separates the two cleanly.

With the guard:

| threshold | hit rate | false hits |
|---|---|---|
| 0.80 | 80% | 1 |
| **0.84** | **80%** | **0** — lowest safe |
| 0.88 | 67% | 0 |
| 0.90 | 53% | 0 |

Guard blocked 10/20 dangerous pairs outright, at a cost of 0/15 equivalent
pairs once narrowed.

### Decision 2 — the threshold is 0.88, not the lowest safe 0.84

The highest dangerous pair surviving the guard scores **0.8156**
(*"What is 15% of 240?"* vs *"What is 50% of 240?"*).

| threshold | margin above 0.8156 | hit rate |
|---|---|---|
| 0.84 | +0.024 | 80% |
| **0.88** | **+0.064** | **67%** |

Taking 0.84 because it is the lowest value with zero false hits **on my own 20
pairs** would be the Day 1 mistake repeated — fitting a constant to the test
set. 0.88 keeps a real margin for pairs I did not think of.

### Measured end to end

```
HIT RATE (rephrasings): 12/15 = 80%
FALSE HITS (dangerous):  0/20 =  0%
```

The 80% exceeds the 67% predicted by the sweep because `lookup()` checks up to
3 candidates above threshold, so a guard-rejected top match does not lose the
hit outright.

### The safety test

`tests/test_cache.py::test_loose_threshold_serves_a_wrong_answer` is required
by SPEC 8.5. At a deliberately reckless 0.75, asking *"What is the tallest
mountain in Asia?"* returns **"Mount Kilimanjaro."** — Africa's answer,
confidently, with nothing to indicate a substitution. The risk is executable,
not described.

`test_shipped_threshold_blocks_that_same_wrong_answer` shows 0.88 refusing it,
and `test_inversion_survives_a_high_similarity_score` covers the 0.99 case that
only the guard catches.

### Honest limitations

- **35 pairs, written by one person.** Zero false hits *here* is not zero false
  hits in production. Treat it as a lower bound on risk.
- **The guard is a patch on a fundamental limitation**, not a fix. It catches
  the two shapes I observed. Others exist.
- **Entity substitution is not caught by the guard at all** — "Africa" vs
  "Asia", "aspirin" vs "penicillin" are not inversions. Only the threshold
  stands between those and a wrong answer.
- `bypass_cache` remains the answer for callers who cannot tolerate any chance
  of substitution.

---

## D17 — In-process vector mirror: the linear scan did not hold

**Date:** 2026-09-10 · **Stage:** 4 · **Amends:** SPEC section 8.5

SPEC: *"A linear scan is fine at 10k entries and is the honest simple choice.
Do not add a vector database. If scan time exceeds 30ms, say so and we'll
discuss."*

**It exceeded 30ms.** Measured, fetching every vector from Redis per lookup:

| entries | total | embed | redis+matmul |
|---|---|---|---|
| 50 | 20.7ms | 12.1 | 8.6 |
| 200 | 14.2ms | 8.9 | 5.4 |
| **1000** | **74.4ms** | 9.4 | **64.9** |

At the configured 10,000 ceiling that extrapolates to roughly **650ms**.

**The cost is the round trip, not the maths.** 1000 vectors is 1.5MB of float32
over the wire; the matmul itself is microseconds even at 10k.

**Decision: mirror the vectors in process, refetch only when Redis says they
changed.** `mm:version` is incremented on every write; a lookup reads that one
integer and reuses the local matrix when it matches.

| entries | before | after |
|---|---|---|
| 1,000 | 74.4ms | **10.0ms** |
| 10,000 | ~650ms | **11.2ms** |

Essentially flat, because the remaining cost is the embedding (~9ms) plus a
1ms version check.

**This is not a vector database.** It is the same linear scan over a local copy
— no index, no ANN, no new dependency.

**The trade-off, stated plainly:** with several worker processes, one
process's write is invisible to another until its next version check. That
costs **missed hits, never wrong answers** — a stale mirror can only fail to
find something, and every hit is still verified against the live answer in
Redis. Memory is 10k x 384 x 4 bytes, about 15MB.

---

## D18 — Embedding input capped at 400 characters

**Date:** 2026-09-10 · **Stage:** 4 · **Amends:** SPEC section 2 budgets

Encoding cost grows with sequence length:

```
   96 chars ->  9.9ms       600 chars -> 16.2ms
 1200 chars -> 34.8ms      2400 chars -> 38.8ms  (plateau)
```

1200 characters breaks both budgets — 20ms classification, 30ms cache lookup.
The plateau at 2400 is `all-MiniLM-L6-v2` truncating at its 256-token limit,
which means **the model was already discarding everything past ~1000
characters.** The cap makes that bound explicit and predictable rather than
accidental.

**400 chars (~100 tokens), chosen on what the job needs.** The signals that
decide a tier — the reasoning cue, the question shape, the subject — sit at the
start of a request, and `question_part()` already strips pasted context before
this cap applies. 600 was tried first and left classification at 20.4ms p50,
grazing the budget with no margin.

**Consequence for the cache:** two prompts sharing their first 400 characters
now embed identically. Guarded by a length-ratio check (`LENGTH_RATIO_MIN =
0.5`) — prompts whose lengths differ by more than 2x are never a match,
whatever their similarity.

**Residual limitation, worth stating:** a prompt whose difficulty is only
revealed after 100 tokens of preamble will be misclassified. The pathological
worst case — 1200 characters with no sentence boundaries at all, so
`question_part()` has nothing to clip to — still sits at 15-20ms, right at the
budget rather than comfortably inside it. Pinned by
`test_classification_worst_case_is_bounded`.

---

## D19 — Test isolation from the live cache, and a 10-minute suite

**Date:** 2026-09-10 · **Stage:** 4

Two problems appeared the moment the cache went into the request path.

### The cache broke test isolation

The mock provider returns identical text for every prompt. With a live cache,
the second request in a test run became a HIT — so `mock.calls` stopped
counting provider calls, and `test_provider_failure_returns_502` got a 200
because a cached answer arrived before the deliberately-failing provider was
ever reached.

**This is the same failure as the Day 1 provider fixture, in a new place:**
adding a code path silently widened what the test doubles had to cover.

Fixed with `MODELMUX_CACHE_DISABLED=1` in `conftest.py`, matching the existing
`MODELMUX_DB_PATH` pattern. `test_cache.py` opts back in.

**And the first fix was wrong.** `test_cache.py` originally popped the variable
at module import — but **pytest imports every test module before running any
test**, so it re-enabled the cache for `test_api.py` too. The opt-in had to
move inside the fixture, scoped to the tests that need it.

### The suite went from 5 seconds to 9m46s

`embedding.init()` loads an 80MB model in ~10-15 seconds, and it is called from
the app lifespan — which a function-scoped `client` fixture enters **once per
test**.

Made `init()` idempotent in both `embedding` and `cache`. **9m46s → 57s.**

> A suite that slow stops being run, which costs more than any bug it would
> catch. Worth treating test runtime as a feature, not an afterthought.

---

## D20 — Resilience: the error taxonomy becomes executable

**Date:** 2026-09-12 · **Stage:** 5 · **This is Stage 5's completion criterion
(SPEC section 11): the system keeps serving with a provider forced to fail.**

### Measured, end to end

```
1. all healthy        200  tier=small  provider=groq       attempts=1
2. groq FORCED DOWN   200  tier=large  provider=anthropic  attempts=7  escalated
3. /health/providers  groq: open, 6 recent failures, last="mock 500"
4. everything down    502  attempts=3  tried=3 providers
```

Step 2 is the gate. Step 4 shows the breaker working: only 3 attempts, not 7,
because groq's circuit was already open and was skipped rather than re-paying
the timeout to rediscover it.

### The taxonomy was never cosmetic

`providers/base.py` has defined four error types since Stage 1. This is the
stage where they stop being documentation and start being control flow:

| type | policy | why |
|---|---|---|
| `ProviderTimeout` | retry | transient |
| `ProviderRateLimited` | retry, honour `Retry-After` | transient, and they told us how long |
| `ProviderServerError` | retry | probably transient |
| `ProviderBadRequest` | **never retry** | deterministic — identical failure, multiplied latency |

`RETRYABLE` is defined by *exclusion*, so a new error type added to `base.py`
defaults to "do not retry" — the safe direction.

### Two bugs I wrote, and what they teach

**1. The circuit let TWO probes through instead of one.**

The `OPEN -> HALF_OPEN` transition returned `True` without setting
`probe_in_flight`. The next caller fell through to the `HALF_OPEN` branch,
found the flag unset, and was also allowed past.

That is precisely the **thundering herd the breaker exists to prevent** — a
recovering provider getting hit by two requests at the moment it is least able
to handle them. Caught by `test_half_open_allows_exactly_one_probe`.

*The lesson:* a state machine with a side effect on the transition needs the
side effect applied on **every** path that performs the transition, not just
the one you were thinking about.

**2. A test that conflated retry with fallback.**

`test_bad_request_is_never_retried` asserted `calls == 1` and got 2. The code
was right: our config puts small AND mid on Groq with different models, so
after a `ProviderBadRequest` on the small model, trying the mid model is
legitimate **fallback** — an unknown model on one tier says nothing about the
other.

Fixed by disabling escalation in that test so it measures retry alone, and
adding `test_transient_failure_exhausts_retries_before_giving_up` as the
contrast case.

### 502 vs 503 — a distinction worth making

- **502** — we tried providers and they failed
- **503** — every circuit was open, so nothing was even attempted

The second carries `estimated_recovery_seconds`. "We tried and they broke" and
"we have stopped trying, come back in 30 seconds" are different facts, and an
operator reading logs during an incident needs to tell them apart.

### Jitter is not decoration

`backoff_seconds()` returns `uniform(0, base * 2^attempt)`, not the exact
value. Without randomisation every client that failed at the same moment
retries at the same moment, producing a synchronised burst that re-triggers the
very rate limit they are backing off from.

`test_backoff_grows_and_is_jittered` asserts the delays actually differ.

### Stated limitation: circuit state is per process

SPEC 8.7 says in-memory single-process is acceptable. With several workers each
keeps its own view, so a provider can be open in one process and closed in
another.

**The consequence is uneven traffic to a failing provider, not incorrect
answers.** Sharing the state would mean putting it in Redis — which introduces
a dependency on the thing most likely to be down during an incident.

---

## D21 — Metrics: percentiles in Python, and latency from successes only

**Date:** 2026-09-12 · **Stage:** 6 (partial) · **Amends:** SPEC section 8.9

SPEC: *"Percentiles: compute in SQL where possible, in Python otherwise. Do not
approximate silently."*

**SQLite has no `PERCENTILE_CONT`.** So p50/p95 are computed in Python from the
latency column. That is **exact, not approximate** — the cost is pulling the
window's latencies into memory, which is fine at this scale and stated rather
than hidden behind a plausible-looking number.

### Nearest-rank, not interpolated

With a handful of requests, interpolation invents a latency nobody experienced.
`p95 = 147ms` should mean some request actually took 147ms. The rank method
always returns an observed value.

### Latency excludes errors

A fast 400 would flatter the p50; a timeout would distort the p95. Neither
tells you how long an *answer* takes, which is the only question a latency
percentile is asked. Only `success` and `cached` rows count.

### The savings figure carries its caveat in the payload

`get_stats()` returns a `savings_caveat` field naming DECISIONS.md D3.

`cost_if_large_usd` reprices the token counts we actually **observed** at
baseline rates — "same tokens, baseline prices", not what the large tier would
truly have cost. A dashboard showing a savings percentage without that is a
false claim dressed as a measurement, and the caveat travelling *in the
response* means no consumer can drop it by accident.

`test_savings_carry_the_bias_caveat` enforces it.

### `/health/providers` lists providers with no circuit yet

A provider that has never failed has no `Circuit` object. Omitting it would
read as "missing" or "broken" on a dashboard, when it is the healthiest state
there is. The endpoint fills those in as `closed`.

---

## D22 — Two more test-isolation bugs, same family as before

**Date:** 2026-09-12 · **Stage:** 5

**1. A Redis client leaked across event loops.** `test_cache.py` left its
connected client in the module global. A later module's app shutdown called
`cache.close()` on it — from a different event loop — and got
`RuntimeError: Event loop is closed`.

Fixed by making `close()` clear `_redis` and by closing properly in the cache
fixture's teardown. **A redis.asyncio client is bound to the loop it was created
on**, so a module-global client is shared state with a hidden affinity.

**2. An ordering-dependent test.** `test_stats_on_an_empty_window_returns_zeros`
took only the `db_path` fixture, so it never ran `init_db()`. It passed only
when some earlier test happened to create the table first. Added the `client`
fixture so it creates its own preconditions.

> This is the **third** distinct test-isolation bug in this project — the
> provider fixture (Day 1), the cache (Day 2), and now the event loop. Every one
> had the same shape: **module-level state that one test populates and another
> silently inherits.** Worth treating any module global in a test-touched code
> path as a hazard by default.

---

## D23 — Quality scoring: the decision that has NOT been made

**Date:** 2026-09-21 · **Stage:** 6 · **Status: OPEN — needs the project
owner's call, not mine.**

SPEC's evaluation section says *"Quality scoring is TBD — see DECISIONS.md for
the approach and its limitations."* This entry exists because that pointer led
nowhere: the decision was never taken, and writing one here as though it had
been would be fabricating a choice.

### Why it cannot be deferred quietly

SPEC section on Results: *"Quality is reported alongside cost deliberately. Cost
savings mean nothing if the cheaper answers are worse, so both numbers are
measured on the same evaluation set."*

A savings percentage published without a quality column is **half a result**,
and the missing half is the one that could invalidate the other. "We cut cost
78%" is a finding only if answers held up; otherwise it is a description of
having bought worse answers.

### The three viable approaches

| Approach | Cost | What it is good for | Where it is weak |
|---|---|---|---|
| **Blind human spot-check** | ~1 hour of your time, 30 items | Most trustworthy signal available | Tiny sample; one grader's taste |
| **LLM-as-judge** | ~1 large-tier call per prompt | Scales to the whole set, consistent rubric | The judge shares blind spots with the model it grades; known to favour longer and more confident answers |
| **Exact-match on a factual subset** | Free | Fully objective | Only works for lookup prompts — exactly the ones routing already gets right, so it measures the easy half |

**LLM-as-judge is not a violation of "no LLM call in the classification path."**
That constraint governs *routing*, which must be cheaper than the request it
routes. This is offline evaluation, run once, on a fixed set.

### Recommendation

**Blind human spot-check of 30, plus LLM-as-judge on the full set, reported as
two separate columns — never averaged together.**

If they disagree, that disagreement *is* a finding worth publishing: it would
say something real about whether automated grading can be trusted for this task.
Averaging them would destroy exactly that information.

### What is built while the decision is open

`eval/run_eval.py` already exports a **blind** spot-check file:

- which answer came from which tier is withheld from the grader
- the mapping is written to a separate `_KEY.json`
- the instructions say not to open the key first

Blinding is the whole point. A grader who knows "A is the cheap one" finds what
they expect to find, and the exercise becomes theatre. This needs **no API key**,
so quality can be graded before the cost blocker clears.

The harness prints a loud warning whenever it reports savings with no quality
column, rather than letting the omission pass silently.

### The cost of leaving it open

The README's results table stays incomplete, and the project's headline claim
stays unproven in the dimension that matters most. That is the honest state, and
it is preferable to a quality number produced by a method nobody chose.

---

## D24 — The dashboard is one static file, not React + Vite + Recharts

**Date:** 2026-09-21 · **Stage:** 6 · **Amends:** SPEC section 3

SPEC lists React + Vite + Recharts and a `dashboard/` directory with its own
`npm install`. Shipped instead: a single `dashboard/index.html`, ~200 lines,
no build step, served by FastAPI at `/dashboard`.

**Why:** PLAN.md compressed nine spec-days into four. Something had to give, and
the honest way to absorb that is to cut deliberately rather than let quality slip
everywhere. The dashboard is the most cuttable item in the project — **the
numbers are the deliverable; the dashboard only shows them off.** A build
toolchain for four `fetch` calls would have cost most of a day that Stage 6's
real work needed.

**A second benefit, not merely an excuse:** served from the API's own origin,
the page calls `/v1/stats` and `/health/providers` with no CORS configuration
and no second dev server to run.

**What was given up:** charts (the figures are tiles and a table), a component
model, and anything resembling a growth path. If this ever needs real
visualisation, it is a rewrite rather than an extension — accepted knowingly.

**One thing it does NOT give up:** the savings caveat. The page renders
`savings_caveat` straight from the `/v1/stats` payload, so the D3 bias cannot be
dropped by a UI that forgot about it.

---

## D25 — Rate limiting: a token bucket, keyed by IP, checked first

**Date:** 2026-09-21 · **Stage:** 6 · **Implements:** SPEC section 10

SPEC required this and it had simply never been built — found by auditing the
spec's endpoint and error tables against `main.py` rather than by anything
failing.

**This is the only component in the project that protects money rather than
correctness.** Every other limit guards a result; this one guards the account.
A single caller in a retry loop empties the budget in minutes, and no amount of
clever routing matters once the credit is gone.

### Token bucket, not a fixed window

A fixed window ("60 per minute, reset on the minute") permits **120 requests
across a boundary** — 60 at 11:59:59 and 60 more at 12:00:00. That is double the
intended rate at the worst possible moment.

A token bucket refills continuously, so the long-run rate is exactly what was
configured while still allowing a short burst up to capacity. It is also
cheaper: two floats per caller, no history to sweep.

`tokens` is a **float**, deliberately. Rounding down on every check would leak
allowance and make the effective rate quietly lower than the configured one.

### Checked BEFORE classification and before the cache

A rejected request must cost nothing. Checking after routing would still burn
~11ms of embedding per rejected request — most of what the limit exists to
prevent.

### A new caller starts FULL

Starting empty would reject everyone's first request, which is
indistinguishable from an outage to someone trying the API for the first time.

### X-Forwarded-For is deliberately NOT trusted

It is caller-controlled. Honouring it without a verified proxy in front would
let anyone bypass the limit by inventing a header — turning the protection into
decoration. `test_forwarded_for_cannot_bypass_the_limit` pins this.

Behind a real load balancer this must change, and the change has to come with
"trust exactly N proxy hops", not a blanket trust of the header.

### Idle buckets are evicted

Unbounded growth per distinct IP is a slow memory leak that an attacker can
accelerate deliberately. Buckets unused for an hour are swept, at most once a
minute.

### A 429 still writes a database row

SPEC section 2: nothing is silently dropped. A rejected request is still a
request that happened, and an operator investigating "the API keeps refusing
me" needs to see it.

### Stated limitations

- **Per process.** With several workers the effective limit is
  `workers x rate`. Sharing it would put Redis on the fast path — a dependency
  on the hot path for what is a safety net rather than a billing boundary.
- **Keyed by IP, because there is no authentication.** An IP is shared by
  everyone behind a NAT and changes for one user on a mobile network. Once
  auth exists, the key should become the API key or account id. Documented as a
  limitation rather than presented as identity.

---

## D26 — `/v1/compare` turns the D3 assumption into a measurement

**Date:** 2026-09-21 · **Stage:** 6 · **Implements:** SPEC section 5,
**mitigates:** DECISIONS.md D3

D3 has been the largest unquantified caveat in the project since Stage 1:
`cost_if_large_usd` **assumes** the baseline tier would emit the same number of
output tokens as the routed tier. That assumption is not measured, because
measuring it means paying for the expensive call — which is the very thing the
router exists to avoid.

`POST /v1/compare` runs both, so the assumption becomes checkable:

```json
"comparison": {
  "cost_saved_usd": 0.000365,
  "savings_pct": 95.67,
  "output_token_ratio": 1.0,
  "d3_bias_note": "... >1 means live savings are UNDERstated, <1 OVERstated."
}
```

**`output_token_ratio` is the bias, stated as a number.** Not "there may be a
discrepancy" in prose, but the factor by which the live figure is wrong and in
which direction.

### Deliberately not cached, and not logged as a normal request

It costs two provider calls. Letting it into `/v1/stats` would corrupt the very
numbers it exists to audit — the savings percentage would include a request
that deliberately paid twice.

### Rate limited like everything else

It is the **last** endpoint that should be exempt from budget protection,
precisely because it costs double. `test_compare_is_rate_limited_too` pins it.

### Why this matters more than it looks

Every savings figure this project reports rests on D3. Until now the honest
position was "we know this number is biased and cannot say by how much." This
endpoint changes that to "here is how much, on this prompt" — and
`run_eval.py` computes the same ratio across a whole evaluation set.

An assumption you have quantified is a finding. The same assumption unquantified
is a footnote nobody acts on.

---

## D27 — The large tier was priced at a previous-generation rate

**Date:** 2026-09-21 · **Stage:** 6 · **Corrects:** config.yaml, and every
savings figure the project has ever produced

The large tier had carried `UNVERIFIED` since Stage 1. Verified now against the
Anthropic model and pricing reference, and **it was wrong in two ways at once**:

| | was | is |
|---|---|---|
| model | `claude-sonnet-5` | **`claude-opus-5`** |
| input / 1K | 0.00300 | **0.005** |
| output / 1K | 0.01500 | **0.025** |

**`0.003 / 0.015` is Claude Sonnet 4.6 pricing** ($3/$15 per 1M) — a previous
generation. Claude Sonnet 5 is $2/$10. So the figure matched neither the model
named in the config nor any current price: I had inherited the spec's
placeholder numbers and attached a model name I guessed at.

The spec itself never named a model — it said `<current-model>` — so the
correct default for the most capable tier is **Claude Opus 5**, $5/$25 per 1M.

### Why this mattered more than a config typo

`cost_if_large_usd` is the baseline **every** savings figure is measured
against. A wrong price there propagates straight into the project's headline
claim.

For a representative 500-in / 300-out exchange:

```
small                  $0.000127
large (old, wrong)     $0.006000   -> savings would read 97.9%
large (verified)       $0.010000   -> savings actually  98.7%
```

The error *understated* the true gap — but the direction is not the point. A
number that is wrong in your favour is still wrong, and it was wrong by 40% on
the baseline.

### What this says about the earlier verification pass

Groq's models and prices were verified on 2026-09-08 (D14) and that same pass
left Anthropic flagged `UNVERIFIED`. The flag was honest and it sat there for
two weeks while every savings figure quietly depended on it.

**A known-unverified number in a load-bearing position is a bug with a comment
on it.** The comment does not make the number less wrong; it only makes it
easier to find once someone finally looks.

### Left as an alternative

`claude-sonnet-5` at 0.002 / 0.010 per 1K is recorded in `config.yaml` as the
cheaper option, should the large tier be meant as "good enough" rather than
"most capable". That is a product decision, not a correctness one.

---

## D28 — A quality score needs a tool that reads the grading back

**Date:** 2026-09-21 · **Stage:** 6 · **Completes:** the buildable half of D23

`run_eval.py` exported a blind spot-check file from the day it was written, and
**nothing could read it back.** Quality was ungradeable even by hand — the
export was a gesture at the problem rather than a solution to it.

`eval/grade_quality.py` closes that: it joins the graded file to its
`_KEY.json`, unblinds, and reports routed-wins / baseline-wins / ties.

### Unblinding happens in exactly one place

The export withholds which tier produced which answer and writes the mapping to
a separate file. This script is the only code that joins them.

**That separation is the method, not a formality.** A grader who knows "A is
the cheap one" finds what they expect to find — and nothing in the output would
reveal that it had happened.

### The headline is "not worse", not "wins"

```
ROUTED NOT WORSE: 27/30 = 90%
```

Routing is justified when the cheap answer is **as good**, not only when it
beats the expensive one. Counting only wins would understate the case for
routing and measure the wrong question.

### It refuses to let a small sample look like a measurement

Below n=20 the output says so explicitly, with what one item is worth as a
percentage. A 6-item spot-check reporting "50%" invites exactly the
over-reading the caution prevents.

### Two caveats printed on every run

- **Not independent.** One grader, and the same person who built the router
  unless someone else filled in the file.
- **Not a quality score.** It is a *pairwise preference* between two answers to
  the same prompt. It says routing held up against the baseline — not that
  either answer was any good.

### No LLM-as-judge mode, deliberately

That needs the `anthropic` SDK, which is **not on the approved dependency list**
in SPEC section 3, plus an `ANTHROPIC_API_KEY` this project has never had.
Adding a dependency is a conversation, not a default taken while the owner is
away.

The human path needs neither — which is exactly why D23 recommended it as the
half to build first. It is gradeable today.

---

## D29 — The 401 was never the key. It was the environment shadowing `.env`.

**Date:** 2026-09-21 · **Stage:** 1 (its last open criterion) · **Fixes:**
`app/config.py`

**Every API key this project has ever had returned 401** — across three keys
and several weeks. The conclusion drawn each time was "the key is bad". It was
never the key.

`load_dotenv()` **does not overwrite a variable that is already set in the
environment.** That is correct 12-factor behaviour: a real deployment sets real
variables and a stray `.env` must not override them. But it is silent.

A `GROQ_API_KEY` had been set once at **Windows User scope** — persistent,
surviving reboots and new terminals, invisible from inside the project. Every
run loaded `.env`, found the variable already present, and kept the dead one.

### The clue that identified it

The Groq console showed the new key at **0 API Calls, Last Used: Never**. A key
that is rejected still records an attempt. Zero attempts means *that key was
never sent* — which moves the problem out of the key entirely and into what the
process actually read.

**Ask what the evidence rules out, not what it suggests.** "401" suggests a bad
key. "0 calls" *rules out* the key being involved at all, and that is the more
useful fact.

### The fix: make shadowing impossible to miss

Removing the stray variable fixes today. It does not stop the next one. So
`.env` loading moved into `config.load_env()`, which compares what `.env` holds
against what the environment holds and **warns loudly on stderr** when they
differ — naming the variable, showing the last four characters of each value,
and printing the exact command that clears it.

**It prints the last four characters and never the key.** Enough to tell two
secrets apart, useless to anyone reading a log. That rule comes from an actual
leak earlier in this project: verify a secret by its *properties* — length,
prefix, quoting, whitespace — never by printing it.

### Why it lives in `config.py` and not `main.py`

It was in `main.py`. The eval scripts never import `main`, so they never loaded
`.env` at all — a second bug this move fixed (D31). `config.py` already owns
where settings come from, and environment loading is the same question.

### Cost

A warning on every run that has both a `.env` and an environment variable set,
including deployments where that is deliberate. Noise in the legitimate case is
the price of never losing another week to the silent one. It is a warning, not
an error: it never changes behaviour, only visibility.

---

## D30 — The large tier holds two providers, so one key runs the whole eval

**Date:** 2026-09-21 · **Stage:** 6 · **Changes:** `config.yaml`,
`app/config.py`

Only a Groq key exists. The large tier pointed at Anthropic, so any prompt
classified `large` died — and the held-out set sends **16 of 32** there. The
evaluation could not complete at all.

A tier holds a **list** of providers (D4, SPEC §7), which until now had never
held more than one entry. The large tier now lists Anthropic first and
`openai/gpt-oss-120b` second. Without `ANTHROPIC_API_KEY` the first fails fast
with `ProviderBadRequest` — deterministic, never retried — and within-tier
fallback serves the request from Groq.

**Rejected: temporarily repointing the large tier at Groq.** That would make
the config lie about the intended ladder, and the intended ladder is exactly
what the projected figures are priced against. The fallback path keeps the real
configuration visible *and* exercises resilience machinery that had only ever
been tested against synthetic responses.

### The correctness problem this exposed

Cost was computed per **tier**, from `providers[0]`. With fallback live, that
prices a Groq answer at **Claude Opus 5 rates — 167× too high.**

`config.cost_for_provider(tier, provider_name, ...)` now looks up the provider
that actually answered, falling back to the tier price only if the name is not
found.

### Cost

Savings measured this way are *"versus `gpt-oss-120b`"*, not *"versus Claude
Opus 5"* — a 2× spread instead of 33×. **This is why the measured saving is
6.6% and not 37%.** Any report using this configuration must say which baseline
it ran against, which is why the README carries both rows, each labelled.

---

## D31 — The eval scripts never loaded `.env`, and the first numbers I published were wrong

**Date:** 2026-09-21 · **Stage:** 6 · **Fixes:** all four `eval/` entry points

Two failures found within an hour, both in the measurement path rather than the
product. Recorded together because they share a cause: **the eval harness was
never held to the same standard as the server.**

### 1. No environment, so no key

`run_eval.py` reported `no successful pairs (32 attempted)`. It imports
`config`, never `main`, and `.env` loading lived in `main`. Fixed by D29's
move; all four scripts now call `config_module.load_env()`.

### 2. The first published numbers were inflated about 40×

The first completed run gave **$11.51 routed / $17.97 baseline per 1k**. Both
were wrong. `run_eval.py` priced by tier — `providers[0]`, Anthropic — while
every call was answered by Groq. It was pricing `gpt-oss-120b` output at Claude
Opus 5 rates.

The real figures are **$0.4121 / $0.4411**.

**What caught it:** the projection computed afterwards came out *identical* to
the measurement. Two numbers that should differ by construction agreeing
exactly is not a coincidence, it is a shared bug. The savings *percentage* was
barely affected — both columns were inflated by nearly the same factor — so
nothing about the headline looked wrong.

### Why this is the failure mode that matters most here

The whole project is a claim about cost. **A bug in the thing that measures
cost is worse than a bug in the thing that spends it.** A routing bug shows up
as a bad answer; a pricing bug shows up as a confident number in a README that
nobody ever re-derives.

`run_eval.py` already refused to run against mock providers without
`--simulated`, on the principle that a plausible fake is worse than no number.
It then produced a plausible fake out of real data instead, through a path
nobody had guarded.

### Cost / what remains unprotected

No test asserts that eval pricing matches server pricing. The two now share
`cost_for_provider()`, which makes them consistent by construction rather than
by assertion — weaker than a test, and worth adding one later.

---

## D32 — A reasoning model returning empty content is an error, not an answer

**Date:** 2026-09-21 · **Stage:** 6 · **Fixes:** `app/providers/groq.py`

The first live call returned **HTTP 200 with `content: ""`**.

`openai/gpt-oss-*` are reasoning models. They think in a separate `reasoning`
field, billed as completion tokens but never returned in `content`. With
`max_tokens=20` the budget was spent entirely on reasoning: a valid,
paid-for, successful response containing nothing.

The adapter now raises `ProviderServerError` naming the likely cause — the
`finish_reason` and the fact that reasoning tokens probably consumed the
budget — rather than returning the empty string.

### Why an exception rather than returning `""`

An empty string is indistinguishable from a real answer to every layer above
it. It would be **cached** — poisoning that prompt for the whole TTL — **logged
as a success**, and counted in the savings figure as a cheap win.

The cheapest possible answer is the one that says nothing. A cost-optimising
router that treats silence as success has an incentive pointing the wrong way.

Raising routes it into machinery that already exists: retry, then within-tier
fallback, then a logged failure row. Nothing is silently dropped (SPEC §2).

### Cost

A model that legitimately answers with empty content is now an error. No such
case exists in this project's traffic; a summariser handed nothing to summarise
might be one.

---

## D33 — What the first live measurement actually showed

> ⚠️ **SUPERSEDED IN PART BY D36.** The 6.6% cost saving recorded below did
> **not** reproduce — a second run of the identical set gave 1.7%. The real
> routing effect is 3-4% and the A/B measurement is noise-dominated. The
> latency result below did reproduce. Left in place because the mistake is the
> useful part.

**Date:** 2026-09-21 · **Stage:** 6 · **Records:** the README results table

32 held-out prompts, live, 0 failures, total spend **$0.027**.

| | ModelMux | baseline | |
|---|---|---|---|
| p50 latency | **2,803 ms** | 6,973 ms | **2.5× faster** |
| p95 latency | 9,037 ms | 9,019 ms | unchanged |
| cost / 1k, measured | $0.4121 | $0.4411 | 6.6% saved |
| cost / 1k, projected at configured tiers | $11.49 | $18.25 | 37.1% saved |

### The measured saving is small, and the reason is the interesting part

With one key the ladder collapses: `large` *is* `gpt-oss-120b` via fallback
(D30), the same model `mid` uses. The router sent 16 of 32 prompts to `large`,
where routed and baseline are byte-for-byte the same call. Half the traffic had
nothing to save, and the other half had a 2× spread to save it from.

**A router's savings are bounded by the price spread it is given.** Perfect
classification earns nothing on a flat ladder. That is a property of the
economics, not a defect in the classifier — and it is a finding the projection
alone would never have produced.

### The projection is arithmetic, not simulation — and is still labelled

It reprices **measured** token counts at the configured rates. No Anthropic
call was made, so it is projected, and the README says so inside the table
rather than in a footnote. Its weakest assumption: Claude Opus 5 would not
answer at the same length as `gpt-oss-120b`, and that ratio is unknown because
it was never run.

### Latency is the stronger measured result, and was never the goal

2.5× at the median, from real calls, with no repricing and no assumptions. p95
is unchanged because the tail is the hard prompts, which route large either way
— routing improves the *typical* request and leaves the worst case alone.

The project was framed entirely around cost. The honest headline from the first
live run is speed.

### D3's bias assumption, finally measured

`cost_if_large_usd` assumes both tiers emit the same number of output tokens.
Measured here: the baseline produced **1.04×** the routed tier's output
(22,713 vs 21,844). The assumption understates savings by about 4% —
approximately unbiased, and now a number rather than a caveat.

### What this still does not show

**Quality.** Nothing above says the cheap answers were any good. A blind
spot-check is exported and gradeable today (D23, D28) and nobody has filled one
in. Until then this is half a result, and the README says so.

---

## D34 — The reported error names the cause, not the circuit that skipped

**Date:** 2026-09-21 · **Stage:** 6 · **Fixes:** `app/resilience.py` ·
**Found by:** a test that D30 broke

Adding a second provider to the large tier (D30) broke
`test_provider_failure_still_writes_a_row`. The failure row still appeared —
SPEC §2 held — but its message changed from `mock 500` to:

```
all providers failed; last error: circuit open -- skipped
```

Both statements are true. Only one is useful.

### What went wrong

`dispatch()` reported `trail[-1].error`. With a longer provider chain, groq's
breaker trips partway through and every subsequent entry is a skip. The last
trail entry is therefore almost always `circuit open -- skipped` — **the
symptom of the outage, recorded after the cause had scrolled past.**

An operator reading that row learns that ModelMux declined to make a call. They
do not learn that the provider was returning 500s, which is the only fact that
tells them what to do next.

### The fix

`Attempt` gained a `skipped: bool` flag, set only on the circuit-open path.
The reported error is now the last provider **actually called**; skips are used
only when nothing was ever called, which is the genuine "all circuits open"
503 and where "circuit open" *is* the right answer.

A boolean rather than matching on the message string: the string is for humans
and should stay free to change.

### Why the test was right and the code was wrong

The tempting fix was to relax the assertion — the row was written, the status
was `error`, the contract held. But the assertion was testing something real:
**that a failure explains itself.** SPEC §2 says nothing is silently dropped;
a row that records the wrong reason is a quieter version of the same failure.

This is the second time in this project that lengthening the fallback chain
changed behaviour nobody predicted. A provider list is not just a longer list —
it changes which error survives to be reported.

### Cost

One more field on `Attempt`, and the reported error is no longer literally the
last thing that happened. The full trail still holds every attempt in order,
so nothing is lost; only the headline is chosen differently.

---

## D35 — The classification budget test is flaky in the full suite

> **UPDATE 2026-09-27: the CPU-contention hypothesis is REFUTED by a correct
> experiment. The cause of the in-suite flake is still unknown, but it is no
> longer merely untested — see the resolution at the end of this entry.**

**Date:** 2026-09-21 · **Stage:** 6 · **Status: OPEN — no fix, no cause**

`test_classification_is_within_budget` asserts classification stays under the
SPEC §2 budget of 20ms. Run alone it passes at the expected 11-12ms. Run inside
the full suite it **intermittently** fails:

```
classify_ms samples: [60, 58, 62, 140, 87, 85, 126]  median=85
```

Two identical full-suite runs, back to back: one passed, one failed. So it is
intermittent — but the failing samples are **not** noise around 12ms. Every one
of the seven is 5-11x the isolated figure. That is a different performance
regime, not a bad draw.

### What has already been ruled out

- **Not a single-sample artefact.** An earlier session already hit this and
  rewrote the helper to warm up three times and take the median of seven. The
  flake survived that.
- **Not the HTTP path.** The test reads the server's own `classify_ms`, which
  times classification only.
- **Not caused by this session's changes.** The changes were to error
  *reporting* in `resilience.py`; the suite failed the same way before them,
  and everything except `test_resilience.py` passes together (111 tests).
- **Not test ordering.** `test_api.py` collects and runs first either way.

### What I guessed, tried to measure, and could not confirm

The hypothesis was CPU contention in torch's intra-op thread pool, with
`torch.set_num_threads(1)` as the standard mitigation.

**The probe written to test it was wrong.** It generated load with
`os.cpu_count()` pure-Python busy loops, which contend on the **GIL**, not on
torch's thread pool. It ran long enough to be assumed dead and the process was
killed; one of its two runs had in fact completed, and its output is worth
recording precisely because of what it does *not* show:

```
torch threads: 1   (PIN_THREADS=True)
idle          median   11.2 ms
under load    median 76105.1 ms
```

**The 76-second figure is meaningless.** With torch pinned to one thread and
every core running a GIL-bound Python loop, the measured thread is starved of
the interpreter lock. That number measures my load generator, not
classification.

The useful half: **11.2 ms idle, measured outside pytest entirely**, which
independently reproduces the 11-12ms figure quoted throughout this project.
That much is confirmed.

**So the cause of the in-suite flake is recorded as unknown.** The hypothesis
is plausible and still untested. Writing it in as the explanation would be the
same category of error as D31 — a confident number nobody re-derived — and the
one reading I have argues only that the *experiment* was bad.

> A failed experiment that is mistaken for a result is worse than no
> experiment. The 76-second number is exactly the kind of figure that would
> have looked like a dramatic finding if quoted without knowing how it was
> produced.

### Why the test is not being loosened

The assertion is a SPEC §2 budget, and the existing docstring already warns
against tuning it until it goes green. A test that fails 5-11x over budget is
either finding something real about performance under load — which would matter
in production, where a uvicorn worker is by definition a busy process — or it
is measuring the machine. **Both are worth knowing, and neither is fixed by
raising the number.**

### What this costs, stated plainly

`pytest tests/ -q` does not reliably go green. That is a real cost: a suite
that sometimes fails trains you to ignore it, and the failure is at the one
place where the project makes a measurable performance promise.

The 11-12ms p50 figure quoted everywhere in this project was measured on an
**idle** process. Whether it holds on a loaded one is now an open question with
evidence attached, and the README says so.

### Next step for whoever picks this up

Rewrite the probe with a load generator that actually competes for CPU
(separate *processes*, not GIL-bound threads), measure classify time with
`torch.set_num_threads(1)` against the default, and only then decide whether
this is a production concern or a test-environment one.

### A second instance of the same family, found 2026-09-21

One full-suite run reported **`123 passed, 8 skipped`**. The 8 were the
Redis-backed cache tests, skipping on *"Redis unavailable"* — while Redis was
in fact up and reachable from Windows Python (`PING -> True`) both before and
after. Re-running gave `131 passed`. Not reproducible; `test_cache.py` passes
alone and passes after `test_api.py`.

So: a load-sensitive connect check, degrading under the same conditions as the
timing flake above.

**What makes this one worse than a flaky failure:**

> **A skip reads as success.** `123 passed` in a `-q` summary looks green. The
> 8 tests that did not run include the cache *safety* tests — the ones guarding
> the similarity threshold, which D16 calls the most dangerous setting in the
> project. A silent skip of exactly the tests that protect the riskiest
> behaviour is the worst possible thing to be intermittent.

**Not fixed, and deliberately not converted to a hard failure yet** — the skip
exists so the suite is runnable without Redis, which is a real need. The right
fix is probably to make it *fail* when Redis is expected (an env var or a
marker in CI) and skip only when it is genuinely absent, so absence is a
choice rather than an accident. Recorded here rather than acted on, because it
was observed once.

---

### RESOLUTION 2026-09-27 — the hypothesis was wrong, and an experiment says so

The probe was rewritten with a load generator that actually competes for CPU:
`os.cpu_count()` separate **processes** burning float arithmetic, rather than
GIL-bound Python threads. 15 samples after 5 warm-ups, on 12 cores.

| torch threads | idle median | under full load | slowdown | worst sample |
|---|---|---|---|---|
| **6 (default)** | 9.3 ms | **11.1 ms** | 1.20× | **65.8 ms** |
| **1 (pinned)** | 10.9 ms | **15.2 ms** | 1.39× | 18.7 ms |

**CPU contention does not explain the flake.** With every core saturated the
median reaches 11 ms unpinned or 15 ms pinned — both comfortably inside the
20 ms budget. The in-suite failure reports a *median* of 85 ms with all seven
of its samples above 55 ms. Contention produces at most a 1.4× median
degradation; the flake is roughly 7×. **The hypothesis is refuted, not merely
unconfirmed.**

### The finding that came out of it anyway

> **`torch.set_num_threads(1)` trades median latency for tail
> predictability.** The default pool is ~4 ms faster at the median and **3.5×
> worse at the worst case** (65.8 ms vs 18.7 ms). Pinned, every sample stayed
> under budget; unpinned, the tail blew straight through it.

For a service with a p95 commitment rather than a p50 one, pinning is
probably the right default — several in-flight requests each spawning a
six-thread forward pass is exactly the contention this measures.

**Not changed in the code.** The project has never measured classification
under real request concurrency, and a default should not be set from one
probe on an idle desktop.

### What is still unexplained

The in-suite 85 ms median. Contention is now out, joining sampling noise, the
HTTP path, test ordering and this session's changes on the ruled-out list.

Not yet examined: memory pressure and GC once the full suite has allocated,
whether `embedding.init()` is re-entered per TestClient, and whether some
other module-level state — the family D22 catalogues — leaves the model in a
slower configuration.

**D35 stays open.** What changed is that its leading hypothesis is dead,
killed by an experiment instead of surviving as a plausible guess.

---

## D36 — The 6.6% cost saving did not reproduce. It was noise.

**Date:** 2026-09-21 · **Stage:** 6 · **Corrects:** D33 and the README
results table

D33 recorded a measured cost saving of **6.6%**. Re-running the identical
32-prompt held-out set, unchanged in every respect, produced **1.7%**.

| run | routed | baseline | "saved" | baseline/routed output tokens |
|---|---|---|---|---|
| 18:40 | $0.4121 / 1k | $0.4411 / 1k | **6.58%** | 1.040 |
| 20:05 | $0.4181 / 1k | $0.4253 / 1k | **1.70%** | 0.974 |

Same prompts, same config, same routing decisions (9 small / 7 mid / 16 large
in both). **The only variable is how long the models chose to answer.**

### The actual routing effect is 3-4%

Comparing routed cost against the **same tokens repriced at baseline rates** —
which holds output length constant and isolates routing — gives:

```
run 1:  3.00%
run 2:  4.15%
```

And the arithmetic confirms why it must be small:

- 9 of 32 prompts route to the small tier
- those 9 produce only **5.2%** and **7.6%** of total output tokens
  respectively — short questions get short answers
- small is exactly **2×** cheaper than the tier above
- ceiling ≈ half of 5-8% ≈ **3-4%** ✓

**The A/B swing is ±5 points around a 3-4% signal. The noise is larger than
the effect.**

### Why this is a real methodological error, not bad luck

The two arms of the A/B are *separate generations*. Both vary independently,
so the difference between them carries **twice** the variance of either — and
the quantity being estimated is a few percent of a number dominated by 23
prompts that cost **identically in both arms**.

> **Subtracting two large noisy numbers to find a small difference is the
> classic way to measure nothing with great precision.** The routed and
> baseline totals agreed to within about 1.5% of each other; the "saving" was
> a rounding artefact of LLM verbosity.

Worse, the error was *invisible from inside a single run*: 6.6% is a plausible
number, it pointed the right direction, and it came from real calls with zero
failures.

### What should have been quoted instead

`cost_if_large_usd` — the same-token counterfactual — which D3 had flagged as
*biased* because it assumes both tiers emit the same output length.

**It is biased, and it is also far less noisy**, because holding tokens
constant removes the dominant error term. The D3 bias is ±4% and roughly
centred (1.040, then 0.974). The A/B noise is ±5 points and swamps a 3-4%
signal.

> **The "more honest" measurement was the worse estimator.** A/B feels more
> rigorous because it measures both arms for real. On a low-signal quantity
> with a high-variance generator, the assumption-carrying estimator wins —
> and it says so in its own caveat, which the A/B never did.

### What was changed

- README now leads with the **3-4% same-token routing effect**, shows both
  runs side by side, and states that a single A/B run of this set is not
  evidence.
- The **latency** result is separated out as the one that reproduced: 2.49×
  then 2.45× at p50. It is stable because it depends on *which model answered*,
  not on how many tokens it emitted.
- D33's 6.6% is superseded rather than deleted; it is left in place with a
  pointer here, because the mistake is the useful part.

### Cost

The project's headline cost claim drops from 6.6% to 3-4%, and gains a caveat
that it cannot be measured by A/B on this configuration at all. That is a
worse-sounding result and a better-supported one.

### What would fix it properly

1. **A second provider key.** At the configured 33× ladder the signal is ~37%
   and clears the noise trivially. This is one input away.
2. **Repeat runs with a variance estimate.** Two runs is not a variance
   estimate, it is two points. Five would give an honest interval.
3. **Pin generation determinism where the provider allows it** (temperature 0,
   fixed seed) so output length stops being a random variable.

None of these were done, and the README says the figure is provisional rather
than pretending otherwise.

### The lesson

> **Re-running a measurement is part of making it, not a formality afterwards.**
> This project already refused to publish numbers from mock data, already used a
> held-out set, already carried a bias caveat in the payload — and still
> published a headline figure from a single draw of a noisy distribution.
>
> Every guard was pointed at *where the number came from*. None was pointed at
> *whether it would happen again*.

---

## D37 — Quality is graded. The honest sample size is 8, not 30.

**Date:** 2026-09-27 · **Stage:** 6 · **Closes:** the empty quality column
(D23, D28) · **Changes:** `eval/grade_quality.py`

The project's oldest gap — *"cost savings mean nothing if the cheaper answers
are worse"* — now has a number. It is smaller and more qualified than the raw
output first suggested.

### What was done

The 30-item blind spot-check from the 2026-09-21 run was graded by **Claude
Opus 5 acting as an LLM-as-judge**, reading only the blind file. The `_KEY`
file was not opened until every verdict was written.

This is the **LLM-as-judge half** of D23's recommendation. It required no new
dependency, because the grader is external to the project rather than a
library it imports — which is precisely the distinction D23/D28 drew when
declining to build an `anthropic`-SDK judge into the harness.

### The raw result, and why it is misleading

```
routed better        5     17%
baseline better      0      0%
indistinguishable   25     83%
ROUTED NOT WORSE:   30/30 = 100%
```

**100% is not the number to quote.** A tier holds a provider *list* (D4), and
with only one API key the large tier falls back to the mid tier's model (D30).
So for every prompt routed to `mid` or `large`, routed and baseline resolved
to **the same model** — `openai/gpt-oss-120b` compared with itself.

| group | n | routed | baseline | tie |
|---|---|---|---|---|
| `large` → same model | 15 | 4 | 0 | 11 |
| `mid` → same model | 7 | 1 | 0 | 6 |
| **`small` → DIFFERENT model** | **8** | **0** | **0** | **8** |

**22 of 30 pairs could not have tested routing.** They measure generation
nondeterminism: two samples from one model, one judged better.

### The actual result

> **On the 8 pairs where routing changed which model answered
> (`gpt-oss-20b` vs `gpt-oss-120b`): not worse in 8/8, with zero cases where
> the cheap model lost.**

That is a real result and a *small* one. n=8; one item is 12%. Those 8 are
also, by construction, the easy prompts — they routed small *because* the
classifier judged them easy, so this measures "the cheap model handles what we
send it", not "the cheap model is as good in general". That is the right
question for a router, and it is a narrower question than the raw 100%
implies.

### The tool was reporting the misleading number, so the tool was fixed

`grade_quality.py` now reads the sibling `eval-*.json`, compares
`routed.model` against `baseline.model` per item, and splits the report:

- pairs that compared **different models** — the routing result, headlined
  with *"quote THIS one"*
- pairs that compared a **model with itself** — reported separately as the
  method's noise floor
- if the eval file is missing it says the split is **unavailable** and calls
  the headline an upper bound, rather than printing it unqualified

> **A harness that can produce a misleading headline eventually will.** This
> is the same lesson as D31 and D36: the guard belongs in the instrument, not
> in the reader's memory.

### The noise floor, and a bias check

The 22 same-model pairs split **5 routed / 0 baseline / 17 tie**. All five
preferences landing on the routed arm is a 6% two-tailed coincidence at n=5 —
suggestive, so it was checked rather than assumed:

- **Blinding held.** The A slot was routed in 14 of 30 items, baseline in 16.
- **No positional bias.** The five picks split 2 "A" / 3 "B".
- **No verbosity bias.** The longer answer was picked in only **2 of 5**, and
  twice the *shorter* answer won — once for penalising a wrong figure, once
  for answering the question actually asked.

With those ruled out, 5-0 is most likely chance. The useful reading is that
**17 of 22 same-model pairs were judged identical**, which is a reasonable
consistency check on the grading method itself.

### What this still is not

- **Not independent.** The grader is the same agent that wrote the router.
- **Not a human spot-check.** D23 recommended human grading *and*
  LLM-as-judge as **separate columns, never averaged**. One column is now
  filled; the other is not.
- **Not a quality score.** It is a pairwise preference. It says routing held
  up against the baseline, not that either answer was good.
- **n=8 on the only meaningful comparison.**

### Cost

The headline the project can now make is *"not worse in 8 of 8 where it
mattered"* rather than *"100% not worse"*. Quantitatively weaker, and the only
version that survives someone reading the key file.

### What would strengthen it

The same single input as everything else: **a second provider key**. With a
real large tier, all 30 pairs would compare different models and the sample
would go from 8 to 30 overnight.

---

## D38 — The large tier is Google Gemini, because it is the only capable model with a free tier

**Date:** 2026-09-27 · **Stage:** 6 · **Changes:** `config.yaml`,
`app/providers/google.py`, 3 test files · **Supersedes:** D30's provider order

### The decision

The large tier is now:

```yaml
providers:
  - name: google
    model: gemini-3.1-pro-preview    # $2 / $12 per 1M -> 0.002 / 0.012 per 1K
  - name: groq
    model: openai/gpt-oss-120b       # reachable fallback (D4/D30)
  # anthropic claude-opus-5 kept commented, verified
```

Prices **VERIFIED 2026-09-27** against `ai.google.dev/gemini-api/docs/pricing`.

### Why not Anthropic

**A Claude Pro subscription does not include API credits.** They are
separately billed products — Pro covers claude.ai and Claude Code; the API
runs on prepaid credits bought in the Console. Verified against Anthropic's
own help centre, not assumed.

So Anthropic costs real money (~$0.30-0.60 per eval run at Opus 5 rates) and
**Google AI Studio has a genuine free tier.** For a project whose remaining
blocker is *"we need a second, more capable model"*, the vendor is
irrelevant and the price is not.

Anthropic stays in the config, commented, with its verified prices, for
whoever has a key.

### What this buys — the whole point

| | before | after |
|---|---|---|
| large tier model | `gpt-oss-120b` **(= the mid tier's own model)** | `gemini-3.1-pro-preview` |
| ladder, input | 1× | **27×** |
| ladder, output | 1× | **40×** |
| cost A/B | noise-dominated, 3-4% signal vs ±5 pt variance (D36) | signal should clear noise |
| quality sample | **n=8** — 22 of 30 pairs compared a model with itself (D37) | **n=30** |

**One config change fixes the root cause of both D36 and D37.** Those were
never separate problems: they were the same missing provider, showing up in
two different measurements.

### Two traps found before they bit, not after

**1. Gemini 3 reasons by default, and bills thinking as output.** Google's
docs are explicit: *"response pricing is the sum of output tokens and
thinking tokens"*. Reading `candidatesTokenCount` alone would have
**understated cost on every single large-tier call** — flowing straight into
the headline, which is precisely the D31 failure.

`_billable_output_tokens()` now sums the answer and the thinking tokens
(`thoughtsTokenCount` / `total_thought_tokens`, spelled differently across
API versions), and reconciles against `totalTokenCount`. Unattributed tokens
are charged to output, which **overstates** our cost and makes the savings
look **worse** — the honest direction to be wrong in.

**2. A thinking model that runs out of budget returns empty, and still
bills.** Google's docs again: it *"returns truncated or empty output (while
still billing for any thinking tokens generated)"*. That is **D32 verbatim**,
in a second adapter.

`google.py` now raises `ProviderServerError` rather than returning `""`. Left
alone it would have been cached for 24 hours, logged as a success, and
counted in the savings figure as a cheap win.

> **The same bug, in a different provider, eight days later.** D32 was fixed
> in `groq.py` only. A lesson written down in one adapter is not a lesson
> applied to the codebase — it is a comment in one file.

### `max_tokens_default` raised 1000 → 4000

Both remaining providers are thinking models, and reasoning spends the same
budget as the answer. At 1000 a hard prompt can burn the whole allowance
thinking and return nothing, billed in full. Google's own guidance is to give
thinking room rather than clamp the budget.

**This is a ceiling, not a target** — raising it costs nothing unless a model
actually emits more, and cost is computed from tokens actually used.

### Five tests broke, and they were wrong to have passed

`test_tier_exhaustion_escalates_a_tier`, `test_open_circuit_is_skipped...`,
`test_trail_records_every_attempt`, `test_force_tier_overrides...` and
`test_health_providers_lists_every_configured_provider` all failed, every one
because it hardcoded `"anthropic"`.

None of them is about Anthropic. They test escalation, circuit skipping,
trail recording, forced tiers and health completeness.

> **They were asserting on a config value they never cared about** — so a
> configuration change broke tests of behaviour that had not changed. Each now
> reads the provider name from config. The tests got *more* correct, not just
> greener.

### Cost, and what stays unverified

- `gemini-3.1-pro-preview` is a **preview model**. Preview names get renamed
  and retired; a 404 here means the model moved, not that the key is broken.
  `gemini-3.8-flash` (0.00075 / 0.00375 per 1K, also free tier) is the stable
  alternative with a narrower ladder.
- **The Google adapter has still never made a live call.** It is written,
  now guarded, and verified against synthetic responses only. The prices are
  verified; the adapter is not — the same distinction D27 drew for Anthropic.
- The free tier has **rate limits**, and the Pro model's free access is
  described as limited. Retry and backoff absorbed Groq's TPM limits at
  32/32 (D36's re-run), so the machinery exists, but a large eval may be slow.
- Nothing changes without a `GOOGLE_API_KEY`: the tier falls back to Groq and
  behaves exactly as before. **The upgrade is opt-in by setting one variable.**

---
