# 06 — The 35 decisions, as talking points

`DECISIONS.md` is the project's spine: every choice the spec didn't make, with
**what was decided, the alternatives, why this one, and what it costs.**

> **The "what it costs" column is the whole point.** A decision recorded
> without its cost is a justification, not a decision. Interviewers can tell
> the difference.

This file compresses them into things you can say out loud.

---

## The seven that carry the interview

Learn these properly. The rest are supporting material.

### D15 — The shipped classifier is the less accurate one

| | accuracy | too cheap | too expensive |
|---|---|---|---|
| heuristic | **88%** | **4** | 0 |
| hybrid (shipped) | 72% | **0** | 9 |

**Decision:** ship hybrid, 16 points worse.

**Why:** the two error types are not equally bad. Too cheap = a hard question
answered badly by a weak model — a **trust** failure the user sees. Too
expensive = you overpaid by a fraction of a cent — a **money** failure that
only shows in a monthly bill.

**Cost:** 9 prompts of the 32 cost more than they needed to. Reversible in one
config line.

> **The sentence to say:** *"A single accuracy number would have ranked the
> heuristic first and hidden that it sends hard prompts to weak models. The
> direction of an error matters more than its frequency — so I report both
> columns and never average them."*

---

### D33 + D36 — The measured saving didn't reproduce, and that's the story

**Decision:** report the **same-token routing effect (3-4%)** as the headline,
show both A/B runs side by side, and state that a single A/B run of this set is
not evidence of anything. The 37.1% projection stays clearly labelled.

**Why:** the first run measured 6.6% and it was published. A re-run of the
identical set gave **1.7%**. Same prompts, same config, same routing — the only
variable was how long the models chose to answer.

**Why the signal is so small:** only one provider key worked, so the large tier
falls back to `gpt-oss-120b` — the same model as mid. The ladder compresses to
2×, and 16 of 32 prompts routed to a tier where routed and baseline are the
*same call*. The 9 prompts that do route cheap produce only 5-8% of all output
tokens, and save half of that → a 3-4% ceiling.

**Cost:** the headline drops from 6.6% to 3-4%, and gains a caveat that it
can't be measured by A/B on this configuration at all. Worse-sounding, better
supported.

> **The two sentences to say:** *"A router's savings are bounded by the price
> spread it's given — perfect classification earns nothing on a flat ladder.
> And when the effect is smaller than the variance, a single measurement isn't
> a result. I published 6.6% from one run; re-running it gave 1.7%. Every
> guard I'd built was pointed at where the number came from, and none at
> whether it would happen again."*

> **The subtle part, if they push:** *"The estimator I'd flagged as **biased** —
> repricing the same tokens at baseline rates — turned out to be the better
> one, because holding tokens constant removes the dominant error term. The A/B
> feels more rigorous because it measures both arms for real, but on a
> low-signal quantity with a high-variance generator it's strictly worse."*

---

### D23 — Quality scoring, the decision deliberately NOT made

**Decision:** build the blind spot-check harness; **don't invent a quality
number**.

**Why:** cost savings mean nothing if the cheaper answers are worse. Three
options existed — human spot-check, LLM-as-judge, automated metrics. The
recommendation: human spot-check of 30 **plus** LLM-as-judge on the full set,
reported as **separate columns, never averaged**.

**Cost:** the results table has an empty column, and the README says so.

> **Why this is a strength, not a gap:** *"I'd rather ship a result with a
> hole in it that's labelled, than fill the hole with a metric I can't
> defend. Averaging a human score with an LLM score produces a number that
> means nothing — it hides which one disagreed."*

**LLM-as-judge was not built** because it needs the `anthropic` SDK, which
isn't on the approved dependency list. **Adding a dependency is a
conversation, not a default.**

---

### D3 — The savings figure carries a known bias, in the payload

`cost_if_large_usd` reprices **the tokens actually observed** at baseline
rates. That's *"same tokens, baseline prices"* — not what the big model would
truly have cost, because a larger model usually answers at a different length.

**Decision:** ship the assumption, but carry the caveat **in the `/v1/stats`
payload itself** (`savings_caveat`), so no dashboard can drop it by accident —
and build `/v1/compare` to *measure* the bias.

**Measured: 1.040, then 0.974** across two runs. Straddles 1.0, so
approximately unbiased on average — but that ±4% swing in output length is
exactly what moved the A/B cost saving from 6.6% to 1.7% (D36).

> *"The caveat travels with the number instead of living in a README nobody
> reads. And then I measured it, so it's a number now rather than a
> disclaimer."*

---

### D24 — Cutting the React dashboard and SSE

**Decision:** the spec asked for React + Vite + Recharts and a `/v1/stream`
SSE endpoint. Both cut. The dashboard is **one static HTML file** served from
the API's own origin.

**Why:** the deliverable was a working chart. A build pipeline, a
`node_modules` tree and a CORS configuration weren't part of it.

**Cost:** no component reuse, no hot reload, hand-written DOM updates. Fine at
this size; wrong at ten times it.

> **Knowing what not to build is a senior signal.** Say the cost out loud —
> that's what makes it a decision rather than laziness.

---

### D7 — The database lives outside the project folder

**Decision:** SQLite at `%LOCALAPPDATA%\ModelMux\`, not in the repo.

**Why:** the project is under OneDrive. OneDrive syncing a SQLite file while
SQLite holds a lock can corrupt it, and WAL adds `-wal`/`-shm` companions that
must stay consistent with each other.

> This turned out to be the right instinct twice: **`learnings/` later
> vanished from the same synced folder mid-session** and was recovered only
> because it had just been committed.

---

### D29 — The 401 was environment shadowing, not a bad key

Covered fully in `07-war-stories.md` #1. The decision part: the fix wasn't
deleting the stray variable — it was making shadowing **impossible to miss**,
with a stderr warning that prints the last four characters of each value and
**never the key**.

---

## The rest, grouped

### Architecture

| D | Decision | The point |
|---|---|---|
| **D4** | A tier holds a **list** of providers, not one | Within-tier fallback. Unused until D30 needed it — then it was already there. |
| **D9** | Redis from **WSL2** | No supported native Windows build. |
| **D8** | **Python 3.13 is fine** | Recommended a 3.11 rebuild, then checked PyPI: `torch` ships a `cp313` wheel. **Reversed it.** |
| **D30** | Large tier gets a Groq fallback | Lets one key run the whole eval — and exposed the per-provider pricing bug. |

> **D8 is worth telling:** *"I recommended a downgrade, verified before acting,
> found I was wrong, and reversed it. Checking beats being confident."*

### Measurement integrity

| D | Decision | The point |
|---|---|---|
| **D12** | Stage 2's deliverable is a **list of misroutes** | Building something bad on purpose and measuring how bad. |
| **D15** | Held-out set created mid-stage | Caught myself reporting a training score. |
| **D16** | Cache threshold **measured**, with the failure case documented | No threshold is fully safe — needed a second mechanism. |
| **D21** | Percentiles from **successes only** | A 30s timeout isn't a slow answer, it's no answer. |
| **D23** | Harness **refuses** to run on mock data | A plausible fake is worse than no number. |
| **D26** | `/v1/compare` turns D3's assumption into a measurement | Not cached, not in `/v1/stats` — it would corrupt what it audits. |
| **D28** | A quality score needs a tool that **reads the grading back** | The export had existed for weeks and nothing could read it. |
| **D31** | Eval priced by tier, inflating figures ~40× | A bug in the instrument beats a bug in the product. |

### Correctness under failure

| D | Decision | The point |
|---|---|---|
| **D20** | Forced-failure testing via `MockProvider.fail_with` | Built day 1; made Stage 5 testable with no real outage. |
| **D22** | Test-isolation bugs are **one shape**, not four bugs | Module-level state inherited between tests. |
| **D25** | Rate limit **before** classification and cache; IP-keyed; `X-Forwarded-For` **not** trusted | A caller-controlled header would let anyone pick their own bucket. |
| **D32** | Empty content is an **error**, not an answer | The cheapest answer is silence — a cost optimiser must not be paid for it. |
| **D34** | Report the last provider **called**, not the last one skipped | "Circuit open" names the symptom after the cause scrolled past. |

### Open / honest

| D | Status | The point |
|---|---|---|
| **D23** | **OPEN** | Quality ungraded. Gradeable today, no key needed. |
| **D35** | **OPEN, cause unknown** | The classification budget test flakes in-suite. My hypothesis was never confirmed; the probe measured the GIL instead and the wrong result is recorded too. |
| **D27** | Corrected | Large-tier price was a previous generation's rate, flagged `UNVERIFIED` for two weeks. |

---

## How to use a decision in an interview

**The four-part shape**, every time:

1. **What was decided** — one sentence, no preamble
2. **What else was on the table** — proves it was a choice
3. **Why this one** — the reasoning, tied to a constraint or a measurement
4. **What it costs** — ← *this is the part that makes you sound senior*

**Worked example:**

> *"The cache similarity threshold is 0.88. I could have picked a
> conventional 0.9 or 0.95, but this is the most dangerous setting in the
> project — too loose and different users' questions share an answer, too
> tight and the cache earns nothing. So I swept it against 35 labelled pairs,
> 15 equivalent and 20 deliberately confusable.*
>
> *What I actually found was that **no threshold was safe** — one pair at 0.99
> similarity had opposite correct answers, because negation barely moves an
> embedding. So the number is backed by a lexical inversion guard, and there's
> a test that deliberately produces a wrong answer at a loose threshold so the
> failure mode is demonstrated rather than described.*
>
> *The cost is that 20% of genuine rephrasings still miss. I'd rather miss a
> cache hit than serve one user's answer to another."*

That answer contains a number, a method, a surprise, a mitigation, and a
stated cost. **That is the shape to aim for.**
