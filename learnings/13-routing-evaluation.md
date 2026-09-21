# Routing, Classification & Evaluation

The domain logic of this project — and the part an interviewer will dig into
hardest, because it's where judgement shows.

---

## 1. What it is

**Model routing** — deciding, per request, which model should answer it, so
cheap models handle easy work and expensive models handle hard work.

**Classification** — estimating how difficult a prompt is, cheaply enough that
the estimate costs less than what it saves.

**Evaluation** — measuring whether the router actually helps, on both cost *and*
quality.

---

## 2. How it works

### The economics

An application that sends everything to its most capable model pays premium
prices for trivial work. *"What is 15% of 240?"* and *"Refactor this 400-line
auth module"* cost the same, and only one needs a frontier model.

Rates in this project span **60x** between small and large tiers.

### The constraint that shapes everything

> **The routing decision must be cheaper than the request it routes.**

That single rule eliminates the obvious approach — asking an LLM which model to
use. If classification costs an LLM call, you've paid the thing you were trying
to avoid. So classification must be **heuristics plus, at most, a small local
CPU model**, inside a ~20ms budget.

### Two classification approaches

**Heuristic / rule-based** — extract features, combine with weights.

```
signals: token_count, has_code, reasoning_markers, multi_question, ...
score = Σ (weight_i × signal_i)  ->  0-1
```

*Fast, fully explainable, no training data. Brittle, and needs hand-tuning.*

**Embedding / similarity-based** — embed the prompt, compare to labelled
examples, take a majority vote over the k nearest.

*Generalises beyond hand-written rules. Needs labelled data, is slower, and is
harder to explain.*

**Hybrid** — run both and reconcile. On disagreement, **take the higher tier**:
fail towards quality.

### The asymmetry that governs the design

Two kinds of error, and they are **not equally bad**:

| Error | Cost |
|---|---|
| **Routed too cheap** | a wrong or weak answer — **damages trust** |
| Routed too expensive | wasted money — **annoying but recoverable** |

> *A wrong cheap answer costs more in trust than a wrong expensive one costs in
> money.*

So a router should be biased towards escalation, and any accuracy figure must be
reported **by direction**, not as a single number.

### Evaluating a classifier

- **Labelled set** — prompts with a known correct tier
- **Held-out split** — measure on data you did not tune on, or you're measuring
  memorisation
- **Baseline** — always compare against something. "80% accurate" means nothing
  without knowing what always-guessing-small scores.
- **Per-class breakdown** — an aggregate hides which cases fail

---

## 3. How we used it in ModelMux

### Stage 2: build the dumb version and measure it

The naive rule routes on **token count alone**. Longer prompt → more expensive
tier. Everyone can see the flaw immediately: *length is not difficulty.*

**So why build it?** Because "everyone can see the flaw" is not the same as
knowing its shape. Before running it I could have told you it would misroute. I
could not have told you:

```
Prompts: 50
  correct         17  (34%)
  TOO CHEAP       28  (56%)   <- quality risk
  too expensive    5  (10%)   <- cost waste

Tier distribution chosen: {'small': 45, 'mid': 5}
```

### Three findings

**1. The failure is asymmetric and backwards.** The spec says fail towards
quality. The rule fails towards cost, **28:5** — badly, in precisely the
direction identified as least acceptable.

**2. 15/15 where length and difficulty agree; 0/32 where they diverge.**

| Agree | Diverge |
|---|---|
| lookup 6/6 | reasoning 0/5 |
| trivial_math 3/3 | summarize 0/2 |
| code_simple 4/4 | multi_question 0/2 |
| formatting 2/2 | long_and_hard 0/5 |
| | trap_short_but_hard **0/11** |
| | trap_long_but_easy **0/5** |

That is not a rule approximating difficulty badly. It is a rule measuring a
**different quantity** that correlates only in easy cases. 34% *sounds* like it
half-works; the split shows it gets the easy half free and the rest wrong.

**3. The large tier was never selected once.** Not designed into the experiment —
it fell out of running it. Reaching `large` needs >396 tokens; nothing realistic
is that long. **The tier every `cost_if_large_usd` is priced against is dead
code under this rule.**

### The temptation I refused

I could have lowered `saturation_tokens` until prompts started reaching `large`
and the accuracy number improved. **I didn't**, because that pushes prompts up by
*length* — still the wrong reason. It would produce a better-looking metric while
the mechanism stayed exactly as broken, and destroy the evidence Stage 3 is built
from.

> **Resist tuning a knob until the metric looks better without the mechanism
> improving.** A misleading number is worse than an obviously bad one, because
> the obviously bad one still tells the truth.

Pinned as a test instead: `test_large_tier_is_effectively_unreachable`.

### Score abstraction, so Stage 3 is a drop-in

```python
def score_from_tokens(token_count: int, saturation_tokens: int) -> float:
    return min(token_count / saturation_tokens, 1.0)

def tier_for_score(score: float, thresholds: dict) -> str:
    if score <= thresholds["small_max"]: return "small"
    if score <= thresholds["mid_max"]:   return "mid"
    return "large"
```

Token count is normalised into a **0-1 score**, then bucketed by the thresholds
already in config. The alternative — token thresholds directly in config — works
today and must be torn out at Stage 3.

**Saturation** exists because past a certain length, longer tells you nothing
extra: a 2,000-token and a 20,000-token prompt are both just "long". Without a
ceiling, length would dominate any future weighted blend purely by having an
unbounded range.

Boundaries are **inclusive at the top** — a score exactly `small_max` routes
small. Cheaper on ties.

### The evaluation harness costs nothing to run

```
python eval/run_routing_check.py
```

**No provider is called and no money is spent.** Routing is a pure function of
the prompt, so measuring it needs no network — which is why it runs today while
the API key is still invalid.

It reports accuracy, **direction of error**, and a per-category breakdown —
because the aggregate would have hidden the 15/15 vs 0/32 split entirely.

### A signal the spec didn't have

All five `trap_long_but_easy` failures share a shape: **a long pasted block plus
one short trivial question.** A 1,400-word diary entry ending *"what day of the
week was it?"* is not hard — it's an easy question with context attached.

Total length is the wrong measure; the *question* is what needs answering. So:
a **question-to-context ratio** signal, absent from the spec, discovered by
running the experiment.

**The experiment paid for itself beyond its stated purpose** — it didn't just
measure the known flaw, it found a requirement nobody had written down.

---

## 3b. Stage 3 — the real classifier, and what measuring honestly cost

### The result

Held-out (n=32), after building signals, a weighted scorer, and a k-NN
embedding classifier:

| mode | accuracy | too cheap | too expensive |
|---|---|---|---|
| naive_tokens | 31% | 20 | 2 |
| heuristic | **88%** | 4 | 0 |
| embedding | 72% | 0 | 9 |
| **hybrid (shipped)** | 72% | **0** | 9 |

### The mistake that mattered most

Partway through I realised I had been **tuning weights against the same 50
prompts I was reporting accuracy on.** That is a training score. It measures how
well constants were fitted to those 50 examples, not how the classifier behaves
on anything new.

I stopped, wrote `holdout.json` *after* tuning finished, and now report both
plus the gap.

**Three sets now, and confusing them would invalidate everything:**

| set | role | can it measure generalisation? |
|---|---|---|
| `prompts.json` | tuned against | **No** — training score |
| `labelled.json` | embedding k-NN reference data | **No** — training data |
| `holdout.json` | written after tuning, never used | **Yes** |

### The gap came out NEGATIVE, and that needs care

Heuristic scored 78% on the tuned set and **88%** on held-out — better on data
it never saw.

That is *not* proof of excellent generalisation. The likelier explanation is
that **the two sets are not equally hard**: `prompts.json` was built
adversarially, stuffed with traps designed to break length-based routing;
`holdout.json` has an ordinary mix.

So the gap does not cleanly measure overfitting and I cannot claim it does. What
it shows is the absence of *catastrophic* memorisation. A real answer needs sets
of matched difficulty, labelled by someone else.

### Why hybrid ships despite being less accurate

```
heuristic   88%   4 too cheap,  0 too expensive
hybrid      72%   0 too cheap,  9 too expensive
```

Hybrid eliminates every quality risk by over-routing 28% of prompts. The
project's stated value is that a wrong cheap answer costs more in trust than a
wrong expensive one costs in money — so the trade is correct by its own rules.

> **A single accuracy number would have ranked heuristic first and hidden that
> it sends hard prompts to weak models.** Report error direction, always.

### The four ideas that produced the jump

1. **Graded markers** — `prove`/`derive` (hard) vs `compare`/`debug` (medium).
2. **Floors, not a pure average** — difficulty is not an average; a prompt is
   hard if any ONE strong indicator fires. Averaging gave "Prove Fermat's Last
   Theorem" a score of 0.18.
3. **`floor + (1-floor) x weighted`** rather than `max()` — keeps length
   information instead of discarding it once a floor fires.
4. **Measure signals on the question, not the pasted context** — this bit twice,
   for reasoning markers and again for `multi_question`.

---

## 4. Interview questions

**Q: Design a system that routes requests between cheap and expensive LLMs.**
Classify difficulty cheaply — heuristics plus optionally a small local embedding
model, never an LLM call, since that would cost the thing you're saving. Map to
tiers by threshold. Bias towards escalation, because a wrong cheap answer costs
more than a wasted expensive one. Log every decision with its inputs so it's
explainable, and measure quality alongside cost.

**Q: Why not use an LLM to decide which LLM to use?**
Because the routing decision must be cheaper than what it routes. An LLM
classification call costs roughly what a small-tier answer costs, so you'd
eliminate the savings and add latency.

**Q: Your router is 34% accurate. Is that bad?**
It depends entirely on the breakdown. *Mine was 34% overall, but 15/15 where
length and difficulty agreed and 0/32 where they diverged — so it wasn't a weak
approximation of difficulty, it was measuring a different quantity. And 56% of
errors were in the dangerous direction, which matters more than the headline.*

**Q: What's your baseline and why does it matter?**
Always-route-large — maximum quality, maximum cost. Every savings claim is the
gap from that. Without a baseline, "we saved 60%" is unanchored. *I also compare
against always-small to check the router beats trivial strategies.*

**Q: How do you evaluate a classifier honestly?**
Label before you run, so results can't influence labels. Hold out a split you
don't tune on. Report per-class, not just aggregate. Compare against a trivial
baseline. And state who did the labelling — *mine are one person's judgement,
which is a real limitation of every number in the project.*

**Q: Why build a router you know is bad?**
To get a measured baseline and a specification. Before running mine I could not
have predicted the 28:5 error asymmetry, the 15/15 vs 0/32 split, or that the
large tier would never be selected at all. Those measurements became the
requirements for the real classifier — and the baseline that proves it helped.

**Q: You could improve your accuracy number by tuning one constant. Do you?**
Not if the mechanism hasn't improved. *Lowering my saturation constant would
have pushed prompts into the large tier by length — still the wrong reason — and
produced a better metric over an equally broken rule, while destroying the
evidence the next stage is built from.*

**Q: How do you know cost savings didn't come at the cost of quality?**
You measure both on the same evaluation set, or the cost number is meaningless.
Quality scoring options: an LLM-as-judge over the full set, a blind human
spot-check on a sample, or exact-match on factual subsets. Best is a sample of
each — *and if they disagree, that disagreement is itself a finding.*

**Q: You tuned your classifier on the set you're reporting accuracy on. What's
wrong with that, and what did you do?**
It is a training score — it measures fit to those examples, not generalisation.
*I caught it mid-build, wrote a held-out set after tuning was finished, and now
report both columns plus the gap. My held-out score came out higher, which I do
NOT claim as good generalisation: the two sets are not equally hard, since the
tuned one was built adversarially.*

**Q: Your simpler model beats your fancier one. Do you ship the fancy one?**
Only if the error profile justifies it. *My keyword heuristic scored 88% and the
hybrid 72% — but hybrid had zero too-cheap misroutes against the heuristic's
four. Since a wrong cheap answer costs more than a wasted expensive one, I
shipped the less accurate model and documented exactly why.*

**Q: What are the risks of caching LLM responses semantically?**
Cross-user leakage is the main one: if user A's prompt and answer are cached and
user B sends something semantically similar, B gets A's answer. That's inherent
to shared semantic caching, not a bug to fix — so it needs a documented
limitation, a per-request bypass, and a conservatively tuned similarity
threshold. Too loose and different prompts share answers; too tight and hit rate
collapses.
