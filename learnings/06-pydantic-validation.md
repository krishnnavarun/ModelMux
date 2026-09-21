# Pydantic & Input Validation

---

## 1. What it is

**Pydantic** is a data-validation library that uses Python type annotations as
the schema. You declare what data should look like; it parses, validates,
coerces, and raises structured errors.

FastAPI is built on it — that's where automatic request validation and the
OpenAPI docs come from.

---

## 2. How it works

### Declaration is the schema

```python
from pydantic import BaseModel, Field

class ChatRequest(BaseModel):
    prompt: str
    max_tokens: int = Field(default=1000, ge=1, le=100_000)
    force_tier: Literal["small", "mid", "large"] | None = None
    bypass_cache: bool = False
```

From this Pydantic derives: required vs optional, types, coercion rules,
constraints, defaults, and a JSON Schema for the docs.

### Parse, don't validate

Pydantic doesn't just check data — it **returns a typed object**. Downstream code
receives `ChatRequest`, not a dict that was checked once and might have drifted.

```python
data = {"prompt": "hi", "max_tokens": "500"}   # string!
req = ChatRequest(**data)
req.max_tokens                                  # -> 500, an int
```

Coercion is configurable; strict mode disables it.

### Where validation errors surface

In FastAPI, model validation happens **before your handler runs**. A failure
returns **422 Unprocessable Entity** with a precise error list:

```json
{"detail": [{"type": "missing", "loc": ["body", "prompt"],
             "msg": "Field required"}]}
```

**Your handler never executes.** That has a consequence people miss: any logging,
metrics, or side effects in the handler don't happen either.

### Validators for logic constraints

```python
@field_validator("prompt")
@classmethod
def not_blank(cls, v: str) -> str:
    if not v.strip():
        raise ValueError("prompt must not be blank")
    return v

@model_validator(mode="after")
def check_combination(self):
    ...   # rules spanning multiple fields
```

### v1 vs v2

Pydantic v2 rewrote the core in Rust — 5–50x faster. Renames to know:
`@validator` → `@field_validator`, `.dict()` → `.model_dump()`,
`.parse_obj()` → `.model_validate()`, `class Config` → `model_config`.

---

## 3. How we used it in ModelMux

### The wire contract lives in one file

`app/schemas.py` holds `ChatRequest`, `Signals`, `Routing`, `ChatResponse`,
`ErrorResponse`. The dashboard and eval harness are written against it, so
keeping it in one file means the contract is readable in one place.

### `min_length` as a cost control

Stage 1 had:

```python
prompt: str = Field(min_length=1)
```

That is not a style choice — **an empty prompt would otherwise become a paid
provider call that could only ever return nothing.**

### Then we deliberately removed it

Stage 1's spec-alignment pass took the length constraints *out* of Pydantic:

```python
class ChatRequest(BaseModel):
    prompt: str        # <- no length rules here, on purpose
```

Two reasons, both important:

1. **The spec requires 400** for an empty or over-length prompt. Pydantic
   produces **422**, from a layer that runs before our code. We cannot change
   that status from inside the handler.
2. **A Pydantic rejection never reaches our handler — so it could never write a
   database row.** The spec says nothing is silently dropped. Enforcing in the
   handler is what makes the row possible.

So the check moved:

```python
# app/main.py
prompt = request.prompt.strip()
if not prompt:
    finish("error", "empty prompt")            # <- writes the row
    return JSONResponse(status_code=400, content={...})

if len(request.prompt) > config.limits["max_prompt_chars"]:
    finish("error", f"prompt exceeds {max_chars} chars")
    return JSONResponse(status_code=400, content={...})
```

**The cost, stated honestly:** two hand-written checks a framework could have
done, and the limit now lives in `config.yaml` rather than next to the field it
constrains. Recorded as DECISIONS.md D1.

### What we still let Pydantic do

Type checking, required-field checking, `max_tokens` bounds, and the
`force_tier` enum. Those have no 400-vs-422 requirement and no logging need, so
the framework handles them. The test asserting `422` for a missing `prompt`
documents that boundary:

```python
def test_missing_prompt_field_is_422(client):
    assert client.post("/v1/chat", json={}).status_code == 422
```

### Nullable-by-design fields

```python
class Signals(BaseModel):
    token_count: int | None = None
    has_code: bool | None = None
    embedding_tier: str | None = None
```

Every field `None` at Stage 1. **`None` means "not computed"; `0.0` would be a
claim we measured something and found nothing.** Fixing the shape early means
the dashboard never has to be rewritten as stages fill it in.

---

## 4. Interview questions

**Q: What does Pydantic give you over hand-written validation?**
Declarative schemas from type hints, parsing into typed objects rather than
checked dicts, structured machine-readable errors, JSON Schema generation, and a
fast Rust core in v2. Less code, and the schema is the documentation.

**Q: 400 vs 422 — when each?**
400 is malformed or invalid input generally; 422 specifically means well-formed
syntax but semantically unprocessable. FastAPI returns 422 for validation
failures. Both are defensible — what matters is being consistent and documenting
which you use.

**Q: Your spec says return 400 for an over-length field, but Pydantic gives 422.
How do you resolve that?**
Three options: a custom exception handler that rewrites 422→400 globally; leave
the constraint off the model and check in the handler; or change the spec.
*We chose the second — because a Pydantic rejection never reaches the handler,
so it could never write the audit row our spec also required. The logging
requirement, not the status code, actually forced it.*

**Q: What's "parse, don't validate"?**
Instead of checking a dict and passing it along — where a later reader can't know
it was checked — parse into a type that can only hold valid data. Validity
becomes a property of the type, enforced once at the boundary.

**Q: Where should validation live in a layered app?**
At the edge, then trusted inward. Validate once where untrusted data enters, and
convert to a typed object. Re-validating in every layer is noise; not validating
at all pushes malformed data deep where errors are confusing.

**Q: Pydantic v1 vs v2?**
v2 moved the core to Rust, 5–50x faster. API renames: `@field_validator`,
`model_dump()`, `model_validate()`, `model_config`. Migration is mostly
mechanical but `@validator` semantics changed subtly around ordering and
pre/post.

**Q: How do you validate one field against another?**
`@model_validator(mode="after")` — it runs once all fields are populated, so you
can compare them. A `@field_validator` only sees its own field.

**Q: Why would you make a response field nullable rather than defaulting to 0?**
Because they mean different things. `None` = not computed; `0` = computed and
found to be zero. *In this project `complexity_score: None` at Stage 1 honestly
said "no classifier exists"; `0.0` would have claimed we scored the prompt and
judged it trivial.*
