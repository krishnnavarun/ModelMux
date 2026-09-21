# 11 — Cheat sheet (read this last, before you walk in)

## The pitch, 30 seconds

> ModelMux is a cost-aware LLM router. It classifies every prompt by difficulty
> in **12 milliseconds with no LLM call**, and sends it to the cheapest tier
> that can handle it. A semantic cache in front serves rephrasings, not just
> exact matches. Every routing decision is logged with the signals behind it.

## The numbers

| | |
|---|---|
| Classification | **11-12ms** (budget 20ms) — *on an idle process* |
| Cache lookup | **11.2ms** at 10k entries (budget 30ms) |
| Heuristic accuracy, held-out n=32 | **88%** |
| Shipped (hybrid) | **72%, zero too-cheap** |
| Cache hit rate / false hits | **80% / 0%** |
| Latency | **2,803ms vs 6,973ms p50 — 2.5× faster** |
| Cost, same-token routing effect | **3-4%** · projected **37.1%** |
| Cost, A/B across two runs | 6.6% then **1.7%** — noise-dominated |
| Tier thresholds | **0.33 / 0.66** |
| Cache threshold | **0.88**, measured |
| Rate limit | **60/min**, per IP |
| Circuit breaker | **5 fails / 60s → open · 30s cooldown · 1 probe** |
| Retries | **2** (3 attempts), jittered, base 0.5s |
| Timeouts | **5s connect / 30s read** |
| Tests · decisions · deps | **131 · 35 · 9** |

## The five non-negotiables

1. **No LLM call in classification** — defeats the purpose
2. **< 20ms classify, < 30ms cache** — must vanish against a 2s model call
3. **Every decision explainable** — `signals` is never dropped
4. **Nothing silently dropped** — every failure writes a row
5. **Config over code** — tiers, prices, thresholds in `config.yaml`

## The five things that make it interesting

1. **The less accurate classifier ships** — 72% over 88%, because it makes
   zero too-cheap misroutes
2. **The eval harness refuses to produce numbers from mock data**
3. **The measured cost saving didn't reproduce** (6.6% → 1.7% on identical
   prompts) and the write-up says so — savings are bounded by the price
   spread, and below a certain spread they're unmeasurable by A/B
4. **A weeks-long 401 was an env var shadowing `.env`** — found via "0 API
   calls"
5. **HTTP 200 with empty content is an error** — the cheapest answer is silence

## The limitations — volunteer these

- **Quality has never been graded.** The harness exists; nobody filled one in.
- **One provider key.** The large tier is a Groq fallback; 37% is a projection.
- **Hand-labels by one person** — the same person who built the classifier.
- **n=32.** One prompt is 3 points.
- **11-12ms was an idle process**, and the test guarding it flakes.

## Best stories

| Asked about | Tell |
|---|---|
| A bug you found | **The 401 → env shadowing.** "0 API calls rules the key out" |
| Being wrong | **Numbers wrong by 40×.** "A ratio survives a bug that kills both terms" |
| API design | **200 with empty content.** "The cheapest answer is silence" |
| A limit of tuning | **No cache threshold is safe.** 0.99 similarity, opposite answers |
| Disagreeing with a spec | **Cut React** (D24). One file, no build step |
| Handling uncertainty | **The GIL probe.** Recorded the cause as unknown |

## Sentences worth memorising

> "A single accuracy number would have ranked the heuristic first and hidden
> that it sends hard prompts to weak models."

> "A router's savings are bounded by the price spread it's given. Perfect
> classification earns nothing on a flat ladder."

> "A plausible fake in a results table is worse than no number at all."

> "The cheapest possible answer is the one that says nothing — a
> cost-optimising router must not be paid for it."

> "A known-unverified number in a load-bearing position is a bug with a
> comment on it."

> "The instrument gets less scrutiny than the thing it measures, and it's the
> instrument that decides what you believe."

> "Embedding similarity measures topical closeness, not semantic equivalence.
> Negation barely moves a vector."

## Commands

```powershell
# run it
C:\dev\modelmux-venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --reload

# tests
C:\dev\modelmux-venv\Scripts\python.exe -m pytest tests/ -q

# explain one routing decision (free, no key)
C:\dev\modelmux-venv\Scripts\python.exe eval/explain.py "your prompt here"

# classifier accuracy, both sets + overfitting gap
C:\dev\modelmux-venv\Scripts\python.exe eval/run_classifier_eval.py

# the full live eval  (flush the cache first!)
redis-cli FLUSHDB
C:\dev\modelmux-venv\Scripts\python.exe eval/run_eval.py --set holdout.json

# grade quality — needs no API key
C:\dev\modelmux-venv\Scripts\python.exe eval/grade_quality.py eval/results/spotcheck-<stamp>.json
```

## Three sets — never confuse them

| File | n | Role |
|---|---|---|
| `prompts.json` | 50 | **Tuned against.** Training score. Don't quote. |
| `labelled.json` | 60 | k-NN reference examples. Not a test set. |
| `holdout.json` | 32 | **Never tuned on. Quote this one.** |

## If you blank

Fall back to the request lifecycle — it structures everything:

**rate limit → validate → cache → classify → dispatch → cost → log**

Every component, every number and every decision hangs off one of those seven
steps.

## The closing line

> "The code is a few thousand lines anyone could write. The thing I'd want to
> be judged on is `DECISIONS.md` — thirty-five choices with the alternatives,
> the reasoning, and what each one costs, including the ones where I was wrong
> and reversed them."
