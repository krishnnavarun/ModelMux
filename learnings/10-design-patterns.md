# Design Patterns Used in ModelMux

Every pattern here is in the codebase. None was added for its own sake.

---

## 1. Adapter Pattern

### What it is

Converts one interface into another so incompatible classes can work together.
You define the interface *you* want; each adapter translates a foreign API into
it.

### How it works

```
  Your code ──▶ Provider (interface)
                    ▲
        ┌───────────┼───────────┬───────────┐
   GroqProvider  GoogleProvider  AnthropicProvider  MockProvider
        │             │              │
     Groq API    Gemini API    Anthropic API
```

The caller depends on the **interface**, never a concrete provider.

### In ModelMux

```python
# app/providers/base.py
class Provider(ABC):
    name: str

    @abstractmethod
    async def complete(self, prompt: str, model: str,
                       max_tokens: int) -> ProviderResult: ...
```

Three genuinely different APIs collapse into that one signature:

| | Groq | Google | Anthropic |
|---|---|---|---|
| Auth | `Authorization: Bearer` | `x-goog-api-key` | `x-api-key` + `anthropic-version` |
| Model | body field | **in the URL path** | body field |
| Body | `messages[]` | `contents[].parts[]` | `messages[]`, `max_tokens` required |
| Text | `choices[0].message.content` | `candidates[0].content.parts[0].text` | `content[]` typed blocks, joined |
| Tokens | `usage.prompt_tokens` | `usageMetadata.promptTokenCount` | `usage.input_tokens` |

**The payoff, measured:** adding Google and Anthropic in Stage 2 required **zero
changes to `main.py` or `router.py`.** That was the real test of the Stage 1
design.

> **The principle: the boundary you draw when there is one of something
> determines how painful the second one is.**

---

## 2. Dependency Injection

### What it is

A component receives its dependencies from outside rather than constructing them
itself.

### How it works

```python
# NOT injected — hidden dependency, untestable
class GroqProvider:
    def __init__(self):
        self.client = httpx.AsyncClient()      # creates its own

# Injected — caller owns it, lends it
class GroqProvider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
```

### In ModelMux

`main.py`'s lifespan owns one `httpx.AsyncClient` and lends it to every adapter:

```python
async with httpx.AsyncClient(timeout=timeout) as client:
    app.state.providers = build_all(config, client)
```

**Two benefits, both real here:**

1. **Connection pooling works.** All adapters share one pool, so requests reuse
   warm TLS connections. A client per adapter would fragment it.
2. **Tests substitute a fake in one line**, with no patching or monkeypatching:

```python
test_client.app.state.providers[name] = mock_provider
```

FastAPI does the same thing for handlers — `request: ChatRequest`,
`background: BackgroundTasks` are injected by type annotation.

---

## 3. Registry Pattern

### What it is

A central map from a key (usually a string from config) to an implementation, so
callers never name a concrete class.

### In ModelMux

```python
# app/providers/__init__.py
REGISTRY: dict[str, type[Provider]] = {
    "groq": GroqProvider,
    "google": GoogleProvider,
    "anthropic": AnthropicProvider,
    "mock": MockProvider,
}

def build_all(config, client) -> dict[str, Provider]:
    for tier_name, tier in config.tiers.items():
        for entry in tier.get("providers") or []:
            name = entry["name"]
            if name not in REGISTRY:
                raise ValueError(f"tier '{tier_name}' names unknown provider "
                                 f"'{name}'. Known: {sorted(REGISTRY)}")
            ...
```

**`main.py` now names no provider at all** — it builds whatever `config.yaml`
asks for. Adding a fourth provider is: write the adapter, add one registry line,
add it to config.

An unknown name fails **at startup** with a message listing valid options,
rather than on the first request that happens to route to it.

---

## 4. Normalised Error Taxonomy

### What it is

Translating every failure mode at a boundary into a small fixed set of types the
layer above understands.

### In ModelMux

```python
ProviderTimeout       # retry — transient
ProviderRateLimited   # retry with backoff — transient (carries retry_after)
ProviderServerError   # retry — probably transient
ProviderBadRequest    # NEVER retry — deterministic
```

**The taxonomy encodes retry policy. That is its entire purpose.** A 500 is worth
retrying — the next attempt may hit a healthy server. A 401 is not: your key will
still be invalid in 500ms, so a retry burns time and adds load for a guaranteed
identical failure.

Without it, `resilience.py` would need to know that Groq says 429 while another
says 503-with-a-JSON-body, and it would silently miss any new variant.

### The mistake worth telling

I mapped `ProviderBadRequest` → HTTP **400**. Obvious-looking, and wrong.

That error fires when **our** API key is dead or **our** model name is unknown.
The caller's prompt was fine. It produced:

```
HTTP 400  {"error": "groq 401: Invalid API Key"}
```

A 400 whose message is about *our* credentials is incoherent.

**I had collapsed two different questions:** *"may we retry this?"* (the
taxonomy) and *"whose fault is it?"* (the status code). All provider errors now
return 502; the taxonomy governs retries only.

---

## 5. Abstract Base Class

```python
from abc import ABC, abstractmethod

class Provider(ABC):
    @abstractmethod
    async def complete(...) -> ProviderResult: ...
```

An `@abstractmethod` makes instantiation fail if a subclass doesn't implement
it — **at construction, not at the first call.**

*ABC vs Protocol:* ABC is nominal (you must inherit) and enforced at runtime;
`typing.Protocol` is structural ("if it has these methods it qualifies"),
checked statically. ABC suits us — we control every implementation and want the
enforcement.

---

## 6. Strategy Pattern (emerging)

`router.select_tier()` will select among classification strategies —
`heuristic`, `embedding`, `hybrid` — chosen by `config.classifier.mode`. The
0-1 score interface stays fixed while the algorithm behind it changes.

That's why Stage 2 normalises token count into a score rather than mapping
tokens directly to tiers: **Stage 3 replaces how the score is computed and
touches nothing else** — not the thresholds, not the config shape, not the
function signature. → DECISIONS.md D11.

---

## 7. Interview questions

**Q: Explain the Adapter pattern with a real example.**
It converts a foreign interface into the one your code wants. *I have three LLM
providers with completely different request and response shapes — one puts the
model in the URL path, all three use different auth headers and different token
count fields. Each adapter normalises into one `ProviderResult` and four error
types, so adding the second and third providers required zero changes to the
endpoint or the router.*

**Q: What is dependency injection and why does it matter?**
Passing dependencies in rather than constructing them internally. It removes
hidden global state, makes lifetime explicit, and makes testing trivial. *In my
project one HTTP client is created at startup and lent to every adapter — which
both preserves connection pooling and lets tests swap in a fake with one
assignment.*

**Q: Isn't DI just passing arguments?**
Essentially, yes — the pattern is the discipline of doing it consistently at
boundaries, plus a container to wire it up in larger systems. Python needs far
less ceremony than Java or C# here; passing the collaborator in is usually
enough.

**Q: When would you use a Registry?**
When the set of implementations is configuration-driven and you don't want
callers importing concrete classes. *Mine maps config strings to adapter classes,
so `main.py` names no provider — and an unknown name fails at startup with the
valid options listed, rather than mid-request.*

**Q: ABC vs Protocol in Python?**
ABC is nominal and runtime-enforced — you inherit, and instantiation fails if an
abstract method is missing. Protocol is structural and static — anything with
the right shape satisfies it, checked by a type checker. Use ABC when you own
the implementations and want runtime enforcement; Protocol for duck-typing
third-party objects you can't subclass.

**Q: How do you decide what abstraction to build for something you only have one
of?**
Ask what the second one would look like. Build the boundary if a second is
plausible and the boundary is cheap; skip it if you're guessing. *I built the
provider interface with one provider, and adding two more cost one file each —
but the interface was small and the second provider was certain, not
speculative.*

**Q: What's the danger of premature abstraction?**
Abstracting from one example gives you an interface shaped by that example's
accidents. You get complexity now for flexibility you may never use, and the
abstraction usually turns out wrong when the second case arrives. The rule of
thumb is to wait for the second or third instance — unless, as here, the second
is certain and imminent.

**Q: Why translate errors at a boundary instead of letting them propagate?**
So the layer above depends on your vocabulary, not the vendor's. It keeps
handling code simple, prevents leaking vendor detail to callers, and means a new
vendor error variant is handled by the adapter rather than silently missed
upstream. *In mine, the four error types are literally the retry policy.*
