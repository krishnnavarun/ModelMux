# Tokenization & tiktoken

---

## 1. What it is

A **token** is the unit a language model actually reads. Not a character, not a
word — a chunk of text from a fixed vocabulary, typically 3–4 characters of
English.

**Tokenization** is converting text into token IDs.
**BPE** (Byte Pair Encoding) is the algorithm most modern LLMs use.
**tiktoken** is OpenAI's fast BPE tokenizer library.

---

## 2. How it works

### Why models don't use characters or words

- **Characters** — sequences get very long, and attention is O(n²) in length
- **Words** — vocabulary is unbounded; every typo and rare name is unknown

**Subword tokenization** is the compromise: common words become one token, rare
words split into pieces, and nothing is ever out-of-vocabulary.

```
"hello"          -> 1 token
"antidis"        -> likely 2-3 tokens
"  "             -> whitespace is tokens too
"日本語"          -> often 1 token per character, sometimes more
```

Rough English rule of thumb: **~4 characters per token**, ~0.75 tokens per word.

### Byte Pair Encoding

Training:
1. Start with a vocabulary of individual bytes
2. Count adjacent pairs across a corpus
3. Merge the most frequent pair into a new token
4. Repeat until you hit the target vocabulary size (e.g. 100k)

Encoding applies those learned merges greedily. Because it starts from **bytes**,
any input is representable — no unknown-token problem.

### Why token counts matter

1. **Cost** — billing is per token, input and output priced separately
2. **Context limits** — models have a maximum token window
3. **Latency** — output tokens are generated one at a time, so output length
   dominates response time

### Every provider tokenizes differently

Vocabularies are model-specific. The same string is a different number of tokens
for GPT-4, Llama, Gemini, and Claude. So:

- **A local tokenizer gives an estimate for anyone but its own model**
- **For billing, use the counts the provider returns in the response**

### tiktoken's practical behaviour

- First use **downloads** the encoding file over the network
- The first `encode()` call is roughly 1000x slower than subsequent ones
- Steady state is very fast — microseconds

Both facts mean: **load once, warm it, reuse.**

---

## 3. How we used it in ModelMux

### Measured before designing around it

The spec budgets classification at **under 20ms**. First measurements looked
alarming:

```
load:          6579 ms      (downloads the BPE file)
first encode:    48 ms      (over the whole budget by itself)
```

Rather than abandon tiktoken, I measured steady state:

```
   19 chars ->   9 tokens | p50 0.005ms  p95 0.005ms
 1440 chars -> 201 tokens | p50 0.048ms  p95 0.052ms
   42 chars ->  12 tokens | p50 0.005ms
```

**400x under budget.** The 48ms was one-time warmup, not per-call cost.

> **The concept: distinguish one-time cost from per-call cost before designing
> around either.** Trusting the first measurement would have meant dropping
> tiktoken for a worse approximation, to solve a problem that exists once per
> process.

### Load once, warm it, at startup

```python
# app/tokens.py
def init() -> None:
    global _encoder, _using_fallback
    try:
        import tiktoken
        _encoder = tiktoken.get_encoding(ENCODING_NAME)   # cl100k_base
        _encoder.encode("warm")     # first encode is ~1000x slower than the rest
        _using_fallback = False
    except Exception as exc:
        _encoder = None
        _using_fallback = True
        print(f"[tokens] tiktoken unavailable ({exc}); falling back", file=sys.stderr)
```

Called from the app's lifespan, so it happens exactly once.

### A fallback that degrades loudly

```python
FALLBACK_CHARS_PER_TOKEN = 4

def count(text: str) -> int:
    if _encoder is not None:
        return len(_encoder.encode(text))
    return max(1, len(text) // FALLBACK_CHARS_PER_TOKEN)
```

tiktoken fetches its encoding file over the network on first use. Without a
fallback, **being offline once would prevent the service from starting at all.**

And the degradation is *visible*:

```python
def using_fallback() -> bool: ...
```

```python
if tokens.using_fallback():
    print("WARNING: tiktoken unavailable; token counts are char estimates.\n")
```

**A routing-accuracy figure measured on estimated tokens is not the same claim as
one measured on real ones**, so the eval harness says which it used.

### Honest about the approximation

```python
"""
1. **It is OpenAI's tokenizer.** Groq, Gemini and Anthropic all tokenize
   differently, so this is an *approximation* for every provider we actually
   call. That is fine for its two jobs -- pre-call size estimation and a
   routing signal -- because both only need the relative ordering of prompts
   to be right. It is NOT fine for billing: real token counts come back from
   the provider in `usage`, and those are what the database records.
"""
```

Two jobs, two standards of accuracy. Routing needs *ordering* to be right;
billing needs *exactness*, and only the provider can supply that.

### Tokens as the Stage 2 routing signal

The naive router scores on token count alone:

```python
score = min(token_count / saturation_tokens, 1.0)
```

**And it fails badly** — 34% accurate — because *length is not difficulty*:

```
#11  large -> small (  9 tk)  Prove Fermat's Last Theorem.
#31  small -> mid   (340 tk)  1,400-word diary entry -> "what day was it?"
```

Token count is a real signal, but a weak one. It is Stage 3's job to add signals
that see *difficulty* rather than *size*. → `13-routing-evaluation.md`.

---

## 4. Interview questions

**Q: What is a token, and why don't models use words or characters?**
A subword unit from a fixed vocabulary. Characters make sequences too long for
quadratic attention; words give an unbounded vocabulary with out-of-vocabulary
failures. Subwords keep sequences short while representing anything.

**Q: Explain BPE.**
Start with a byte-level vocabulary, repeatedly merge the most frequent adjacent
pair into a new token, and stop at a target vocabulary size. Encoding applies
those merges greedily. Starting from bytes means nothing is ever unrepresentable.

**Q: Roughly how many tokens in a page of English?**
About 4 characters per token, ~0.75 tokens per word. A 500-word page is roughly
650–700 tokens. Non-English and code tokenize less efficiently.

**Q: Can you use one tokenizer to count tokens for a different model?**
Only as an estimate. Vocabularies are model-specific, so counts differ. Fine for
sizing and routing where relative ordering is what matters; **wrong for billing**
— use the counts the provider returns.

**Q: You need token counts on every request with a 20ms budget. How?**
Load the encoder once at startup and warm it, then reuse. *I measured 6.6s load
and 48ms for the first encode, but 0.005–0.05ms steady state — so the only
design requirement was avoiding per-request initialisation.*

**Q: How would you handle the tokenizer being unavailable?**
Degrade rather than fail to start — a character-based estimate is far better than
a service that won't boot because it couldn't reach a CDN. **And surface the
degradation**, so any metric computed on estimates is labelled as such.

**Q: Why are output tokens more expensive than input tokens?**
Input is processed in parallel in a single forward pass. Output is generated
autoregressively — one token at a time, each requiring a full pass. Output costs
more compute and dominates latency.

**Q: How do you reduce token costs in an LLM application?**
Shorter prompts and system messages; cache repeated context (prompt caching);
cap `max_tokens`; summarise rather than resend long histories; and **route easy
requests to cheaper models** — which is what this entire project is.
