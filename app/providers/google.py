"""Google Gemini adapter (SPEC section 8.6).

Serves the **large** tier (DECISIONS.md D38); it was originally specced for
mid. Unlike Groq, Gemini is **not** OpenAI-compatible -- different
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

        body = {}
        try:
            body = response.json()
            candidate = body["candidates"][0]
            text = candidate["content"]["parts"][0]["text"]
            usage = body.get("usageMetadata", {})
        except (KeyError, IndexError, ValueError) as exc:
            # Gemini returns 200 with no `parts` when it blocks a response on
            # safety grounds, and ALSO when a thinking model spends the whole
            # output budget reasoning. Both are real, reachable cases -- not
            # defensive padding -- and neither must surface as a raw KeyError.
            #
            # `body` is pre-bound above so this message still works when it
            # was response.json() itself that raised.
            raise ProviderServerError(
                f"unexpected google response shape: {exc} "
                f"(finish_reason={_finish_reason(body)})"
            ) from exc

        # Same guard as groq.py, for the same reason (DECISIONS.md D32).
        # Gemini 3 models think by default; if `maxOutputTokens` is consumed
        # by reasoning, the API returns 200 with empty text AND STILL BILLS
        # for the thinking tokens. An empty string is indistinguishable from
        # a real answer to every layer above -- it would be cached, logged as
        # a success, and counted in the savings figure as a cheap win.
        if not text:
            raise ProviderServerError(
                f"google returned empty content "
                f"(finish_reason={_finish_reason(body)}); the output budget was "
                f"likely consumed by thinking tokens -- raise max_tokens or "
                f"lower thinkingLevel"
            )

        return ProviderResult(
            text=text,
            model=body.get("modelVersion", model),
            tokens_in=usage.get("promptTokenCount", 0),
            tokens_out=_billable_output_tokens(usage),
            raw_latency_ms=raw_latency_ms,
        )


def _finish_reason(body: dict) -> str:
    try:
        return str(body["candidates"][0].get("finishReason", "unknown"))
    except (KeyError, IndexError, TypeError):
        return "unknown"


def _billable_output_tokens(usage: dict) -> int:
    """Output tokens as GOOGLE BILLS them, not as it labels them.

    Gemini thinking models report reasoning separately from the answer, but
    charge for both at the output rate: "response pricing is the sum of output
    tokens and thinking tokens". Reading `candidatesTokenCount` alone
    therefore UNDERSTATES cost on every thinking model -- and this project's
    entire claim is a cost comparison, so that error would flow straight into
    the headline (the D31 failure mode).

    The field has been spelled `thoughtsTokenCount` and `total_thought_tokens`
    across API versions, so both are checked.

    The `totalTokenCount` reconciliation is the safety net: if Google reports
    a total larger than the parts we recognise, those tokens were billed to
    somebody and it was us. Attributing the remainder to output OVERSTATES
    our cost slightly, which makes the savings figure look WORSE. That is the
    honest direction to be wrong in.
    """
    prompt = usage.get("promptTokenCount", 0) or 0
    answer = usage.get("candidatesTokenCount", 0) or 0
    thinking = (
        usage.get("thoughtsTokenCount")
        or usage.get("total_thought_tokens")
        or 0
    )
    out = answer + thinking

    total = usage.get("totalTokenCount") or 0
    unattributed = total - (prompt + out)
    if unattributed > 0:
        out += unattributed

    return out


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
