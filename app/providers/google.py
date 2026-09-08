"""Google Gemini adapter (SPEC section 8.6).

The mid tier. Unlike Groq, Gemini is **not** OpenAI-compatible -- different
URL shape, different request body, different response body, and the API key
goes in a header rather than an Authorization bearer token.

That difference is the point of this file. Everything provider-specific is
contained here; `router.py` and `main.py` do not change to accommodate it.
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

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GoogleProvider(Provider):
    name = "google"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def complete(
        self, prompt: str, model: str, max_tokens: int
    ) -> ProviderResult:
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ProviderBadRequest("GOOGLE_API_KEY is not set")

        # The model name is part of the URL path here, not a body field.
        url = f"{BASE_URL}/{model}:generateContent"

        started = time.perf_counter()
        try:
            response = await self.client.post(
                url,
                # Key in a header, not the query string: a URL with a secret in
                # it leaks into logs, proxies and browser history.
                headers={"x-goog-api-key": api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": max_tokens},
                },
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"google timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise ProviderServerError(f"could not reach google: {exc}") from exc

        raw_latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code != 200:
            raise _translate_error(response)

        try:
            body = response.json()
            candidate = body["candidates"][0]
            text = candidate["content"]["parts"][0]["text"]
            usage = body.get("usageMetadata", {})
            return ProviderResult(
                text=text,
                model=body.get("modelVersion", model),
                tokens_in=usage.get("promptTokenCount", 0),
                tokens_out=usage.get("candidatesTokenCount", 0),
                raw_latency_ms=raw_latency_ms,
            )
        except (KeyError, IndexError, ValueError) as exc:
            # Gemini returns 200 with no `parts` when it blocks a response on
            # safety grounds. That is a real, reachable case -- not defensive
            # padding -- and it must not surface as a raw KeyError.
            raise ProviderServerError(
                f"unexpected google response shape: {exc}"
            ) from exc


def _translate_error(response: httpx.Response) -> Exception:
    """Map an HTTP status onto the shared taxonomy in base.py."""
    status = response.status_code
    try:
        detail = response.json().get("error", {}).get("message", "")
    except ValueError:
        detail = ""
    detail = (detail or response.text)[:200]

    if status == 429:
        retry_after = response.headers.get("retry-after")
        return ProviderRateLimited(
            f"google rate limited: {detail}",
            retry_after_seconds=float(retry_after) if retry_after else None,
        )
    if status >= 500:
        return ProviderServerError(f"google {status}: {detail}")
    return ProviderBadRequest(f"google {status}: {detail}")
