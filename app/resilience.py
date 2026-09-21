"""Retry, fallback, and circuit breaking (SPEC section 8.7).

Three mechanisms, applied in this order:

    RETRY      same provider, a moment later
    FALLBACK   next provider in the tier; if none, escalate a tier
    CIRCUIT    stop asking a provider that keeps failing

Each answers a different question. Retry handles a blip. Fallback handles one
provider being down. The circuit breaker handles a provider being down *and
stops us paying the timeout to discover it every single request.*

-----------------------------------------------------------------------------
THIS IS WHERE THE ERROR TAXONOMY EARNS ITS KEEP
-----------------------------------------------------------------------------
`providers/base.py` normalises every provider failure into four types. That was
never cosmetic -- it IS the retry policy:

    ProviderTimeout       retry -- transient
    ProviderRateLimited   retry with backoff, honour Retry-After
    ProviderServerError   retry -- probably transient
    ProviderBadRequest    NEVER retry -- deterministic

Retrying a dead API key is not conservative, it is wasteful: three attempts,
three identical 401s, three times the latency, and a caller kept waiting for an
answer that was never coming.
"""

import asyncio
import random
import sys
import time
from dataclasses import dataclass, field
from enum import Enum

from app.providers.base import (
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderResult,
)

# Errors worth trying again. The complement of this set is
# ProviderBadRequest -- listed by exclusion so that a NEW error type added to
# base.py defaults to "do not retry", which is the safe direction.
RETRYABLE = (ProviderError,)


class CircuitState(str, Enum):
    CLOSED = "closed"        # normal
    OPEN = "open"            # skipping this provider entirely
    HALF_OPEN = "half_open"  # allowing exactly one probe


@dataclass
class Circuit:
    """Failure state for ONE provider.

    SPEC 8.7: state lives in memory, and single-process is acceptable. Stated
    as a limitation rather than hidden -- with several workers, each keeps its
    own view, so a provider can be open in one process and closed in another.
    The consequence is uneven traffic to a failing provider, not incorrect
    answers.
    """

    name: str
    state: CircuitState = CircuitState.CLOSED
    failures: list[float] = field(default_factory=list)  # timestamps
    opened_at: float | None = None
    last_error: str | None = None
    probe_in_flight: bool = False

    def _prune(self, window_seconds: float) -> None:
        """Drop failures older than the window.

        The threshold is "N failures **within** a window", not "N failures
        ever". Without pruning, a provider that failed 5 times over a month
        would trip the breaker on the fifth -- which says nothing about its
        health right now.
        """
        cutoff = time.monotonic() - window_seconds
        self.failures = [t for t in self.failures if t > cutoff]

    def allows_request(self, cooldown_seconds: float) -> bool:
        """Whether to attempt this provider at all."""
        if self.state is CircuitState.CLOSED:
            return True

        if self.state is CircuitState.OPEN:
            elapsed = time.monotonic() - (self.opened_at or 0)
            if elapsed >= cooldown_seconds:
                # Cooldown served. Move to half-open and let ONE request past.
                #
                # `probe_in_flight = True` is load-bearing and was missing in
                # the first version: this branch returned True without claiming
                # the probe, so the NEXT caller fell through to the HALF_OPEN
                # branch below, found the flag unset, and was also allowed
                # through. Two probes instead of one -- the exact thundering
                # herd the breaker exists to prevent. Caught by
                # test_half_open_allows_exactly_one_probe.
                self.state = CircuitState.HALF_OPEN
                self.probe_in_flight = True
                return True
            return False

        # HALF_OPEN: exactly one probe at a time. Letting several through
        # would hammer a provider that is still recovering -- the thundering
        # herd the breaker exists to prevent.
        if self.probe_in_flight:
            return False
        self.probe_in_flight = True
        return True

    def record_success(self) -> None:
        self.state = CircuitState.CLOSED
        self.failures.clear()
        self.opened_at = None
        self.probe_in_flight = False

    def record_failure(self, error: str, threshold: int, window: float) -> None:
        self.last_error = error

        if self.state is CircuitState.HALF_OPEN:
            # The probe failed. Reopen and restart the cooldown -- do NOT give
            # it another immediate try.
            self.state = CircuitState.OPEN
            self.opened_at = time.monotonic()
            self.probe_in_flight = False
            return

        self.failures.append(time.monotonic())
        self._prune(window)
        if len(self.failures) >= threshold:
            self.state = CircuitState.OPEN
            self.opened_at = time.monotonic()

    def snapshot(self, cooldown_seconds: float) -> dict:
        """State for GET /health/providers."""
        recovers_in = None
        if self.state is CircuitState.OPEN and self.opened_at is not None:
            recovers_in = max(
                0.0, cooldown_seconds - (time.monotonic() - self.opened_at)
            )
        return {
            "provider": self.name,
            "state": self.state.value,
            "recent_failures": len(self.failures),
            "last_error": self.last_error,
            "recovers_in_seconds": round(recovers_in, 1) if recovers_in else None,
        }


class CircuitBreakers:
    """One circuit per provider name."""

    def __init__(self, config):
        self.config = config
        self._circuits: dict[str, Circuit] = {}

    def for_provider(self, name: str) -> Circuit:
        if name not in self._circuits:
            self._circuits[name] = Circuit(name=name)
        return self._circuits[name]

    def allows(self, name: str) -> bool:
        cooldown = self.config.resilience["circuit_cooldown_seconds"]
        return self.for_provider(name).allows_request(cooldown)

    def record_success(self, name: str) -> None:
        self.for_provider(name).record_success()

    def record_failure(self, name: str, error: str) -> None:
        self.for_provider(name).record_failure(
            error,
            self.config.resilience["circuit_failure_threshold"],
            self.config.resilience["circuit_window_seconds"],
        )

    def snapshot(self) -> list[dict]:
        cooldown = self.config.resilience["circuit_cooldown_seconds"]
        return [c.snapshot(cooldown) for c in self._circuits.values()]


@dataclass
class Attempt:
    """One provider call, recorded whether it succeeded or not."""

    provider: str
    model: str
    tier: str
    error: str | None = None
    # True when the provider was never called because its breaker was open.
    # A skip is a CONSEQUENCE of earlier failures, not a failure itself, so it
    # must not be reported as the reason the request died. See DECISIONS D34.
    skipped: bool = False


@dataclass
class DispatchResult:
    """The outcome of trying however many providers it took."""

    result: ProviderResult
    tier: str
    provider_name: str
    attempts: int
    fallback_fired: bool
    escalated: bool
    trail: list[Attempt]


class AllProvidersFailed(Exception):
    """Every provider in every tried tier failed, or was circuit-open."""

    def __init__(self, message: str, attempts: int, trail: list[Attempt],
                 all_open: bool = False):
        super().__init__(message)
        self.attempts = attempts
        self.trail = trail
        # Distinguishes 503 (everything circuit-open, come back later) from
        # 502 (we tried and they failed). SPEC section 5.
        self.all_open = all_open


def backoff_seconds(attempt: int, base: float) -> float:
    """Exponential backoff with jitter: base * 2^attempt, randomised.

    **Jitter is not decoration.** Without it, every client that failed at the
    same moment retries at the same moment, producing a synchronised burst that
    re-triggers the very rate limit or overload they are backing off from --
    the thundering herd. Spreading retries randomly across the window is what
    makes backoff actually work under load.
    """
    window = base * (2 ** attempt)
    return random.uniform(0, window)


async def dispatch(prompt: str, tier: str, max_tokens: int, config,
                   providers: dict, breakers: CircuitBreakers) -> DispatchResult:
    """Get an answer, trying harder as things fail.

    Order of escalation:
      1. each provider in the tier, with retries
      2. the next tier up, if this one is exhausted and escalation is enabled

    Raises AllProvidersFailed when nothing worked.
    """
    tier_order = ["small", "mid", "large"]
    start_index = tier_order.index(tier)
    escalate = config.resilience.get("escalate_on_tier_exhausted", True)

    trail: list[Attempt] = []
    attempts = 0
    fallback_fired = False
    escalated = False
    saw_open_circuit = False
    tried_any = False

    tiers_to_try = tier_order[start_index:] if escalate else [tier]

    for tier_index, current_tier in enumerate(tiers_to_try):
        if tier_index > 0:
            # We only reach a higher tier because the one below was exhausted.
            escalated = True

        for provider_index, entry in enumerate(
                config.tiers[current_tier].get("providers") or []):
            name = entry["name"]
            provider = providers.get(name)
            if provider is None:
                continue

            if not breakers.allows(name):
                saw_open_circuit = True
                trail.append(Attempt(name, entry["model"], current_tier,
                                     "circuit open -- skipped", skipped=True))
                continue

            if tier_index > 0 or provider_index > 0:
                fallback_fired = True

            tried_any = True
            outcome = await _try_one_provider(
                provider, prompt, entry, current_tier, max_tokens, config,
                breakers, trail,
            )
            attempts += outcome["attempts"]

            if outcome["result"] is not None:
                return DispatchResult(
                    result=outcome["result"],
                    tier=current_tier,
                    provider_name=name,
                    attempts=attempts,
                    fallback_fired=fallback_fired,
                    escalated=escalated,
                    trail=trail,
                )

    # Nothing worked. If we never actually called anybody because every circuit
    # was open, that is a 503 ("try later"), not a 502 ("they broke").
    all_open = saw_open_circuit and not tried_any

    # Report the last provider that was actually CALLED, not the last entry in
    # the trail. Once a breaker opens, every later entry is "circuit open --
    # skipped" -- true, but it names the symptom and buries the cause. An
    # operator reading the log needs "mock 500", not "we declined to try".
    # Skips are still in the trail; they are just not the headline.
    called = [a for a in trail if not a.skipped]
    if called:
        last = called[-1].error
    elif trail:
        last = trail[-1].error
    else:
        last = "no providers configured"
    raise AllProvidersFailed(
        f"all providers failed; last error: {last}",
        attempts=attempts, trail=trail, all_open=all_open,
    )


async def _try_one_provider(provider, prompt, entry, tier, max_tokens, config,
                            breakers, trail) -> dict:
    """Call one provider, retrying transient failures.

    Returns {"result": ProviderResult|None, "attempts": int}.
    """
    name = entry["name"]
    max_retries = config.resilience["max_retries"]
    base = config.resilience["backoff_base_seconds"]
    attempts = 0

    for attempt in range(max_retries + 1):
        attempts += 1
        try:
            result = await provider.complete(
                prompt=prompt, model=entry["model"], max_tokens=max_tokens,
            )
            breakers.record_success(name)
            trail.append(Attempt(name, entry["model"], tier))
            return {"result": result, "attempts": attempts}

        except ProviderBadRequest as exc:
            # Deterministic. Retrying produces the identical failure, so stop
            # immediately and let fallback try a DIFFERENT provider.
            breakers.record_failure(name, str(exc))
            trail.append(Attempt(name, entry["model"], tier, str(exc)))
            return {"result": None, "attempts": attempts}

        except ProviderError as exc:
            breakers.record_failure(name, str(exc))
            trail.append(Attempt(name, entry["model"], tier, str(exc)))

            if attempt >= max_retries:
                return {"result": None, "attempts": attempts}

            delay = backoff_seconds(attempt, base)
            if isinstance(exc, ProviderRateLimited) and exc.retry_after_seconds:
                # The provider told us how long to wait. Believe it over our
                # own guess -- but never wait less than the backoff we would
                # have chosen anyway.
                delay = max(delay, exc.retry_after_seconds)

            print(f"[resilience] {name} failed ({exc}); "
                  f"retry {attempt + 1}/{max_retries} in {delay:.2f}s",
                  file=sys.stderr)
            await asyncio.sleep(delay)

    return {"result": None, "attempts": attempts}
