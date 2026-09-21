# 08 — The question bank

Grouped by topic, roughly easy → hard within each. **Answer out loud.** Reading
an answer you agree with feels like learning and isn't.

---

## A. The project itself

**Q1. What is ModelMux, in one sentence?**
> A cost-aware LLM router: it classifies every incoming prompt by difficulty
> and dispatches it to the cheapest model tier that can handle it, with a
> semantic cache in front and full logging behind.

**Q2. Why not just let developers choose the model?**
> Because it pushes a guess onto the caller at request time, and the safe guess
> is always the biggest model. Manual selection degrades to "everything
> expensive" within a sprint. Removing the decision is the product.

**Q3. What's the hardest constraint in the design?**
> **No LLM call in the classification path.** Calling a model to decide which
> model to call defeats the purpose on both cost and latency. That forces the
> classifier to be heuristics plus a small local embedding model, inside a
> 20ms budget on CPU. Anyone can classify prompts with GPT-4; doing it in 12ms
> with no network call is the actual engineering problem.

**Q4. How do you know it works?**
> Three separate measurements. Classification accuracy against a **held-out**
> set of 32 prompts never used for tuning — 88% for the heuristic. Cache hit
> rate of 80% with zero false hits on deliberately confusable pairs. And a
> live run: 2.5× faster at the median, 6.6% cheaper.
>
> And one thing I *don't* know: **nobody has graded answer quality.** Cost
> savings mean nothing if the cheap answers are worse, and that column is
> empty.

**Q5. What would you do next?**
> Three things in order. Grade the quality — the harness exists and needs no
> API key. Get a second provider key so the tier ladder isn't compressed.
> Then train a learned router on the logged routing outcomes, since every
> request already writes its signals and its tier to the database — that table
> is a training set accumulating by itself.

---

## B. The classifier

**Q6. Walk me through classifying a prompt.**
> Extract signals from the raw text — token count, reasoning markers, code
> presence, whether it's multi-part, whether it has the shape of a factual
> lookup, the ratio of question to pasted context. Score them with fixed
> weights, reasoning weighted highest at 0.34. Then apply floors: if a hard
> reasoning marker is present, the score can't fall below 0.75 regardless of
> the average. In parallel, embed the question and take a 5-nearest-neighbour
> vote against 60 labelled examples. Reconcile the two, escalating on
> disagreement. Map the final score through thresholds at 0.33 and 0.66.

**Q7. Why floors instead of just weights?**
> Because an average dilutes a strong signal. "Prove Fermat's Last Theorem" is
> six tokens — the length component drags the average down and it routes to the
> cheapest model, which is exactly the failure mode the classifier exists to
> prevent. A floor lets the strongest single indicator override the average.
> **An average is the wrong aggregator for evidence of difficulty.**

**Q8. Your shipped classifier scores 72% and you have one that scores 88%. Explain.**
> *(This is the question. See `06`, D15.)*
> The 88% heuristic makes **4 too-cheap** misroutes. The 72% hybrid makes
> **zero**, at the cost of 9 too-expensive ones. Those errors aren't equally
> bad: too cheap means a hard question answered badly by a weak model, which
> the user sees and doesn't forgive. Too expensive means overpaying by a
> fraction of a cent. **A single accuracy number ranks the heuristic first and
> hides that it sends hard prompts to weak models.** It's a values decision,
> it's documented, and it's one config line to reverse.

**Q9. How do you know you're not overfitting?**
> I caught myself doing it. The weights were tuned on the same 50 prompts I was
> reporting accuracy on — a training score dressed as a result. So I wrote a
> 32-prompt held-out set *after* tuning finished, which has never influenced a
> single constant. The eval script prints both columns side by side, and **the
> gap between them is the size of the overfitting**, stated as a number rather
> than a worry.

**Q10. n=32 is small. Doesn't that undermine everything?**
> Yes, and it's stated in the README: one prompt is three percentage points, so
> these figures have wide error bars. What it's adequate for is detecting the
> *direction* of an error — zero too-cheap versus four is a real difference in
> behaviour, not a rounding artefact. What it can't support is ranking two
> classifiers four points apart, so I don't.

---

## C. The cache

**Q11. Why semantic caching rather than exact-match?**
> "What's the capital of France?" and "capital of France?" are the same
> question. Exact matching serves neither from the other. We embed the prompt
> and serve on cosine similarity, so rephrasings hit.

**Q12. That sounds dangerous. How do you stop it serving the wrong answer?**
> It **is** dangerous, and it's the most dangerous setting in the project.
> Three mitigations: a threshold of 0.88 that was measured by sweeping against
> 35 labelled pairs rather than guessed; a lexical inversion guard for
> negations and antonyms, because embedding similarity is topical closeness,
> not semantic equivalence — I measured a pair at **0.99 similarity with
> opposite correct answers**; and a `bypass_cache` flag for callers who need a
> fresh answer.
>
> And it's still an inherent risk of shared caching, not a solved problem —
> there's a test that deliberately produces a wrong answer at a loose threshold
> so the failure mode is demonstrated rather than described.

**Q13. Why not use a vector database?**
> At 10,000 entries a brute-force cosine scan over float32 vectors takes 11.2ms
> against a 30ms budget. A vector database is a dependency, an operational
> surface and a thing to explain, bought to solve a problem I don't have. I'd
> add one at roughly 10⁵–10⁶ vectors, when the linear scan stops fitting the
> budget — and then I'd be accepting *approximate* recall in exchange, which is
> its own tradeoff.

**Q14. TTL of 24 hours and a max of 10,000 entries. Why both?**
> They bound different resources. TTL bounds **staleness** — a cached answer
> about a changing world shouldn't live forever. The entry cap bounds
> **memory**, and keeps the linear scan inside its latency budget.

---

## D. Resilience

**Q15. Explain the circuit breaker.**
> Three states. **Closed** is normal. Five failures within a 60-second window
> opens it, and while **open** every request is rejected immediately without
> calling the provider. After a 30-second cooldown it goes **half-open** and
> allows exactly **one** probe: success closes it, failure re-opens it and
> **restarts the cooldown**.
>
> Three details people get wrong: the threshold is failures *within a window*,
> not ever — a provider that failed three times last month isn't unhealthy now.
> Half-open allows exactly one probe, because letting several through is the
> thundering herd the breaker exists to prevent. And a failed probe must
> restart the cooldown rather than grant another attempt. **We shipped that
> last one wrong once, letting two probes through.**

**Q16. What's the point, if you're going to fail the request anyway?**
> It fails **fast and cheap** instead of slow and expensive. Without it, every
> request spends its full timeout — 30 seconds — discovering the same outage,
> which ties up connections and cascades back to your own callers. It also
> stops you hammering a service that's trying to recover.

**Q17. Which errors do you retry?**
> A four-way taxonomy. Timeout, rate-limited and server-error are transient and
> retried, with rate-limited honouring `Retry-After`. **Bad request is never
> retried** — it's deterministic. Retrying a dead API key gives you three
> identical 401s and triples your latency. Making retryability a property of
> the error *type* means the call site never has to guess.

**Q18. Why jitter the backoff?**
> Without it, every client that failed together retries together, re-triggering
> the overload they're backing off from. Jitter spreads the retry storm.

**Q19. You escalate a tier when one is exhausted. Isn't that dangerous?**
> Yes, which is why it's a config switch rather than a default. During a
> provider outage it converts an availability problem into a **cost spike** —
> everything lands on the most expensive tier. That should be a deliberate
> choice per deployment, so it's `escalate_on_tier_exhausted` in config.

---

## E. Measurement and honesty

**Q20. Your savings figure has a known bias. Explain it.**
> `cost_if_large_usd` reprices **the tokens actually observed** at baseline
> rates. That's "same tokens, baseline prices" — but a larger model usually
> answers at a different length, so it's not truly what the big model would
> have cost. I couldn't eliminate the assumption without paying for every
> expensive call, so I did two things: the caveat travels **inside the
> `/v1/stats` payload** so no dashboard can drop it, and `/v1/compare` runs
> both and reports the real ratio. **Measured at 1.04×** — the baseline was 4%
> more verbose, so the figure slightly understates savings.

**Q21. Why does your eval harness refuse to run?**
> Against mock providers it refuses unless you pass `--simulated`, and then it
> prefixes every line with `SIMULATED`. **A plausible fake in a results table
> is worse than no number at all** — a missing number prompts a question, a
> wrong one gets quoted.
>
> And it still wasn't enough: it published figures inflated 40× from *real*
> data, because it priced every call at the tier's first provider while a
> fallback answered them. Guarding one path doesn't guard the thing.

**Q22. Your headline saving is 6.6%. That's not very impressive.**
> It isn't, and the reason is the most interesting thing I measured. Only one
> API key worked, so the large tier falls back to the same model the mid tier
> uses. The ladder compresses to 2×, and the router sent 16 of 32 prompts to
> "large" — where routed and baseline are byte-for-byte the same call. Half the
> traffic had nothing to save.
>
> **A router's savings are bounded by the price spread it's given. Perfect
> classification earns nothing on a flat ladder.** The projection at the
> configured ladder is 37%, and it's labelled as a projection because no
> Anthropic call was ever made. The stronger measured result was latency: 2.5×
> faster at the median, from a project framed entirely around cost.

**Q23. Why is p95 unchanged while p50 halved?**
> Because the tail is the hard prompts, and they route to the big model either
> way. Routing improves the **typical** request and leaves the worst case
> alone. That's the expected shape, and if p95 *had* improved I'd want to know
> why before believing it.

---

## F. System design / scaling

**Q24. A thousand requests per second. What breaks first?**
> Three things, in order.
>
> **The rate limiter and circuit breaker are per-process**, so with N workers
> the effective limit is N× the intended rate and each worker keeps its own
> view of provider health. Both need to move to Redis.
>
> **Classification is synchronous CPU work on the event loop.** At 12ms per
> request, one worker saturates around 80 rps and then starts adding latency to
> everything else. It belongs in a thread or process pool.
>
> **The cache's linear scan** is 11.2ms at 10k entries and grows linearly. At
> 100k it blows the budget and needs an ANN index.
>
> SQLite would also need to become Postgres — it's a single-writer database and
> the spec always said so.

**Q25. How would you add a new provider?**
> One file in `providers/`, implementing one method, translating that API's
> shape into `ProviderResult` and mapping its failures onto the four-error
> taxonomy. Then a config entry. No change to the router, the resilience layer
> or anything else — that's what the ABC buys.

**Q26. How would you make the routing decisions better over time?**
> Every request already writes its signals, its chosen tier, its cost and its
> outcome to the database. **That table is a training set accumulating by
> itself.** The next step is a learned router — logistic regression or a small
> gradient-boosted model over the same signals — trained on logged outcomes.
> The blocker isn't the model, it's the **label**: I'd need a quality signal
> per request to know whether a cheap route was actually good enough, which is
> the same gap as the ungraded quality column.

**Q27. How do you handle a provider changing its prices?**
> Config, not code — that's one of the five non-negotiables. But the deeper
> answer is that `cost_usd` is **written into each database row at request
> time** rather than computed later, so historical costs stay correct when
> prices change. Recomputing history at today's prices would silently rewrite
> every past result.

---

## G. Questions about *you*

**Q28. What was the hardest part?**
> Technically, the cache threshold — discovering that no value was safe, and
> that the answer was a second mechanism rather than a better number.
>
> Honestly though, the hardest part was a 401 that lasted weeks and turned out
> to be an environment variable shadowing `.env`. *(Story 1.)*

**Q29. What would you do differently?**
> Grade quality first, not last. The whole project is a claim that cheaper
> routing is acceptable, and I built every measurement *except* the one that
> tests the claim. I also had the blind-grading export sitting there for weeks
> with nothing able to read it back, which looked like the problem was handled.

**Q30. What are you most proud of?**
> `DECISIONS.md`. Thirty-five decisions, each with the alternatives, the
> reasoning, and **what it costs** — including the ones where I was wrong and
> reversed. The code is a few thousand lines anyone could write. The file that
> says *why* is the part I'd want to be judged on.

**Q31. What's still broken?**
> Two things, both written down. Quality is ungraded. And a performance test
> flakes in the full suite — 85ms against a 20ms budget — where I hypothesised
> torch thread contention, wrote a probe that measured the GIL instead, and
> recorded the cause as **unknown** rather than shipping the guess as an
> explanation.

---

## H. Rapid fire

| Q | A |
|---|---|
| Why FastAPI? | I/O-bound waiting on provider calls; async is the right shape |
| Why no ORM? | One table |
| Why SQLite? | One file, zero setup, real SQL; Postgres later |
| Why WAL mode? | Dashboard reads while requests write |
| Why is the DB outside the repo? | OneDrive + SQLite locks + WAL companions = corruption risk |
| Why Redis from WSL2? | No supported native Windows build |
| Why MiniLM? | 384 dims, CPU, ~10ms — fits a 20ms budget |
| Why k=5? | Odd, so a majority vote can't tie |
| Why float32 vectors? | Half the memory of float64, no useful precision lost |
| Why cap embedding at 400 chars? | 55ms → ~10ms, **and** it stopped measuring pasted context |
| Why token-bucket rate limiting? | Allows bursts while bounding the average |
| Why not trust `X-Forwarded-For`? | Caller-controlled — anyone could pick their own bucket |
| Why check the rate limit first? | A rejected request should cost nothing |
| Why percentiles from successes only? | A 30s timeout isn't a slow answer, it's no answer |
| Why does `/health` skip providers? | Their outage would read as your death and get your pods killed |
| Why isn't `/v1/compare` in the stats? | It costs two calls; it would corrupt the numbers it audits |
