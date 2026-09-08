"""Fake provider (SPEC section 8.6).

Exists so tests and load tests never touch a real provider. Two reasons that
matters: free-tier rate limits (SPEC section 13), and the fact that a test
suite whose results depend on a third party's uptime is not a test suite.

It is also what lets Stage 1 be verified end to end while the real Groq key is
invalid -- the request path, the cost arithmetic, and the database row are all
exercisable without a single network call.
"""

import asyncio

from app.providers.base import (
    Provider,
    ProviderBadRequest,
    ProviderRateLimited,
    ProviderResult,
    ProviderServerError,
    ProviderTimeout,
)

# Name -> exception, so a test can ask for a specific failure by string.
FAILURE_MODES = {
    "timeout": ProviderTimeout("mock timeout"),
    "rate_limited": ProviderRateLimited("mock rate limited", retry_after_seconds=1.0),
    "server_error": ProviderServerError("mock 500"),
    "bad_request": ProviderBadRequest("mock 400"),
}


class MockProvider(Provider):
    """Returns canned text after a configurable delay.

    `fail_with` makes it raise instead -- that is how Stage 5's retry, fallback
    and circuit-breaker tests will force failures deterministically, without
    needing a provider to actually be down.
    """

    name = "mock"

    def __init__(
        self,
        text: str = "mock response",
        delay_seconds: float = 0.0,
        fail_with: str | None = None,
        tokens_in: int = 10,
        tokens_out: int = 5,
    ):
        self.text = text
        self.delay_seconds = delay_seconds
        self.fail_with = fail_with
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        # Call counting is how the cache test proves a hit served without
        # touching a provider (SPEC section 9).
        self.calls = 0

    async def complete(
        self, prompt: str, model: str, max_tokens: int
    ) -> ProviderResult:
        self.calls += 1

        if self.delay_seconds:
            # asyncio.sleep, never time.sleep: the latter would block the whole
            # event loop and stall every other in-flight request.
            await asyncio.sleep(self.delay_seconds)

        if self.fail_with:
            if self.fail_with not in FAILURE_MODES:
                raise ValueError(f"unknown failure mode: {self.fail_with}")
            raise FAILURE_MODES[self.fail_with]

        return ProviderResult(
            text=self.text,
            model=model,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            raw_latency_ms=int(self.delay_seconds * 1000),
        )
