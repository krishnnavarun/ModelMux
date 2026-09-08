"""ModelMux -- FastAPI application (SPEC section 5).

Stage 1: bare proxy plus the database. One endpoint, one provider, no routing,
no cache. Per SPEC section 11, Stage 1 is done when a curl request returns an
answer *and the row is visible in SQLite* -- logging is part of this stage, not
a later one.

The response is already the full Stage 6 shape. Fields that no stage has
implemented yet are None (never 0.0, never a plausible-looking fake), because
`None` honestly means "not computed" while a number would be a claim we
measured something. Fixing the shape now means the dashboard and eval harness
never have to be rewritten around it.
"""

import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse

from app import config as config_module
from app import db, router, tokens
from app.providers import build_all
from app.providers.base import ProviderError
from app.schemas import ChatRequest

# Read .env into the environment before anything reads os.environ.
load_dotenv()

# Loaded at import so a broken config.yaml stops the process immediately with
# a clear message, rather than surfacing as a KeyError on the first request.
config = config_module.load()

PROMPT_PREVIEW_CHARS = 80


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown.

    Everything before `yield` runs once at startup, everything after once at
    shutdown.

    Two timeouts, not one (SPEC section 7): `connect` bounds how long we wait
    to establish a connection, `read` bounds how long we wait for the model to
    answer. A single flat timeout cannot express "give up on an unreachable
    host quickly, but be patient with a thinking model".
    """
    timeout = httpx.Timeout(
        config.resilience["request_timeout_seconds"],
        connect=config.resilience["connect_timeout_seconds"],
    )
    db.init_db(config.db_path)

    # Load and warm the tokenizer once. First load fetches a BPE file and the
    # first encode costs ~48ms; steady state is ~0.01ms. Doing this per request
    # would blow the 20ms classification budget on its own.
    tokens.init()

    async with httpx.AsyncClient(timeout=timeout) as client:
        app.state.http = client
        # Built from config -- main.py names no provider. Adding Google and
        # Anthropic in Stage 2 required no change to this line.
        app.state.providers = build_all(config, client)
        yield

app = FastAPI(title="ModelMux", version="0.1.0", lifespan=lifespan)


def _blank_record(request_id: str, prompt: str) -> dict:
    """The columns every row shares, success or failure.

    SPEC section 10: we store an 80-character preview plus a hash, never the
    full prompt. That is a privacy decision -- the request log must not become
    a transcript of everything anyone ever pasted in.
    """
    return {
        "id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_preview": prompt[:PROMPT_PREVIEW_CHARS],
        "cache_hit": 0,
        "escalated": 0,
        "fallback_fired": 0,
        "attempts": 1,
    }


@app.get("/health")
async def health():
    """Liveness only. Never calls a provider, so it stays free and instant."""
    return {"status": "ok"}


@app.post("/v1/chat")
async def chat(request: ChatRequest, background: BackgroundTasks):
    # Monotonic clock: measures duration correctly even if the system clock
    # jumps. time.time() is for timestamps, perf_counter() for durations.
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    record = _blank_record(request_id, request.prompt)

    def finish(status: str, error: str | None = None, **fields):
        """Attach outcome to the row and queue the write.

        BackgroundTasks runs this *after* the response is sent, so the caller
        never waits on the disk. Every exit path from this endpoint goes
        through here -- that is what "nothing is silently dropped" means.
        """
        record.update(
            status=status,
            error_message=error,
            latency_ms=int((time.perf_counter() - started) * 1000),
            **fields,
        )
        background.add_task(db.log_request, record)

    # --- Input limits (SPEC section 10) -----------------------------------
    # Enforced here rather than as a pydantic constraint because the spec
    # requires 400, and a pydantic failure produces 422 from a layer that runs
    # before this function. See DECISIONS.md.
    prompt = request.prompt.strip()
    if not prompt:
        finish("error", "empty prompt")
        return JSONResponse(
            status_code=400,
            content={"error": "prompt must not be empty", "request_id": request_id},
        )

    max_chars = config.limits["max_prompt_chars"]
    if len(request.prompt) > max_chars:
        # An unbounded prompt is a cost attack: rejected before any embedding
        # or provider call, so an oversized request costs us nothing.
        finish("error", f"prompt exceeds {max_chars} chars")
        return JSONResponse(
            status_code=400,
            content={
                "error": f"prompt exceeds the {max_chars} character limit",
                "request_id": request_id,
            },
        )

    # --- Routing ----------------------------------------------------------
    classify_started = time.perf_counter()
    decision = router.select_tier(request.prompt, config, request.force_tier)
    classify_ms = int((time.perf_counter() - classify_started) * 1000)

    tier = decision.tier
    provider_config = decision.provider
    provider = app.state.providers[provider_config["name"]]

    try:
        result = await provider.complete(
            prompt=request.prompt,
            model=provider_config["model"],
            max_tokens=request.max_tokens,
        )
    except ProviderError as exc:
        # Always 502. Per SPEC section 5, 400 means the *caller's* prompt was
        # empty or over-length; it must never mean "our API key is dead" or
        # "our configured model name is wrong". Both of those are
        # ProviderBadRequest, and from the caller's side they are simply the
        # gateway failing -- which is exactly what 502 says.
        #
        # The four-way taxonomy still matters, but it governs *retry policy*
        # (Stage 5), not the status code we return here.
        finish(
            "error",
            str(exc),
            tier=tier,
            provider=provider_config["name"],
            model=provider_config["model"],
        )
        return JSONResponse(
            status_code=502,
            content={"error": str(exc), "request_id": request_id, "attempts": 1},
        )

    # --- Cost -------------------------------------------------------------
    cost_usd = config.cost_usd(tier, result.tokens_in, result.tokens_out)

    # The counterfactual: what this exact exchange would have cost on the
    # baseline tier. Every savings claim in the README is a sum of the gap
    # between these two numbers.
    #
    # KNOWN BIAS: this reuses the token counts we actually observed. A larger
    # model asked the same question often answers at a different length, so
    # this is "same tokens, baseline prices", not a true counterfactual. It is
    # the honest cheap approximation; recorded in DECISIONS.md.
    cost_if_large_usd = config.cost_usd(
        config.baseline_tier, result.tokens_in, result.tokens_out
    )

    signals = decision.signals

    finish(
        "success",
        complexity_score=decision.complexity_score,
        signals_json=json.dumps(signals),
        tier=tier,
        provider=provider_config["name"],
        model=result.model,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=cost_usd,
        cost_if_large_usd=cost_if_large_usd,
        classify_ms=classify_ms,
        cache_lookup_ms=None,
    )

    return {
        "response": result.text,
        "routing": {
            "request_id": request_id,
            "tier": tier,
            "provider": provider_config["name"],
            "model": result.model,
            "cache_hit": False,           # no cache until Stage 4
            "complexity_score": decision.complexity_score,
            "signals": signals,
            "escalated": decision.escalated,
            "fallback_fired": False,      # no resilience layer until Stage 5
            "attempts": 1,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "cost_usd": round(cost_usd, 8),
            "cost_if_large_usd": round(cost_if_large_usd, 8),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "provider_latency_ms": result.raw_latency_ms,
            "classify_ms": classify_ms,
            "cache_lookup_ms": None,   # no cache until Stage 4
            "routing_reason": decision.reason,
        },
    }
