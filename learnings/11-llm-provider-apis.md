# LLM Provider APIs

---

## 1. What it is

An **LLM provider API** is an HTTP endpoint that takes a prompt and returns
generated text, billed per token. We call three:

- **Groq** — very fast inference of open models (Llama etc.). Our *small* tier.
- **Google Gemini** — our *mid* tier.
- **Anthropic Claude** — our *large* tier, and the cost baseline.

---

## 2. How it works

### The common shape

Every one of them is: `POST` a JSON body containing a model name and messages,
get back JSON containing generated text and token usage. The details differ
everywhere else.

### OpenAI-compatibility

Many providers deliberately copy OpenAI's request/response format so existing
client code works unchanged. Groq is one:

```
https://api.groq.com/openai/v1/chat/completions
                     ^^^^^^
```

**That `/openai/` is not a mistake and does not call OpenAI.** It signals
"OpenAI-compatible schema".

This is genuinely useful — compatible providers need near-identical adapters.
It's also a trap: the shapes are only *mostly* identical, and differences hide in
streaming, usage fields, and error bodies. **Assume compatibility, verify per
provider.**

### The three shapes we handle

**Groq** (OpenAI-compatible):
```python
headers={"Authorization": f"Bearer {key}"}
json={"model": model, "max_tokens": n, "messages": [{"role": "user", "content": prompt}]}
# -> body["choices"][0]["message"]["content"]
# -> body["usage"]["prompt_tokens"], ["completion_tokens"]
```

**Google Gemini** — model in the URL, different body and response:
```python
url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
headers={"x-goog-api-key": key}
json={"contents": [{"parts": [{"text": prompt}]}],
      "generationConfig": {"maxOutputTokens": n}}
# -> body["candidates"][0]["content"]["parts"][0]["text"]
# -> body["usageMetadata"]["promptTokenCount"], ["candidatesTokenCount"]
```

**Anthropic** — dated API version header, `max_tokens` required, typed content
blocks:
```python
headers={"x-api-key": key, "anthropic-version": "2023-06-01"}
json={"model": model, "max_tokens": n, "messages": [...]}   # max_tokens REQUIRED
# -> "".join(b["text"] for b in body["content"] if b["type"] == "text")
# -> body["usage"]["input_tokens"], ["output_tokens"]
```

### Token-based billing

You pay per token, **input and output priced separately** — output typically
much higher.

```
cost = (tokens_in / 1000 * rate_in) + (tokens_out / 1000 * rate_out)
```

Providers return the authoritative counts in the response. **Use those for
billing**, never a local estimate.

### Failure modes

| Status | Meaning | Retry? |
|---|---|---|
| 401 | bad key | **No** — deterministic |
| 404 | unknown model | **No** — deterministic |
| 429 | rate limited | Yes, with backoff; honour `Retry-After` |
| 500/503 | provider broken | Yes |
| 529 | Anthropic "overloaded" | Yes (non-standard code) |
| 200 with no content | safety block (Gemini) | No — handle explicitly |

---

## 3. How we used it in ModelMux

### One adapter per provider, one shared interface

See `10-design-patterns.md`. Each adapter's whole job is translation, both ways:
our call shape → their JSON, their JSON → `ProviderResult`, their failures → our
four error types.

### The 200-that-isn't-a-success

```python
# app/providers/google.py
except (KeyError, IndexError, ValueError) as exc:
    # Gemini returns 200 with no `parts` when it blocks a response on
    # safety grounds. That is a real, reachable case -- not defensive
    # padding -- and it must not surface as a raw KeyError.
    raise ProviderServerError(f"unexpected google response shape: {exc}") from exc
```

**A 200 whose body isn't the shape you expect is still a failure.** Checking
status alone isn't enough.

### Anthropic's content blocks

```python
text = "".join(
    block.get("text", "")
    for block in body["content"]
    if block.get("type") == "text"
)
```

`content` is a list of *typed* blocks — text, tool_use, thinking. Taking
`content[0].text` blindly breaks the moment a non-text block appears first. We
join text blocks and ignore the rest.

### Never leak an upstream body

```python
detail = (detail or response.text)[:200]
```

Provider error bodies can carry internal detail and are unbounded. Extract the
message, cap it, then it's safe for a log or a response.

### Model names and prices are dated and marked unverified

```yaml
# PRICING SNAPSHOT: 2026-09-08
# !! VERIFY BEFORE TRUSTING ANY COST NUMBER !!
- name: groq
  model: llama-3.1-8b-instant   # VERIFY: Groq renames/deprecates often
```

**Update (2026-09-08): the Groq half is now verified**, and checking found
two real errors:

1. `llama-3.1-8b-instant` is **Enterprise / Contact Sales** — no published
   price, not callable on a developer key. The small tier pointed at a model we
   could not have used.
2. The output price was **3.75x too low**. Groq publishes per 1M tokens; our
   config is per 1K. `gpt-oss-20b` is $0.075/$0.30 per 1M → 0.000075/0.0003 per
   1K. My placeholder said 0.00008 for output.

> **Unit mismatches are where pricing bugs live.** Per-1M vs per-1K, per-token
> vs per-thousand, input vs output. Every one of those is a factor-of-1000 or
> factor-of-4 error that still *looks* plausible in a config file.

The Anthropic large tier remains entirely unverified — and it is the baseline
every savings claim is measured against. → DECISIONS.md D14.

### The counterfactual, and its bias

`cost_if_large_usd` reprices **the token counts we actually observed** at
large-tier rates. That is *"same tokens, baseline prices"*, **not** what the
large model would truly have cost — a bigger model answering the same prompt
usually writes a different amount.

We keep it because the alternative is calling the large model every time, which
destroys the point of the router. We **label** it everywhere: in code comments,
in `specs.md`, and in DECISIONS.md D3.

> **When you cannot measure the right thing, measure the affordable thing and
> name the gap precisely.** An approximation you have characterised is a finding;
> the same approximation unlabelled is a false claim.

---

## 4. Interview questions

**Q: How do you design a system that talks to multiple LLM providers?**
An adapter per provider behind one interface, returning a normalised result and
a normalised error taxonomy. Config-driven provider selection via a registry, one
shared HTTP client for pooling, and per-provider translation of both request
shape and failure modes. *Adding my second and third providers required no
change to the endpoint or router.*

**Q: What does "OpenAI-compatible" actually mean, and what's the catch?**
The provider mirrors OpenAI's request/response schema so existing clients work.
The catch is it's only *mostly* compatible — streaming formats, usage field
names, and error bodies commonly differ. Assume compatibility to move fast, then
verify each provider's edges.

**Q: How is LLM API usage billed, and what does that mean for your code?**
Per token, with input and output priced separately and output usually far more
expensive. So cost calculations must count both, and you must use the
provider-returned counts rather than a local tokenizer estimate. *My first
version priced input only — which understated spend unevenly across tiers and
would have corrupted the exact comparison the project exists to make.*

**Q: A provider returns 200 but your parser throws. What's wrong?**
The status was fine; the body wasn't the shape you assumed. Common causes:
safety filtering returning an empty candidate, a new response variant, or a
content block type you don't handle. Treat a shape mismatch as a provider error,
not a crash, and never let a raw `KeyError` reach the caller.

**Q: Which provider errors do you retry?**
Timeouts, 429, and 5xx — transient. Never 401 or 404: a dead key or unknown
model fails identically on retry, so retrying just adds latency and load. *This
is exactly why my error taxonomy has four types rather than one.*

**Q: How do you handle rate limits well?**
Honour `Retry-After` when present; otherwise exponential backoff **with jitter**
to avoid a thundering herd. Beyond that, a circuit breaker so a persistently
limited provider is skipped entirely, and ideally client-side rate limiting to
stay under the quota in the first place.

**Q: How would you estimate cost before making a call?**
Tokenize locally to estimate input tokens and multiply by the input rate; output
is unknown until you have it, so bound it with `max_tokens`. Treat it as an
estimate — different providers tokenize differently, so a local count is
approximate for anyone but its own tokenizer.

**Q: What are the risks of routing between models by cost?**
Quality regression is the main one — a cheap model confidently wrong is worse
than an expensive correct answer, so you must measure quality alongside cost.
Then: classification overhead eating the savings, cache leakage across users if
you cache semantically, and cost spikes if failover escalates tiers during an
outage. *All four are explicit constraints in my spec.*
