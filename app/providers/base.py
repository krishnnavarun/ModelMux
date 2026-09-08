"""Provider abstraction and the normalised error taxonomy (SPEC section 8.6).

Every provider adapter translates its own API shape into `ProviderResult`, and
its own failures into exactly four exception types. The resilience layer
(Stage 5) will only ever see those four -- it must never need to know that
Groq says 429 while another provider says 503 with a JSON body.

The taxonomy is not cosmetic. It encodes what the retry logic is allowed to do:

    ProviderTimeout       retry -- transient
    ProviderRateLimited   retry with backoff -- transient
    ProviderServerError   retry -- probably transient
    ProviderBadRequest    NEVER retry -- deterministic, will fail identically
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ProviderError(Exception):
    """Base class. Nothing raises this directly -- catch it to catch all four."""


class ProviderTimeout(ProviderError):
    """Provider did not respond within the configured deadline."""


class ProviderRateLimited(ProviderError):
    """429. Back off before retrying."""

    def __init__(self, message: str, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderServerError(ProviderError):
    """5xx, or a response we could not parse. Provider's fault."""


class ProviderBadRequest(ProviderError):
    """4xx that is our fault: bad key, unknown model, malformed body.

    Retrying is pointless and wastes the caller's time -- the request is
    deterministically invalid.
    """


@dataclass
class ProviderResult:
    """What every adapter returns on success.

    `raw_latency_ms` is the provider call alone. main.py measures the
    end-to-end figure separately; keeping both lets us show how much of the
    latency is ours versus theirs.
    """

    text: str
    model: str
    tokens_in: int
    tokens_out: int
    raw_latency_ms: int


class Provider(ABC):
    """One adapter per upstream API."""

    name: str

    @abstractmethod
    async def complete(
        self, prompt: str, model: str, max_tokens: int
    ) -> ProviderResult:
        """Send one prompt, return normalised result, raise a normalised error."""
        raise NotImplementedError
