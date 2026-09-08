"""Groq provider adapter (SPEC section 8.6).

Knows how to talk to one API and nothing else. No tiers, no cost, no caching,
no HTTP status codes of our own -- that lives in router.py and main.py. The
adapter's whole job is translation, in both directions:

    our call shape  ->  Groq's JSON
    Groq's JSON     ->  ProviderResult
    Groq's failures ->  the four normalised errors in base.py
"""

import os
import time

import httpx

from app.providers.base import (
    Provider,
    ProviderBadRequest,
    ProviderRateLimited,
    ProviderResult,
    ProviderServerError,
    ProviderTimeout,
)

# Groq exposes an OpenAI-compatible API, which is why the path says "openai".
# Not a typo, and it does not mean we are calling OpenAI. See learnings/.
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqProvider(Provider):
    name = "groq"

    def __init__(self, client: httpx.AsyncClient):
        # The client is injected, not created here. main.py owns one for the
        # process lifetime so requests reuse warm TLS connections.
        self.client = client

    async def complete(
        self, prompt: str, model: str, max_tokens: int
    ) -> ProviderResult:
        # SPEC section 10: keys come from the environment only, never from
        # config.yaml, and never appear in an error returned to the caller.
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise ProviderBadRequest("GROQ_API_KEY is not set")

        started = time.perf_counter()
        try:
            response = await self.client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"groq timed out: {exc}") from exc
        except httpx.RequestError as exc:
            # DNS failure, refused connection, TLS problem. No HTTP status at
            # all. Treated as a server error because it is transient and worth
            # retrying.
            raise ProviderServerError(f"could not reach groq: {exc}") from exc

        raw_latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code != 200:
            raise self._translate_error(response)

        try:
            body = response.json()
            usage = body.get("usage", {})
            return ProviderResult(
                text=body["choices"][0]["message"]["content"],
                model=body.get("model", model),
                tokens_in=usage.get("prompt_tokens", 0),
                tokens_out=usage.get("completion_tokens", 0),
                raw_latency_ms=raw_latency_ms,
            )
        except (KeyError, IndexError, ValueError) as exc:
            # A 200 whose body is not the shape we expect is still the
            # provider misbehaving.
            raise ProviderServerError(f"unexpected groq response shape: {exc}") from exc

    @staticmethod
    def _translate_error(response: httpx.Response) -> Exception:
        """Map an HTTP status onto the taxonomy in base.py.

        The status code is what decides whether the resilience layer may
        retry, so this mapping is the whole point of the adapter.
        """
        status = response.status_code

        # SPEC section 10: never hand a raw provider body to the caller -- it
        # can carry internal detail. Extract the message, cap the length.
        try:
            detail = response.json().get("error", {}).get("message", "")
        except ValueError:
            detail = ""
        detail = (detail or response.text)[:200]

        if status == 429:
            retry_after = response.headers.get("retry-after")
            return ProviderRateLimited(
                f"groq rate limited: {detail}",
                retry_after_seconds=float(retry_after) if retry_after else None,
            )
        if status >= 500:
            return ProviderServerError(f"groq {status}: {detail}")
        # 400, 401, 404 -- bad key, unknown model, malformed request. Retrying
        # produces the identical failure, so it must not be retried.
        return ProviderBadRequest(f"groq {status}: {detail}")
