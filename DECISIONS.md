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
