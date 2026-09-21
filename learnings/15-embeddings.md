# Embeddings & Semantic Similarity

---

## 1. What it is

An **embedding** is a fixed-length list of numbers representing a piece of text,
arranged so that texts with similar meaning get similar numbers.

**sentence-transformers** is the Python library that produces them.
**`all-MiniLM-L6-v2`** is the specific model we use: 6 transformer layers, ~22M
parameters, **384 dimensions**, ~80MB, runs on CPU.

**Cosine similarity** is how you compare two embeddings.

---

## 2. How it works

### From text to vector

```
"What is the capital of Japan?"  ->  [0.021, -0.113, 0.087, ...]  384 floats
"Japan's capital city is?"       ->  [0.019, -0.109, 0.091, ...]  ← close
"Prove Fermat's Last Theorem."   ->  [-0.204, 0.331, -0.055, ...] ← far
```

Mechanically: a transformer encoder emits one vector per token; **mean pooling**
averages them into a single sentence vector. Training used a **contrastive
objective** — paraphrase pairs pulled together, unrelated pairs pushed apart —
so the geometry encodes meaning rather than spelling.

### Cosine similarity

The angle between two vectors, ignoring their length:

```
cos(a, b) = (a · b) / (|a| × |b|)      range -1 to 1
```

- `1.0` — same direction, semantically near-identical
- `0.0` — unrelated
- negative — rare in practice for sentence embeddings

**The optimisation that matters:** if you *normalise* every vector to unit
length in advance, `|a| = |b| = 1`, so cosine similarity collapses to a plain
dot product. One matrix multiply, no per-request norm computation.

### k-nearest-neighbour classification

Embeddings alone classify nothing. The classifier is:

1. Embed a set of hand-labelled examples once
2. Embed the incoming prompt
3. Compute similarity against all examples
4. Take the **k** most similar
5. Majority label wins; **confidence** = fraction agreeing

No training step — this is *instance-based* learning. Adding an example is
adding a row.

### Cost model

| | |
|---|---|
| Model download | ~80MB, once |
| Model load | seconds |
| Encode one short sentence | ~10ms CPU |
| **Encode grows with sequence length** | this bites |
| Network per request | **none** |
| Money per request | **none** |

`all-MiniLM-L6-v2` **truncates at 256 tokens.** Longer input is silently
clipped — an important detail, not a footnote.

---

## 3. How we used it in ModelMux

### Why an embedding model is allowed at all

SPEC section 2 forbids an LLM call in the classification path — the routing
decision must be cheaper than the request it routes.

**An embedding model is not an LLM call.** It is a 22M-parameter encoder running
locally: no network, no per-token billing, ~10ms. A matrix multiply, not a
generation. That distinction is what makes the whole design legal.

### What it adds over keyword matching

`heuristic.py` matches a marker list. That works — it took accuracy from 34% to
88% on held-out data — but it can only catch phrasings somebody thought to list.

```
"Is mathematics discovered or invented? Defend your position."
```

`defend` is in the list. Change one word:

```
"Do numbers exist independently of human minds, or do we make them up?"
```

No marker fires. But the **embedding lands near the other philosophy-of-
mathematics examples**, sharing no vocabulary with them. That generalisation is
the entire justification for the extra 10ms.

### Setup: load once, at startup

```python
# app/classifier/embedding.py
def init() -> None:
    _model = SentenceTransformer(MODEL_NAME)
    _vectors = _model.encode(prompts, normalize_embeddings=True)
```

Both halves are expensive and neither changes per request. Doing either
per-request would blow the 20ms budget by orders of magnitude — model load alone
measured **10.5 seconds**.

`normalize_embeddings=True` is the optimisation above: cosine becomes a dot
product.

### The query

```python
similarities = _vectors @ query          # one matmul, all 60 examples
top_k = similarities.argsort()[-k:][::-1]
```

`argsort` is ascending, so the **last** k are the most similar.

### Ties break upward

```python
best_count = max(counts.values())
winners = [t for t, c in counts.items() if c == best_count]
tier = max(winners, key=lambda t: order[t])
```

A 2-2-1 split between small and mid must not silently pick the cheap side just
because it appeared first. SPEC 8.4 rule 3 — fail towards quality.

### The bug that cost 45ms: embedding the wrong text

First version embedded the **whole prompt**. Measured latency:

```
short   p50 15.4 ms   p95 33.6 ms
long    p50 55.0 ms   p95 83.9 ms     <- budget is 20 ms
```

The fix was to embed only the **question**, not the pasted context:

```python
target = signals.question_part(prompt)
```

```
long    p50 10.9 ms   p95 14.6 ms     <- 55ms -> 11ms
```

**It was also more correct, which is the interesting part.** The labelled
examples are questions. Comparing them against a prompt that is 95% pasted diary
entry measures similarity to *the diary*. And since MiniLM truncates at 256
tokens, a long prompt was *already* being clipped — at an arbitrary point,
keeping the context and discarding the trailing question. Embedding the question
made a choice that the model was otherwise making badly on our behalf.

> **The lesson: when a performance fix and a correctness fix are the same
> change, you have probably found the real bug.** The slowness was a symptom of
> feeding the model text that was never relevant.

### The measured result — and the honest reading

Held-out accuracy (n=32):

| mode | accuracy | too cheap | too expensive |
|---|---|---|---|
| heuristic | **88%** | 4 | 0 |
| embedding | 72% | **0** | 9 |
| hybrid | 72% | **0** | 9 |

**The embedding classifier is less accurate than the keyword heuristic.** That
is not the result I expected, and it is worth saying plainly rather than burying.

But it **never routes too cheap** — zero quality risks — because it over-escalates.
Given the project's stated value that a wrong cheap answer costs more than a
wrong expensive one, `hybrid` ships despite being 16 points less accurate.

**Why the embedding classifier underperforms here** is worth understanding:

1. **Only 60 labelled examples.** k-NN quality scales with reference density.
   60 points in 384 dimensions is extremely sparse.
2. **Tier is not really a semantic property.** Two prompts can be about the same
   topic and need different models. Embeddings capture topic and phrasing far
   better than difficulty.
3. **My labels are one person's judgement** — the same person who wrote the
   examples.

The honest conclusion is not "embeddings do not work" but "60 examples is not
enough data, and difficulty may not be what embedding distance measures."

---

## 4. Interview questions

**Q: What is an embedding?**
A fixed-length vector representing text, arranged so semantic similarity becomes
geometric proximity. Produced by a transformer encoder plus pooling, trained
contrastively so paraphrases land near each other.

**Q: Why cosine similarity rather than Euclidean distance?**
Cosine compares direction and ignores magnitude, and for text embeddings
magnitude often tracks length or word frequency rather than meaning. On
normalised vectors the two are monotonically related, so the practical answer is
that cosine reduces to a dot product — one matmul.

**Q: How would you build a classifier from embeddings without training a model?**
k-nearest-neighbour: embed labelled examples once, embed the query, take the k
most similar, majority vote. Instance-based — no training step, and adding an
example is adding a row. *Confidence comes free as the fraction of neighbours
agreeing, which is what my low-confidence escalation acts on.*

**Q: How do you pick k?**
Small k is sensitive to noise; large k blurs class boundaries and, with few
examples, starts including everything. Odd numbers avoid ties in binary cases.
*I used k=5 against 60 examples — with only 20 per tier, larger k would have
started voting with genuinely unrelated prompts.*

**Q: Your embedding classifier is less accurate than a regex-based heuristic.
What does that tell you?**
Three possibilities, and I'd check them in this order: too few reference
examples for k-NN in high dimensions; the target property not being what
embedding distance measures — difficulty is not topic; or label noise. *In my
case it is mostly the first two: 60 examples, and two prompts on the same
subject can need completely different models.*

**Q: What's the latency cost, and how do you keep it acceptable?**
Model load is seconds and encode is ~10ms, so you load once at startup and never
per request. Encoding cost grows with sequence length — *I cut a long-prompt
classification from 55ms to 11ms by embedding only the trailing question instead
of the entire pasted context, which was also more correct.*

**Q: What does it mean that the model truncates at 256 tokens?**
Anything longer is silently clipped, so the tail of a long document never
reaches the model. That is a correctness trap: you get a plausible embedding of
the wrong half. Either chunk deliberately, or select the span you actually care
about — never let the truncation choose for you.

**Q: When would you use an embedding model instead of an LLM?**
When you need similarity, clustering, retrieval, or classification rather than
generation. It is orders of magnitude cheaper, runs locally with no per-token
cost, and is deterministic. *In my project it is the only reason a semantic
classifier is permissible at all — the spec forbids an LLM call in the routing
path.*

**Q: How does this relate to RAG?**
Same primitive. RAG embeds documents, embeds the query, retrieves the nearest
chunks, and stuffs them into a prompt. My use is the same machinery with a
different endpoint — nearest labelled examples voting on a tier instead of
nearest documents becoming context. *The semantic cache in Stage 4 is the same
idea a third time: nearest cached prompt, and if it is close enough, reuse the
stored answer.*
