"""Pydantic request/response models (SPEC section 4, 5).

Kept out of main.py so the wire contract is readable in one place. The
dashboard and eval harness are written against this file.
"""

from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Body of POST /v1/chat.

    Note there is no max_length on `prompt` here. SPEC section 5 requires a
    **400** for an over-length prompt, but a pydantic constraint produces a
    422 from FastAPI's validation layer before our code runs. So the length
    limit is enforced explicitly in the endpoint against config's
    `max_prompt_chars`, and only the "must be a non-empty string" check is
    left to pydantic. See DECISIONS.md.
    """

    prompt: str
    max_tokens: int = Field(default=1000, ge=1, le=100_000)

    # Testing and dashboard-comparison affordances (SPEC section 5). Neither is
    # part of normal operation; both are honoured from Stage 3 / Stage 4.
    force_tier: Literal["small", "mid", "large"] | None = None
    bypass_cache: bool = False


class Signals(BaseModel):
    """Individual, inspectable components of a complexity score.

    SPEC section 5: "The `signals` object is what makes the system
    explainable. Never remove it."

    At Stage 1 there is no classifier, so every field is None. The shape is
    fixed now so nothing downstream changes when Stage 3 fills it in.
    """

    token_count: int | None = None
    has_code: bool | None = None
    reasoning_markers: int | None = None
    multi_question: bool | None = None
    embedding_tier: str | None = None
    embedding_confidence: float | None = None


class Routing(BaseModel):
    """The full explanation of what happened to a request."""

    request_id: str
    tier: str
    provider: str
    model: str
    cache_hit: bool
    complexity_score: float | None
    signals: Signals
    escalated: bool
    fallback_fired: bool
    attempts: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    cost_if_large_usd: float
    latency_ms: int
    classify_ms: int | None
    cache_lookup_ms: int | None


class ChatResponse(BaseModel):
    response: str
    routing: Routing


class ErrorResponse(BaseModel):
    """Every error body carries request_id so a caller complaint can be traced
    to its database row (SPEC section 5)."""

    error: str
    request_id: str
