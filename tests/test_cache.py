"""Semantic cache tests (SPEC sections 8.5, 9).

The safety tests here are not optional extras. SPEC 8.5:

    "There must be a test in tests/ that demonstrates a wrong-answer case at a
     deliberately loose threshold."

That test is `test_loose_threshold_serves_a_wrong_answer`. It is the honest
counterweight to any hit-rate figure this project reports -- the risk is
executable, not just described in prose.

Tests that need Redis skip cleanly when it is unavailable, so the suite still
runs on a machine without it. A skipped safety test is visible; a silently
absent one is not.
"""

import asyncio
import os

import pytest

# NOTE: the opt-in happens inside the redis_cache fixture, NOT at import.
# pytest imports every test module before running any test, so touching the
# environment here re-enabled the cache for test_api.py too -- which then got
# 200s where it expected 502s, because a cache hit answered before the
# deliberately-failing provider was ever called.

from app import cache
from app import config as config_module
from app.classifier import embedding


# ---------------------------------------------------------------------------
# The inversion guard -- pure functions, no Redis needed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    # Argument order reversed, identical vocabulary. THE case that proves
    # similarity alone is insufficient: these score 0.9903.
    ("Convert 32 Fahrenheit to Celsius.", "Convert 32 Celsius to Fahrenheit."),
    # Antonym swaps
    ("How do I encrypt a file?", "How do I decrypt a file?"),
    ("How do I start a Docker container?", "How do I stop a Docker container?"),
    ("Show me how to enable logging.", "Show me how to disable logging."),
    ("How do I install Docker?", "How do I uninstall Docker?"),
])
def test_guard_catches_inversions(a, b):
    assert cache.is_inverted(a, b), f"guard missed an inversion: {a!r} vs {b!r}"


@pytest.mark.parametrize("a,b", [
    # Legitimate rephrasings. These MUST still be cacheable -- the guard is
    # worthless if it blocks the hits the cache exists to produce.
    ("What is the capital of Japan?", "Whats the capital city of japan"),
    ("How do I reverse a string in Python?", "In Python, how can I reverse a string?"),
    ("Why is the sky blue?", "What makes the sky appear blue?"),
    ("What causes rain?", "Why does it rain?"),
])
def test_guard_allows_rephrasings(a, b):
    assert not cache.is_inverted(a, b), f"guard false-positived: {a!r} vs {b!r}"


def test_guard_order_check_requires_identical_vocabulary():
    """The narrowing that removed two false positives.

    An inversion keeps the words and swaps their order. A rephrasing changes
    the words too. Without this restriction the order check fired on ordinary
    rewording.
    """
    # exact positional exchange -> inversion
    assert cache.is_inverted("copy files from server to laptop",
                             "copy files from laptop to server")
    # a word merely migrating -> rephrasing, not an inversion
    assert not cache.is_inverted("How do I reverse a string in Python?",
                                 "In Python, how can I reverse a string?")


# ---------------------------------------------------------------------------
# Redis-backed tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cfg():
    return config_module.load()


@pytest.fixture(scope="module")
def loop():
    """One event loop for the module.

    A redis.asyncio client binds to the loop it was created on. `asyncio.run()`
    creates and closes a NEW loop every call, so the second use of the same
    client raises "Event loop is closed" -- which is exactly what happened
    first time round. One loop, created once, used throughout.
    """
    new_loop = asyncio.new_event_loop()
    yield new_loop
    new_loop.close()


@pytest.fixture(scope="module")
def redis_cache(cfg, loop):
    """Connect once, or skip the whole module.

    conftest.py sets MODELMUX_CACHE_DISABLED for the whole suite. This module
    is about the cache, so it lifts that for the duration of its own tests and
    restores it afterwards -- scoped to the fixture, never at import time.
    """
    previous = os.environ.pop("MODELMUX_CACHE_DISABLED", None)
    embedding.init()
    loop.run_until_complete(cache.init(cfg))
    if not cache.available():
        if previous is not None:
            os.environ["MODELMUX_CACHE_DISABLED"] = previous
        pytest.skip("Redis unavailable -- start it with "
                    "`wsl -e sudo service redis-server start`")
    yield cache

    loop.run_until_complete(cache.clear())
    cache._available = False
    if previous is not None:
        os.environ["MODELMUX_CACHE_DISABLED"] = previous


def test_store_then_exact_lookup_hits(redis_cache, loop):
    loop.run_until_complete(cache.clear())

    async def run():
        await cache.store("What is the capital of Japan?", "Tokyo.",
                          "small", "m", 3)
        return await cache.lookup("What is the capital of Japan?")

    hit, ms = loop.run_until_complete(run())
    assert hit is not None
    assert hit.response == "Tokyo."
    assert hit.similarity > 0.99


def test_unrelated_prompt_misses(redis_cache, loop):
    loop.run_until_complete(cache.clear())

    async def run():
        await cache.store("What is the capital of Japan?", "Tokyo.",
                          "small", "m", 3)
        return await cache.lookup("Explain quantum entanglement in detail.")

    hit, _ = loop.run_until_complete(run())
    assert hit is None


def test_lookup_is_within_budget(redis_cache, loop):
    """SPEC section 2: cache lookup must stay under 30ms."""
    loop.run_until_complete(cache.clear())

    async def run():
        for i in range(50):
            await cache.store(f"Question number {i} about topic {i}.",
                              f"Answer {i}", "small", "m", 3)
        _, ms = await cache.lookup("Question number 7 about topic 7.")
        return ms

    assert loop.run_until_complete(run()) < 30


def test_eviction_keeps_the_cache_bounded(redis_cache, loop, cfg):
    """Past max_entries, the oldest entries are dropped (FIFO)."""
    loop.run_until_complete(cache.clear())
    original = cfg.cache["max_entries"]
    cfg.cache["max_entries"] = 5
    try:
        async def run():
            for i in range(9):
                await cache.store(f"Distinct question {i}?", f"Answer {i}",
                                  "small", "m", 3)
            return await cache.size()

        assert loop.run_until_complete(run()) == 5
    finally:
        cfg.cache["max_entries"] = original


# ---------------------------------------------------------------------------
# THE SAFETY TEST -- required by SPEC section 8.5
# ---------------------------------------------------------------------------

def test_loose_threshold_serves_a_wrong_answer(redis_cache, loop, cfg):
    """DEMONSTRATES THE RISK. Required by SPEC section 8.5.

    This test does not check that our code is correct. It proves that a
    carelessly-set threshold serves a **factually wrong answer to a real
    question**, with no indication anything was substituted.

    "What is the tallest mountain in Africa?" (Kilimanjaro) and
    "What is the tallest mountain in Asia?" (Everest) score ~0.80 similarity.
    Set the threshold below that and the second question is answered with the
    first one's answer.

    The inversion guard does NOT save us here: these prompts are not
    inversions, they are entity substitutions. Only the threshold stands
    between a user and a wrong answer -- which is precisely why the threshold
    was chosen by measurement rather than taste (DECISIONS.md D16).
    """
    loop.run_until_complete(cache.clear())
    original = cfg.cache["similarity_threshold"]
    cfg.cache["similarity_threshold"] = 0.75      # deliberately reckless
    try:
        async def run():
            await cache.store("What is the tallest mountain in Africa?",
                              "Mount Kilimanjaro.", "small", "m", 4)
            return await cache.lookup("What is the tallest mountain in Asia?")

        hit, _ = loop.run_until_complete(run())

        assert hit is not None, "expected the loose threshold to produce a hit"
        assert hit.response == "Mount Kilimanjaro."
        # The correct answer is Everest. The cache just served Kilimanjaro,
        # confidently, to a question about Asia.
        assert "Everest" not in hit.response
    finally:
        cfg.cache["similarity_threshold"] = original


def test_shipped_threshold_blocks_that_same_wrong_answer(redis_cache, loop, cfg):
    """The other half of the story: at the configured 0.88 it is a miss."""
    loop.run_until_complete(cache.clear())

    async def run():
        await cache.store("What is the tallest mountain in Africa?",
                          "Mount Kilimanjaro.", "small", "m", 4)
        return await cache.lookup("What is the tallest mountain in Asia?")

    hit, _ = loop.run_until_complete(run())
    assert hit is None, "the shipped threshold must not serve this"


def test_inversion_survives_a_high_similarity_score(redis_cache, loop, cfg):
    """The case no threshold can catch.

    These two score 0.9903 -- higher than most legitimate rephrasings -- and
    have opposite answers. Only the lexical guard blocks it. Without the guard
    this is a wrong answer at ANY threshold.
    """
    loop.run_until_complete(cache.clear())

    async def run():
        await cache.store("Convert 32 Fahrenheit to Celsius.", "0 degrees C.",
                          "small", "m", 4)
        return await cache.lookup("Convert 32 Celsius to Fahrenheit.")

    hit, _ = loop.run_until_complete(run())
    assert hit is None, (
        "the inversion guard failed -- a 0.99-similarity inversion got through"
    )


# ---------------------------------------------------------------------------
# END TO END -- required by SPEC section 9
# ---------------------------------------------------------------------------

def test_cache_hit_calls_no_provider(redis_cache, loop, monkeypatch):
    """SPEC section 9: "Cache hit returns without calling any provider --
    assert the mock was not invoked."

    Every other test in this module calls cache.lookup() directly, which proves
    the cache works but NOT that the endpoint actually short-circuits before
    reaching a provider. That is the claim the whole savings figure rests on,
    so it needs asserting through the real request path.
    """
    import os
    import time

    monkeypatch.setenv("MODELMUX_MOCK_PROVIDERS", "1")
    os.environ.pop("MODELMUX_CACHE_DISABLED", None)

    from fastapi.testclient import TestClient

    from app.main import app

    loop.run_until_complete(cache.clear())

    with TestClient(app) as client:
        # MODELMUX_MOCK_PROVIDERS builds a SEPARATE mock per provider name, so
        # picking one arbitrarily counts calls on an adapter the router may
        # never choose. Summing across all of them counts "did ANY provider get
        # called", which is the actual question.
        mocks = list(client.app.state.providers.values())
        for m in mocks:
            m.text = "Tokyo."
        total_calls = lambda: sum(m.calls for m in mocks)

        prompt = "What is the capital of Japan?"

        first = client.post("/v1/chat", json={"prompt": prompt}).json()
        assert first["routing"]["cache_hit"] is False
        calls_after_miss = total_calls()
        assert calls_after_miss >= 1, "a miss must call a provider"

        # The store is a BackgroundTask, so it lands just after the response.
        time.sleep(0.5)

        second = client.post("/v1/chat", json={"prompt": prompt}).json()

        assert second["routing"]["cache_hit"] is True
        assert second["response"] == "Tokyo."
        # THE ASSERTION THAT MATTERS: no additional provider call.
        assert total_calls() == calls_after_miss, (
            f"a cache hit called a provider: "
            f"{calls_after_miss} -> {total_calls()}"
        )
        assert second["routing"]["cost_usd"] == 0.0
        assert second["routing"]["attempts"] == 0

    loop.run_until_complete(cache.clear())
