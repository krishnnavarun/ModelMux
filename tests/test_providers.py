"""Provider adapter unit tests (SPEC section 9).

The important ones are the status-code translations. That mapping is what
decides whether Stage 5 is allowed to retry, so getting it wrong means either
hammering a provider with requests that cannot succeed, or giving up on one
that would have.
"""

import httpx
import pytest

from app.providers.base import (
    ProviderBadRequest,
    ProviderRateLimited,
    ProviderServerError,
)
from app.providers.groq import GroqProvider
from app.providers.mock import MockProvider


def _response(status: int, body: dict | None = None, headers: dict | None = None):
    return httpx.Response(
        status_code=status,
        json=body if body is not None else {},
        headers=headers or {},
        request=httpx.Request("POST", "https://example.invalid"),
    )


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, ProviderBadRequest),
        (401, ProviderBadRequest),   # dead key -- never worth retrying
        (404, ProviderBadRequest),   # unknown model -- never worth retrying
        (429, ProviderRateLimited),
        (500, ProviderServerError),
        (503, ProviderServerError),
    ],
)
def test_status_maps_to_correct_error_type(status, expected):
    error = GroqProvider._translate_error(_response(status))
    assert isinstance(error, expected)


def test_rate_limit_captures_retry_after():
    error = GroqProvider._translate_error(
        _response(429, headers={"retry-after": "2.5"})
    )
    assert error.retry_after_seconds == 2.5


def test_rate_limit_without_header_is_none():
    assert GroqProvider._translate_error(_response(429)).retry_after_seconds is None


def test_provider_message_is_truncated():
    """SPEC section 10: never hand a raw provider body to the caller."""
    body = {"error": {"message": "z" * 5000}}
    error = GroqProvider._translate_error(_response(500, body))
    assert len(str(error)) < 300


# --- mock provider ---------------------------------------------------------

@pytest.mark.anyio
async def test_mock_counts_calls():
    provider = MockProvider()
    await provider.complete("hi", "m", 100)
    await provider.complete("hi", "m", 100)
    assert provider.calls == 2


def test_mock_rejects_unknown_failure_mode():
    import asyncio
    provider = MockProvider(fail_with="nonsense")
    with pytest.raises(ValueError, match="unknown failure mode"):
        asyncio.run(provider.complete("hi", "m", 100))


def test_mock_raises_requested_failure():
    import asyncio
    provider = MockProvider(fail_with="rate_limited")
    with pytest.raises(ProviderRateLimited):
        asyncio.run(provider.complete("hi", "m", 100))
