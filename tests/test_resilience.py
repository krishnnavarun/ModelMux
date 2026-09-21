"""Retry, fallback and circuit breaker tests (SPEC sections 8.7, 9).

SPEC section 9 requires:
  - the circuit breaker state machine through closed -> open -> half_open ->
    closed, AND half_open -> open on probe failure
  - forced provider failure triggers retry, then fallback
  - all providers failing returns 502 and still writes a row

None of this waits for a real outage. `MockProvider.fail_with` makes failure
deterministic, which is the entire reason it was built on Day 1.
"""

import asyncio
import time

import pytest

from app import config as config_module
from app import resilience
from app.providers.base import (
    ProviderBadRequest,
    ProviderRateLimited,
    ProviderServerError,
)
from app.providers.mock import MockProvider
from app.resilience import Circuit, CircuitBreakers, CircuitState


@pytest.fixture
def cfg():
    """A fresh config with fast, test-sized resilience settings."""
    c = config_module.load()
    c.resilience = dict(c.resilience)
    c.resilience.update(
        max_retries=2,
        backoff_base_seconds=0.001,     # keep the suite fast
        circuit_failure_threshold=3,
        circuit_window_seconds=60,
        circuit_cooldown_seconds=0.05,
    )
    return c


# ---------------------------------------------------------------------------
# Circuit breaker state machine -- every transition
# ---------------------------------------------------------------------------

def test_circuit_starts_closed():
    assert Circuit(name="p").state is CircuitState.CLOSED


def test_closed_to_open_after_threshold_failures():
    c = Circuit(name="p")
    for _ in range(2):
        c.record_failure("boom", threshold=3, window=60)
    assert c.state is CircuitState.CLOSED, "must not trip before the threshold"

    c.record_failure("boom", threshold=3, window=60)
    assert c.state is CircuitState.OPEN


def test_open_circuit_refuses_requests():
    c = Circuit(name="p")
    for _ in range(3):
        c.record_failure("boom", threshold=3, window=60)
    assert c.allows_request(cooldown_seconds=60) is False


def test_open_to_half_open_after_cooldown():
    c = Circuit(name="p")
    for _ in range(3):
        c.record_failure("boom", threshold=3, window=60)

    time.sleep(0.06)
    assert c.allows_request(cooldown_seconds=0.05) is True
    assert c.state is CircuitState.HALF_OPEN


def test_half_open_allows_exactly_one_probe():
    """Letting several through would hammer a provider that is still
    recovering -- the thundering herd the breaker exists to prevent."""
    c = Circuit(name="p")
    for _ in range(3):
        c.record_failure("boom", threshold=3, window=60)
    time.sleep(0.06)

    assert c.allows_request(0.05) is True      # the probe
    assert c.allows_request(0.05) is False     # everyone else waits


def test_half_open_to_closed_on_probe_success():
    c = Circuit(name="p")
    for _ in range(3):
        c.record_failure("boom", threshold=3, window=60)
    time.sleep(0.06)
    c.allows_request(0.05)

    c.record_success()
    assert c.state is CircuitState.CLOSED
    assert c.failures == []


def test_half_open_to_open_on_probe_failure():
    """The transition people forget. A failed probe must RESTART the cooldown,
    not grant another immediate attempt."""
    c = Circuit(name="p")
    for _ in range(3):
        c.record_failure("boom", threshold=3, window=60)
    time.sleep(0.06)
    c.allows_request(0.05)
    assert c.state is CircuitState.HALF_OPEN

    c.record_failure("still broken", threshold=3, window=60)
    assert c.state is CircuitState.OPEN
    assert c.allows_request(0.05) is False, "cooldown must restart"


def test_failures_outside_the_window_do_not_count():
    """The threshold is N failures WITHIN a window, not N failures ever.

    A provider that failed three times over a month is not unhealthy now.
    """
    c = Circuit(name="p")
    for _ in range(3):
        c.record_failure("old", threshold=3, window=0.01)
        time.sleep(0.02)      # each failure ages out before the next
    assert c.state is CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------

def test_backoff_grows_and_is_jittered():
    """Jitter is not decoration: without it every client that failed together
    retries together, re-triggering the overload they are backing off from."""
    base = 0.5
    samples = [resilience.backoff_seconds(2, base) for _ in range(40)]

    assert all(0 <= s <= base * 4 for s in samples)
    assert len(set(samples)) > 1, "identical delays means no jitter"

    # Upper bound doubles with each attempt.
    assert max(resilience.backoff_seconds(3, base) for _ in range(60)) > base * 4


# ---------------------------------------------------------------------------
# Retry policy -- which errors are retried, and which must never be
# ---------------------------------------------------------------------------

def _dispatch(cfg, providers, breakers=None, tier="small"):
    breakers = breakers or CircuitBreakers(cfg)
    return asyncio.run(resilience.dispatch(
        prompt="hello", tier=tier, max_tokens=100,
        config=cfg, providers=providers, breakers=breakers,
    )), breakers


def test_transient_failure_is_retried(cfg):
    """A provider that fails twice then succeeds should still answer."""
    class FlakyProvider(MockProvider):
        def __init__(self):
            super().__init__(text="recovered")
            self.attempts = 0

        async def complete(self, prompt, model, max_tokens):
            self.attempts += 1
            self.calls += 1
            if self.attempts <= 2:
                raise ProviderServerError("mock 500")
            return await MockProvider.complete(self, prompt, model, max_tokens)

    flaky = FlakyProvider()
    result, _ = _dispatch(cfg, {"groq": flaky})

    assert result.result.text == "recovered"
    assert flaky.attempts == 3
    assert result.attempts == 3


def test_bad_request_is_never_retried(cfg):
    """Retrying a dead API key produces identical 401s and multiplied latency.
    The taxonomy exists to prevent exactly this.

    Escalation is disabled so this measures RETRY alone. With it on, `groq`
    would legitimately be called a second time for the mid tier -- our config
    puts both tiers on Groq with different models, and an unknown model on one
    says nothing about the other. That is fallback, not a retry, and it is
    covered by test_tier_exhaustion_escalates_a_tier.
    """
    cfg.resilience["escalate_on_tier_exhausted"] = False
    provider = MockProvider(fail_with="bad_request")

    with pytest.raises(resilience.AllProvidersFailed) as exc:
        _dispatch(cfg, {"groq": provider})

    assert provider.calls == 1, "a deterministic failure must be tried once"
    assert exc.value.attempts == 1


def test_transient_failure_exhausts_retries_before_giving_up(cfg):
    """The contrast case: a retryable error IS retried, max_retries + 1 times."""
    cfg.resilience["escalate_on_tier_exhausted"] = False
    provider = MockProvider(fail_with="server_error")

    with pytest.raises(resilience.AllProvidersFailed):
        _dispatch(cfg, {"groq": provider})

    expected = cfg.resilience["max_retries"] + 1
    assert provider.calls == expected, (
        f"expected {expected} attempts, got {provider.calls}"
    )


def test_rate_limit_honours_retry_after(cfg, monkeypatch):
    """When a provider tells us how long to wait, believe it over our guess."""
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(resilience.asyncio, "sleep", fake_sleep)

    class Limited(MockProvider):
        async def complete(self, prompt, model, max_tokens):
            self.calls += 1
            raise ProviderRateLimited("slow down", retry_after_seconds=7.0)

    with pytest.raises(resilience.AllProvidersFailed):
        _dispatch(cfg, {"groq": Limited()})

    assert slept, "expected a backoff sleep"
    assert max(slept) >= 7.0, f"ignored Retry-After: slept {slept}"


# ---------------------------------------------------------------------------
# Fallback and escalation
# ---------------------------------------------------------------------------

def test_tier_exhaustion_escalates_a_tier(cfg):
    """SPEC 8.7: when a tier has no other provider, escalate rather than fail.

    small -> groq (broken), mid -> groq (same instance, also broken),
    large -> anthropic (healthy). So the answer comes from the large tier.
    """
    broken = MockProvider(fail_with="server_error")
    healthy = MockProvider(text="from the large tier")

    result, _ = _dispatch(cfg, {"groq": broken, "anthropic": healthy})

    assert result.result.text == "from the large tier"
    assert result.tier == "large"
    assert result.escalated is True
    assert result.fallback_fired is True


def test_escalation_can_be_switched_off(cfg):
    """DECISIONS.md D5: during an outage, escalation turns an availability
    incident into a cost spike. It must be possible to turn off."""
    cfg.resilience["escalate_on_tier_exhausted"] = False
    broken = MockProvider(fail_with="server_error")
    healthy = MockProvider(text="never reached")

    with pytest.raises(resilience.AllProvidersFailed):
        _dispatch(cfg, {"groq": broken, "anthropic": healthy})

    assert healthy.calls == 0, "escalation was disabled; large must not be used"


def test_open_circuit_is_skipped_and_the_next_tier_answers(cfg):
    """THE GATE (SPEC 11, Stage 5): the system keeps serving with a provider
    forced to fail."""
    broken = MockProvider(fail_with="server_error")
    healthy = MockProvider(text="still serving")
    breakers = CircuitBreakers(cfg)

    # Trip groq's breaker.
    for _ in range(cfg.resilience["circuit_failure_threshold"]):
        breakers.record_failure("groq", "forced down")
    assert breakers.for_provider("groq").state is CircuitState.OPEN

    result, _ = _dispatch(cfg, {"groq": broken, "anthropic": healthy}, breakers)

    assert result.result.text == "still serving"
    assert broken.calls == 0, "an open circuit must not be called at all"
    assert result.escalated is True


def test_all_circuits_open_is_flagged_for_503(cfg):
    """Nothing was tried, so "come back later" is honest -- distinct from
    "we tried and they broke"."""
    breakers = CircuitBreakers(cfg)
    for name in ("groq", "anthropic"):
        for _ in range(cfg.resilience["circuit_failure_threshold"]):
            breakers.record_failure(name, "down")

    with pytest.raises(resilience.AllProvidersFailed) as exc:
        _dispatch(cfg, {"groq": MockProvider(), "anthropic": MockProvider()},
                  breakers)

    assert exc.value.all_open is True


def test_success_closes_a_half_open_circuit_end_to_end(cfg):
    """The full recovery loop, through dispatch rather than the Circuit alone."""
    provider = MockProvider(text="back up")
    breakers = CircuitBreakers(cfg)

    for _ in range(cfg.resilience["circuit_failure_threshold"]):
        breakers.record_failure("groq", "was down")
    assert breakers.for_provider("groq").state is CircuitState.OPEN

    time.sleep(cfg.resilience["circuit_cooldown_seconds"] + 0.02)

    result, _ = _dispatch(cfg, {"groq": provider}, breakers)
    assert result.result.text == "back up"
    assert breakers.for_provider("groq").state is CircuitState.CLOSED


def test_trail_records_every_attempt(cfg):
    """Every provider tried is recorded, including ones skipped for an open
    circuit -- otherwise a 502 cannot be explained after the fact."""
    broken = MockProvider(fail_with="server_error")
    healthy = MockProvider(text="ok")

    result, _ = _dispatch(cfg, {"groq": broken, "anthropic": healthy})

    providers_tried = [a.provider for a in result.trail]
    assert "groq" in providers_tried and "anthropic" in providers_tried
    assert any(a.error for a in result.trail), "failures must be recorded"
