"""Anthropic Claude adapter (SPEC section 8.6).

The large tier -- and therefore the baseline every savings figure is measured
against. If this adapter's token counts are wrong, `cost_if_large_usd` is wrong,
and the headline number of the whole project is wrong with it.

A third API shape again: `x-api-key` rather than a bearer token, a mandatory
`anthropic-version` header, and `max_tokens` is required rather than optional.
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

URL = "https://api.anthropic.com/v1/messages"

# Anthropic pins behaviour to a dated API version. Sending it explicitly means
# a future default change cannot silently alter our responses.
API_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def complete(
        self, prompt: str, model: str, max_tokens: int
    ) -> ProviderResult:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ProviderBadRequest("ANTHROPIC_API_KEY is not set")

        started = time.perf_counter()
        try:
            response = await self.client.post(
                URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": API_VERSION,
                },
                json={
                    "model": model,
                    # Required by this API, not optional as it is elsewhere.
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"anthropic timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise ProviderServerError(f"could not reach anthropic: {exc}") from exc

        raw_latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code != 200:
            raise _translate_error(response)

        try:
            body = response.json()
            # `content` is a list of typed blocks. Only text blocks are joined;
            # other block types are ignored rather than crashing on them.
            text = "".join(
                block.get("text", "")
                for block in body["content"]
                if block.get("type") == "text"
            )
            usage = body.get("usage", {})
            return ProviderResult(
                text=text,
                model=body.get("model", model),
                tokens_in=usage.get("input_tokens", 0),
                tokens_out=usage.get("output_tokens", 0),
                raw_latency_ms=raw_latency_ms,
            )
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderServerError(
                f"unexpected anthropic response shape: {exc}"
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
            f"anthropic rate limited: {detail}",
            retry_after_seconds=float(retry_after) if retry_after else None,
        )
    # 529 is Anthropic's "overloaded" -- transient, and worth retrying.
    if status >= 500 or status == 529:
        return ProviderServerError(f"anthropic {status}: {detail}")
    return ProviderBadRequest(f"anthropic {status}: {detail}")
