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
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app import config as config_module
from app import cache, db, metrics, ratelimit, resilience, router, tokens
from app.classifier import embedding
from app.providers import build_all
from app.providers.base import ProviderError  # noqa: F401  (re-exported for tests)
from app.schemas import ChatRequest

# Read .env into the environment before anything reads os.environ.
# Lives in config.py so the eval scripts -- which never import main --
# get the same loading AND the same shadowing warning.
config_module.load_env()

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
    embedding.init()
    await cache.init(config)

    async with httpx.AsyncClient(timeout=timeout) as client:
        app.state.http = client
        # Built from config -- main.py names no provider. Adding Google and
        # Anthropic in Stage 2 required no change to this line.
        app.state.providers = build_all(config, client)
        # Circuit state is per-process and lives for the process lifetime --
        # see the note in resilience.Circuit.
        app.state.breakers = resilience.CircuitBreakers(config)
        # Per-caller token buckets. In memory, per process -- see the note in
        # ratelimit.py. Protects the API BUDGET, not correctness.
        app.state.limiter = ratelimit.RateLimiter(
            config.limits["rate_limit_per_minute"])
        yield
    await cache.close()

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


DASHBOARD_PATH = config_module.PROJECT_ROOT / "dashboard" / "index.html"


@app.get("/", include_in_schema=False)
async def root():
    """Send the bare origin to the dashboard.

    There was no route here, so opening http://localhost:8000 -- the first
    thing anyone tries -- returned a 404 and read as "the server is broken".
    A redirect costs nothing and removes that moment.
    """
    return RedirectResponse(url="/dashboard", status_code=307)


@app.get("/dashboard")
async def dashboard():
    """Serve the single-page dashboard (SPEC section 11, Stage 6).

    Served from the API's own origin so the page can call /v1/stats and
    /health/providers with no CORS configuration -- which is most of why it is
    one static file rather than a separate Vite dev server.
    """
    if not DASHBOARD_PATH.exists():
        return JSONResponse(status_code=404,
                            content={"error": "dashboard/index.html not found"})
    return FileResponse(DASHBOARD_PATH, media_type="text/html")


@app.get("/health/providers")
async def health_providers():
    """Per-provider circuit state and last error (SPEC section 5).

    This is the operational view during an incident: which providers we are
    still talking to, which we have given up on, and how long until we probe
    them again.
    """
    breakers = getattr(app.state, "breakers", None)
    circuits = breakers.snapshot() if breakers else []

    configured = {
        entry["name"]
        for tier in config.tiers.values()
        for entry in (tier.get("providers") or [])
    }
    seen = {c["provider"] for c in circuits}

    # A provider with no circuit yet has simply never failed. Reporting it as
    # absent would read as "broken" on a dashboard; it is the healthiest state
    # there is.
    for name in sorted(configured - seen):
        circuits.append({
            "provider": name, "state": "closed", "recent_failures": 0,
            "last_error": None, "recovers_in_seconds": None,
        })

    return {
        "providers": sorted(circuits, key=lambda c: c["provider"]),
        "cache_available": cache.available(),
        "classifier_mode": config.classifier.get("mode"),
        "embedding_available": embedding.available(),
    }


@app.get("/v1/stats")
async def stats(window: str = "24h"):
    """Aggregates for the dashboard overview (SPEC section 8.9)."""
    try:
        return metrics.get_stats(config.db_path, window)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/v1/requests")
async def recent_requests(limit: int = 50):
    """Recent request log for the live feed (SPEC section 8.9)."""
    return {"requests": metrics.get_recent(config.db_path, limit)}


@app.post("/v1/compare")
async def compare(request: ChatRequest, http_request: Request):
    """Run one prompt through the router AND the baseline tier (SPEC section 5).

    This is the mitigation named in DECISIONS.md D3. `cost_if_large_usd`
    assumes the baseline would produce the SAME number of output tokens as the
    routed tier -- an assumption, not a measurement, because measuring it means
    paying for the expensive call.

    Here both calls really happen, so the assumption becomes checkable:
    `output_token_ratio` is the factor by which the live savings figure is
    wrong, and in which direction.

    Deliberately NOT cached and NOT logged as a normal request: it is a
    diagnostic that costs two calls, and letting it into the statistics would
    corrupt the very numbers it exists to audit.
    """
    caller = ratelimit.caller_id(http_request)
    allowed, retry_after = app.state.limiter.check(caller)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"error": "rate limit exceeded",
                     "retry_after_seconds": retry_after},
        )

    prompt = request.prompt.strip()
    if not prompt:
        return JSONResponse(status_code=400,
                            content={"error": "prompt must not be empty"})

    decision = router.select_tier(request.prompt, config)

    async def run(tier: str) -> dict:
        started = time.perf_counter()
        try:
            dispatched = await resilience.dispatch(
                prompt=request.prompt, tier=tier,
                max_tokens=request.max_tokens, config=config,
                providers=app.state.providers, breakers=app.state.breakers,
            )
        except resilience.AllProvidersFailed as exc:
            return {"ok": False, "tier": tier, "error": str(exc)}

        result = dispatched.result
        return {
            "ok": True,
            "tier": dispatched.tier,
            "provider": dispatched.provider_name,
            "model": result.model,
            "response": result.text,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "cost_usd": round(config.cost_usd(
                dispatched.tier, result.tokens_in, result.tokens_out), 8),
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }

    routed = await run(decision.tier)
    baseline = await run(config.baseline_tier)

    body = {
        "prompt_preview": request.prompt[:PROMPT_PREVIEW_CHARS],
        "routed": routed,
        "baseline": baseline,
        "routing": {
            "tier": decision.tier,
            "complexity_score": decision.complexity_score,
            "signals": decision.signals,
            "reason": decision.reason,
        },
    }

    if routed.get("ok") and baseline.get("ok"):
        saved = baseline["cost_usd"] - routed["cost_usd"]
        ratio = (baseline["tokens_out"] / routed["tokens_out"]
                 if routed["tokens_out"] else None)
        body["comparison"] = {
            "cost_saved_usd": round(saved, 8),
            "savings_pct": round(saved / baseline["cost_usd"] * 100, 2)
            if baseline["cost_usd"] else 0.0,
            "output_token_ratio": round(ratio, 3) if ratio else None,
            "d3_bias_note": (
                "cost_if_large_usd assumes output_token_ratio == 1.0. The "
                "measured ratio above is how wrong that assumption is for this "
                "prompt: >1 means live savings are UNDERstated, <1 OVERstated."
            ),
        }

    return body


@app.post("/v1/chat")
async def chat(request: ChatRequest, background: BackgroundTasks,
               http_request: Request):
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

    # --- Rate limit (SPEC section 10) --------------------------------------
    # BEFORE classification and before the cache: a rejected request should
    # cost nothing at all. Checking after would mean a caller in a retry loop
    # still burns ~11ms of embedding per rejected request, which is most of
    # what the limit exists to prevent.
    caller = ratelimit.caller_id(http_request)
    allowed, retry_after = app.state.limiter.check(caller)
    if not allowed:
        finish("error", f"rate limited ({caller})")
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate limit exceeded",
                "request_id": request_id,
                "retry_after_seconds": retry_after,
            },
            headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
        )

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

    # --- Cache (SPEC section 8.5) -----------------------------------------
    # AFTER routing, not before. Routing is ~11ms of local compute and the
    # decision is recorded on a hit too -- so a cached row still shows which
    # tier the router WOULD have chosen. Without that, cache hits would be
    # blind spots in the routing statistics the whole project is measured by.
    cache_lookup_ms = None
    if not request.bypass_cache and cache.available():
        hit, cache_lookup_ms = await cache.lookup(request.prompt)
        cache_lookup_ms = int(cache_lookup_ms)

        if hit is not None:
            # Zero provider calls, zero cost.
            finish(
                "cached",
                complexity_score=decision.complexity_score,
                signals_json=json.dumps(decision.signals),
                tier=hit.tier,
                provider=provider_config["name"],
                model=hit.model,
                cache_hit=1,
                tokens_in=0,
                tokens_out=hit.tokens_out,
                cost_usd=0.0,
                cost_if_large_usd=config.cost_usd(
                    config.baseline_tier, 0, hit.tokens_out),
                classify_ms=classify_ms,
                cache_lookup_ms=cache_lookup_ms,
            )
            return {
                "response": hit.response,
                "routing": {
                    "request_id": request_id,
                    "tier": hit.tier,
                    "provider": provider_config["name"],
                    "model": hit.model,
                    "cache_hit": True,
                    "cache_similarity": hit.similarity,
                    "cached_at": hit.cached_at,
                    "complexity_score": decision.complexity_score,
                    "signals": decision.signals,
                    "escalated": decision.escalated,
                    "fallback_fired": False,
                    "attempts": 0,
                    "tokens_in": 0,
                    "tokens_out": hit.tokens_out,
                    "cost_usd": 0.0,
                    "cost_if_large_usd": round(config.cost_usd(
                        config.baseline_tier, 0, hit.tokens_out), 8),
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "classify_ms": classify_ms,
                    "cache_lookup_ms": cache_lookup_ms,
                    "routing_reason": decision.reason + " (served from cache)",
                },
            }

    try:
        dispatched = await resilience.dispatch(
            prompt=request.prompt,
            tier=tier,
            max_tokens=request.max_tokens,
            config=config,
            providers=app.state.providers,
            breakers=app.state.breakers,
        )
        result = dispatched.result
        # The tier that ANSWERED, which is not always the tier that was chosen
        # -- fallback may have escalated. Everything downstream (cost, the
        # logged row, the response) must reflect what actually happened, not
        # what we intended.
        tier = dispatched.tier
        provider_config = config.provider_for(tier)
    except resilience.AllProvidersFailed as exc:
        # Always 502. Per SPEC section 5, 400 means the *caller's* prompt was
        # empty or over-length; it must never mean "our API key is dead" or
        # "our configured model name is wrong". Both of those are
        # ProviderBadRequest, and from the caller's side they are simply the
        # gateway failing -- which is exactly what 502 says.
        #
        # The four-way taxonomy still matters, but it governs *retry policy*
        # (Stage 5), not the status code we return here.
        # 503 when every provider was circuit-open -- nothing was even tried,
        # so "try again later" is the honest answer. 502 when we tried and they
        # failed. SPEC section 5 distinguishes these deliberately.
        status_code = 503 if exc.all_open else 502
        finish(
            "error",
            str(exc),
            tier=tier,
            provider=provider_config["name"],
            model=provider_config["model"],
            attempts=exc.attempts,
            fallback_fired=1 if len(exc.trail) > 1 else 0,
        )
        body = {
            "error": str(exc),
            "request_id": request_id,
            "attempts": exc.attempts,
            "tried": [
                {"provider": a.provider, "tier": a.tier, "error": a.error}
                for a in exc.trail
            ],
        }
        if exc.all_open:
            cooldown = config.resilience["circuit_cooldown_seconds"]
            body["estimated_recovery_seconds"] = cooldown
        return JSONResponse(status_code=status_code, content=body)

    # --- Cost -------------------------------------------------------------
    # Priced by the provider that ACTUALLY answered, not by the tier's first
    # entry -- within-tier fallback means those can differ.
    cost_usd = config.cost_for_provider(
        tier, dispatched.provider_name, result.tokens_in, result.tokens_out)

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

    # Cache the answer for next time. Fire-and-forget in the background so the
    # caller never waits on Redis -- same reasoning as the database write.
    if not request.bypass_cache and cache.available():
        background.add_task(
            cache.store, request.prompt, result.text, tier,
            result.model, result.tokens_out,
        )

    signals = decision.signals

    finish(
        "success",
        complexity_score=decision.complexity_score,
        signals_json=json.dumps(signals),
        tier=tier,
        provider=dispatched.provider_name,
        model=result.model,
        escalated=1 if dispatched.escalated else 0,
        fallback_fired=1 if dispatched.fallback_fired else 0,
        attempts=dispatched.attempts,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=cost_usd,
        cost_if_large_usd=cost_if_large_usd,
        classify_ms=classify_ms,
        cache_lookup_ms=cache_lookup_ms,
    )

    return {
        "response": result.text,
        "routing": {
            "request_id": request_id,
            "tier": tier,
            "provider": dispatched.provider_name,
            "model": result.model,
            "cache_hit": False,
            "complexity_score": decision.complexity_score,
            "signals": signals,
            "escalated": decision.escalated or dispatched.escalated,
            "fallback_fired": dispatched.fallback_fired,
            "attempts": dispatched.attempts,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "cost_usd": round(cost_usd, 8),
            "cost_if_large_usd": round(cost_if_large_usd, 8),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "provider_latency_ms": result.raw_latency_ms,
            "cache_similarity": None,   # miss: nothing was close enough
            "classify_ms": classify_ms,
            "cache_lookup_ms": cache_lookup_ms,
            "routing_reason": decision.reason,
        },
    }
