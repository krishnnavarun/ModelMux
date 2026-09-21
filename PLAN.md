# ModelMux — Status and Delivery Plan

**As of 2026-09-21.** All six stages complete. Stage 1 and Stage 6 closed on
2026-09-21 when a working API key produced the first live measurements.

---

# Part 1 — Where the project actually is

## Done

| Stage | SPEC completion criterion | State |
|---|---|---|
| **1 — Bare proxy** | "A curl request returns an answer **and the row is visible in SQLite**" | ✅ **Complete 2026-09-21** — live `200` after the env-shadowing fix (D29) |
| **2 — Two providers, naive rule** | "A written list of misroutes exists in DECISIONS.md" | ✅ **Complete** — DECISIONS.md D12 |
| **3 — Real classifier** | Accuracy figures for heuristic / embedding / hybrid | ✅ **Complete** — DECISIONS.md D15 |
| **4 — Cache** | Measured hit rate + justified threshold + documented failure case | ✅ **Complete** — DECISIONS.md D16 |
| **5 — Resilience** | System keeps serving with one provider forced to fail | ✅ **Complete** — DECISIONS.md D20 |
| 6 — Metrics, dashboard, eval | README results table has **real numbers, no placeholders** | ✅ **Complete 2026-09-21** — cost and latency measured live (D33). Quality still ungraded, and the README says so. |

## What exists

```
app/
  main.py         endpoint, request IDs, limits, background logging
  config.py       config.yaml load + startup validation
  schemas.py      request/response contract
  router.py       naive + heuristic + embedding + hybrid modes
  tokens.py       tiktoken, loaded once and warmed
  db.py           SQLite schema, WAL, never-fails-a-request logging
  providers/      base ABC + 4-error taxonomy, groq, google, anthropic, mock, registry
  classifier/     signals.py, heuristic.py, embedding.py
  cache.py        Redis semantic cache, inversion guard, in-process mirror
  resilience.py   retry, fallback, circuit breaker
  metrics.py      aggregates, exact percentiles
  ratelimit.py    per-caller token bucket
eval/
  prompts.json          50 prompts -- TUNED AGAINST (training score)
  labelled.json         60 examples -- embedding k-NN reference data
  holdout.json          32 prompts -- NEVER used for tuning (the honest number)
  cache_pairs.json      15 equivalent + 20 dangerous pairs
  run_routing_check.py  misroute report
  run_classifier_eval.py  all 4 modes x both sets + overfitting check
  tune_cache_threshold.py  threshold sweep with the inversion guard
  run_eval.py           routed vs baseline; refuses to run on mock data
  explain.py            full decision breakdown for any prompt
dashboard/index.html    one static page, served at /dashboard
tests/            130 passing, no network, no API key
```

**Everything in the spec is built except `/v1/stream`**, cut deliberately
(D24). The only thing missing is *measurements*, which need a working key.

## What is genuinely verified vs. merely written

This distinction matters more than the file count.

**Verified by test:** the request path, cost arithmetic in both token
directions, prompt limits, the 400/502 split, database logging on every failure
path, privacy truncation, request-ID traceability, provider error→taxonomy
mapping, routing mechanics and boundaries.

**Written but never executed against reality:**
- **No adapter has ever spoken to a live server.** All three are tested against
  synthetic responses only.
- **Groq** models and prices are VERIFIED (2026-09-08). **Anthropic's large tier
  is not** — no key, prices never checked. Every savings claim rests on them.
- Every accuracy figure is measured against *my* hand-labels, written and
  judged by one person. That is the weakest link in the evidence.

## Stage 3 result (DECISIONS.md D15)

Held-out (n=32) — the only column that estimates generalisation:

| mode | accuracy | too cheap | too expensive |
|---|---|---|---|
| naive_tokens (Stage 2) | 31% | 20 | 2 |
| heuristic | **88%** | 4 | 0 |
| embedding | 72% | 0 | 9 |
| **hybrid (shipped)** | 72% | **0** | 9 |

Three datasets now, and confusing them invalidates the numbers:
`prompts.json` = tuned against · `labelled.json` = k-NN reference data ·
`holdout.json` = the quotable figure.

---

# Part 2 — Blockers, ranked

| # | Blocker | Blocks | Status |
|---|---|---|---|
| **1** | **No working API key** | Stage 1's live `200`, **cost + latency + quality in the README** | ❌ **STILL OPEN — you only** |
| **2** | **Quality scoring method undecided** | the quality column | ❌ **STILL OPEN — your design call (D23)** |
| 3 | No Google / Anthropic keys | live multi-provider routing; verifying large-tier prices | ❌ Open — you only |
| 4 | Redis not installed | Stage 4 | ✅ **Cleared** — WSL2, D9 |
| 5 | Embedding model not downloaded | Stage 3 | ✅ **Cleared** |

**Blockers 4 and 5 are done. 1 and 2 are the entire remaining gap**, and
neither is a coding task.

### Blocker 2 is a design decision nobody has made

SPEC's evaluation section says *"Quality scoring is TBD — see DECISIONS.md for
the approach and its limitations."* **DECISIONS.md contains no such entry,
because the decision was never taken.**

Day 4's gate requires quality reported alongside cost — "cost savings mean
nothing if the cheaper answers are worse." That cannot be done until you choose
how quality is measured. Three viable options:

| Approach | Cost | Honesty |
|---|---|---|
| **LLM-as-judge** — a large model grades small-tier answers | ~1 large call per eval prompt | Decent, but the judge shares the graded model's blind spots. Not a violation of "no LLM in the classification path" — this is offline evaluation, not routing. |
| **Human spot-check** — you grade 30 answers blind | Your time, ~1 hour | Most trustworthy, smallest sample |
| **Exact-match on a factual subset** | Free | Objective but only works for lookup-type prompts, which are the ones routing already gets right |

**Recommendation: human spot-check of 30, plus LLM-as-judge on the full set,
reported separately.** If they disagree, that disagreement is itself a finding
worth publishing. Decide this before Day 4 starts, not during it.

### Blocker 1 is the critical path, and it is not a code problem

Stage 6's completion criterion is *"the README results table has real measured
numbers and no placeholders."* **That is unreachable without at least one
working key.** Everything else can be built and tested against the mock; the
headline numbers cannot be faked.

Get one working Groq key before Day 3 and the plan holds. Without it, Days 1–3
still land in full and Day 4 produces a results table that is still `TBD` — the
project would be architecturally complete and evidentially empty.

### Do these before Day 1

```powershell
# 1. New Groq key -> paste into .env, then verify the model name is current
#    https://console.groq.com/keys   and   https://console.groq.com/docs/models
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --reload
curl -X POST http://localhost:8000/v1/chat -H "Content-Type: application/json" -d "{\"prompt\":\"hi\"}"

# 2. Redis, for Day 2
wsl -e sudo apt update
wsl -e sudo apt install redis-server
wsl -e redis-server --daemonize yes
wsl -e redis-cli ping        # expect: PONG
```

---

# Part 3 — Four-day plan

**Compression warning.** SPEC §11 allots 9 working days to Stages 3–6. This
plan does it in 4. Roughly 2.3x compression, and the honest way to absorb that
is to cut scope deliberately rather than let quality slip silently. What I
propose cutting is in Part 4 — read it before starting.

Each day ends with a **gate**. SPEC §12: do not start the next stage until the
previous gate passes.

---

## Day 1 — Stage 3: the real classifier

The largest stage, and the one the whole project's thesis rests on.

**Morning — heuristic signals** (`app/classifier/signals.py`)

Pure functions, no state, each independently testable. Built in the order the
D12 evidence ranks them:

| Signal | Fixes |
|---|---|
| `reasoning_markers` | "prove", "derive", "critique", "compare", "step by step" — present in nearly every too-cheap failure |
| `is_lookup` | protects the 15 cases already correct from being broken by the above |
| `multi_question` | 0/2 today |
| `has_code` | separates `code_simple` from `code_mid` |
| `question_context_ratio` | **not in SPEC §8.1** — the new signal D12 discovered; fixes all 5 `trap_long_but_easy` |
| `output_length_request` | "write an essay" vs "in one word" |
| `language_complexity` | avg sentence length, rare-word ratio |

**Midday — the scorer** (`app/classifier/heuristic.py`)

Weights as a single readable dict at the top of the file. Returns
`(score, signals)`. **Re-run `run_routing_check.py` after each signal is added**,
so each one's contribution is attributable rather than a lump at the end.

**Afternoon — the embedding classifier** (`app/classifier/embedding.py`)

- `eval/labelled.json`: ~120 examples, disjoint from `prompts.json`
- Load `all-MiniLM-L6-v2` once at startup (~80MB first download)
- Embed labelled set once at startup, hold vectors in memory
- Per request: embed, cosine-similarity, k=5 nearest, majority tier
- Confidence = fraction of the k neighbours agreeing

**Late — hybrid reconciliation** in `router.py`: on disagreement take the
**higher** tier (SPEC §8.4 rule 3 — fail towards quality), then escalate if
confidence is below threshold.

**GATE — Day 1  ✅ PASSED 2026-09-08**
- [x] Accuracy recorded for all four modes on a held-out split → DECISIONS.md D15
- [x] Hybrid beats naive (31% → 72%), and **too-cheap fell 20 → 0**
- [x] The `stage2_failure` tests failed and were inverted *(the success signal)*
- [x] Classification **11-12ms p50** vs the 20ms budget
- [x] Numbers written into DECISIONS.md (D15) and learnings/13
- [x] 56 tests passing

**Held-out results (n=32):**

| mode | accuracy | too cheap | too expensive |
|---|---|---|---|
| naive_tokens | 31% | 20 | 2 |
| heuristic | **88%** | 4 | 0 |
| embedding | 72% | 0 | 9 |
| **hybrid (shipped)** | 72% | **0** | 9 |

**Two caveats on that table.** Hybrid is 16 points *less* accurate than the
heuristic and ships anyway, because it eliminates every too-cheap misroute
(SPEC 8.4 rule 3). And the held-out score came out *higher* than the tuned
score, which is not evidence of good generalisation — the two sets are not
equally hard.

**Unplanned work absorbed on Day 1:** the venv hit 1.1 GB / 36,030 files after
PyTorch and had to be moved out of OneDrive (`C:\dev\modelmux-venv`), because
cold imports were taking 60-80 seconds.

**Risks:** the 80MB download needs internet. Embedding may push past 20ms —
measure, and if so make `mode: heuristic` the default and record why.

---

## Day 2 — Stage 4: the semantic cache

**Requires Redis running.** Do the WSL install before starting.

**Morning** (`app/cache.py`) — Redis keys per SPEC §8.5:
```
mm:vec:{id}   embedding as bytes
mm:ans:{id}   JSON: prompt_hash, response, tier, model, tokens_out, created_at
mm:index      live ids for the linear scan
```
Linear scan at 10k entries. **No vector database** — if scan time exceeds 30ms,
stop and say so rather than reaching for one.

**Afternoon — threshold tuning.** SPEC calls `similarity_threshold` *"the most
dangerous setting in this project"*, and it is the one number in the whole
system I most want you to choose rather than inherit from me. Sweep it, measure
hit rate against false-hit rate, and write the justification.

**Mandatory:** a test in `tests/` that **demonstrates a wrong answer** at a
deliberately loose threshold. SPEC §8.5 requires this, and it is the honest
counterweight to a good hit-rate number.

**GATE — Day 2**
- [ ] Measured hit rate exists
- [ ] Threshold choice justified in DECISIONS.md **with the failure case**
- [ ] Cache hit returns **without calling any provider** — asserted via `mock.calls == 0`
- [ ] Eviction works past `max_entries`
- [ ] Cache lookup under **30ms**

---

## Day 3 — Stage 5 resilience + Stage 6 metrics

**Morning** (`app/resilience.py`) — three mechanisms, in order:
1. **Retry** — timeout / rate-limit / 5xx only. Exponential backoff plus jitter.
   **Never** retry `ProviderBadRequest` — the taxonomy already encodes this.
2. **Fallback** — next provider in the tier; if none, escalate one tier
   (governed by the `escalate_on_tier_exhausted` flag from D5).
3. **Circuit breaker** — closed → open → half-open, per provider, in memory.
   Note the single-process limitation explicitly.

The mock provider's `fail_with` makes all of this testable deterministically —
no waiting for a real outage.

**Afternoon** (`app/metrics.py` + endpoints) — `GET /v1/stats`,
`GET /v1/requests`, `GET /health/providers`. Aggregate queries only; nothing
here computes routing. Percentiles in SQL where possible — **do not approximate
silently.**

**GATE — Day 3**
- [ ] System keeps serving with one provider forced to fail
- [ ] Circuit breaker state machine unit-tested through every transition
- [ ] `/v1/stats` returns real aggregates from real rows

---

## Day 4 — Stage 6: evaluation, dashboard, README

**Morning** (`eval/run_eval.py`) — the real deliverable. Runs the eval set
through ModelMux and through a baseline that sends everything to the large tier,
then reports cost, latency and quality side by side.

**Also measure the D3 bias.** `cost_if_large_usd` reprices *observed* token
counts at baseline rates — it is "same tokens, baseline prices", not a true
counterfactual. Use `/v1/compare` on a sample to measure how wrong that is, and
report it next to the headline savings. **A savings figure published without
this caveat is a false claim.**

**Afternoon** — dashboard (see Part 4 for scope) and the README results table.

**GATE — Day 4**
- [ ] README results table has **real measured numbers, zero `TBD`**
- [ ] Quality reported alongside cost — savings mean nothing if answers got worse
- [ ] The cache-leakage limitation documented in the README (SPEC §10)
- [ ] Cost figures dated

---

# Part 4 — What to cut, and what not to

2.3x compression has to come from somewhere. My recommendations, ranked by how
little they cost:

| Cut | Impact | Recommendation |
|---|---|---|
| **k6 load testing** | Low — mock-only anyway, proves little | **Cut.** Note as not-done. |
| **`GET /v1/stream` (SSE)** | Low — the feed can poll | **Cut.** Polling every 2s is indistinguishable at this scale. |
| **React + Vite + Recharts dashboard** | Medium — it is the demo surface | **Reduce.** One static HTML page hitting `/v1/stats`, no build step. Saves most of a day. |
| **`POST /v1/compare`** | High — it is how the D3 bias gets measured | **Keep.** |
| **Held-out classifier split** | High — without it, accuracy is self-reported | **Keep.** |
| **The loose-threshold wrong-answer test** | High — the honesty counterweight | **Keep, always.** |

### For a 3-day version

Drop Day 4's dashboard entirely and merge the eval harness into Day 3's
afternoon. You end with a fully working router, real measured numbers, and no
visual demo. **That is the right trade** — the numbers are the project; the
dashboard shows them off.

What I would *not* drop under any compression: the held-out split, the cache
failure-case test, and the D3 bias caveat. Each is what separates a measured
result from a marketing claim.

---

# Part 5 — Standing risks

- **The results table is hostage to blocker 1.** Everything else survives
  without keys; this does not.
- **My hand-labels are one person's judgement.** Every accuracy number in this
  project is measured against them. Worth having someone else label 20 prompts
  blind and checking agreement — if it is poor, the metric is softer than it
  looks.
- **Free-tier rate limits** will bite during evaluation runs. Cache results
  locally so a re-run does not re-spend.
- **Model names and prices drift.** Both are unverified today. Date them and
  re-check before quoting any cost figure.
- **The 20ms and 30ms budgets are real constraints, not aspirations.** If
  embedding blows the classification budget, that is a finding to record, not a
  number to quietly relax.
