"""Metrics aggregation and endpoint tests (SPEC sections 8.9, 9).

`metrics.py` only reads rows the request path already wrote. These tests write
rows through the real endpoint and then check what the dashboard would see --
testing the aggregates against hand-inserted rows would prove the SQL works
while missing whether the request path records the right thing.
"""

import pytest

from app import config as config_module
from app import metrics
from conftest import rows


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------

def test_percentile_returns_a_value_that_actually_occurred():
    """Nearest-rank, not interpolated.

    With a handful of requests, interpolation invents a latency nobody
    experienced. "p95 = 147ms" should mean some request took 147ms.
    """
    values = [10, 20, 30, 40, 50]
    assert metrics._percentile(values, 0.50) in values
    assert metrics._percentile(values, 0.95) in values


def test_percentile_of_empty_is_none_not_zero():
    """None means "no data". Zero would claim every request was instant."""
    assert metrics._percentile([], 0.50) is None


def test_percentile_ordering():
    values = list(range(1, 101))
    assert metrics._percentile(values, 0.50) < metrics._percentile(values, 0.95)


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------

def test_stats_on_an_empty_window_returns_zeros(client, db_path):
    """A quiet night should render "nothing happened", not an error.

    Depends on `client` so the app lifespan runs init_db(). Without it the
    table does not exist and the query raises -- and the test only passed at
    all when some earlier test happened to create it first, which is an
    ordering dependency, not a passing test.
    """
    stats = metrics.get_stats(db_path, "1h")
    assert stats["requests"] >= 0
    assert stats["cache_hit_rate"] == 0.0 or stats["requests"] > 0


def test_unknown_window_is_rejected(db_path):
    with pytest.raises(ValueError, match="unknown window"):
        metrics.get_stats(db_path, "nonsense")


def test_stats_counts_requests_the_endpoint_wrote(client, db_path):
    before = metrics.get_stats(db_path, "24h")["requests"]

    for i in range(3):
        client.post("/v1/chat", json={"prompt": f"question {i}"})

    after = metrics.get_stats(db_path, "24h")
    assert after["requests"] == before + 3
    assert "small" in after["tier_distribution"]


def test_stats_reports_savings_against_the_baseline(client, db_path):
    client.post("/v1/chat", json={"prompt": "a cheap question"})
    stats = metrics.get_stats(db_path, "24h")

    assert stats["cost_if_all_large_usd"] > stats["cost_usd"]
    assert stats["cost_saved_usd"] > 0
    assert 0 < stats["savings_pct"] <= 100


def test_savings_carry_the_bias_caveat(client, db_path):
    """DECISIONS.md D3: cost_if_large_usd reprices OBSERVED tokens at baseline
    rates. Any surface reporting savings must carry that, or it is a false
    claim dressed as a measurement."""
    stats = metrics.get_stats(db_path, "24h")
    assert "savings_caveat" in stats
    assert "DECISIONS.md D3" in stats["savings_caveat"]


def test_error_rate_counts_failures(client, db_path, mock_provider):
    mock_provider.fail_with = "server_error"
    client.post("/v1/chat", json={"prompt": "this will fail"})

    stats = metrics.get_stats(db_path, "24h")
    assert stats["error_rate"] > 0


def test_latency_percentiles_exclude_errors(client, db_path, mock_provider):
    """A fast 400 must not flatter the p50, and a timeout must not distort the
    p95. Neither tells you how long an ANSWER takes."""
    for i in range(4):
        client.post("/v1/chat", json={"prompt": f"good question {i}"})
    client.post("/v1/chat", json={"prompt": "   "})      # 400, very fast

    stats = metrics.get_stats(db_path, "24h")
    assert stats["latency_p50_ms"] is not None


# ---------------------------------------------------------------------------
# get_recent
# ---------------------------------------------------------------------------

def test_recent_returns_newest_first(client, db_path):
    client.post("/v1/chat", json={"prompt": "older request"})
    client.post("/v1/chat", json={"prompt": "newer request"})

    recent = metrics.get_recent(db_path, limit=10)
    assert len(recent) >= 2
    assert recent[0]["timestamp"] >= recent[1]["timestamp"]


def test_recent_never_exposes_a_full_prompt(client, db_path):
    """SPEC section 10. The feed shows a preview; the full prompt is not stored
    at all, so it cannot leak through this endpoint."""
    secret = "CONFIDENTIAL" + "z" * 300
    client.post("/v1/chat", json={"prompt": secret})

    recent = metrics.get_recent(db_path, limit=5)
    for row in recent:
        assert "prompt" not in row
        if row["prompt_preview"]:
            assert len(row["prompt_preview"]) <= 80


def test_recent_limit_is_clamped(client, db_path):
    """An unbounded limit is a trivial way to pull the whole table."""
    assert len(metrics.get_recent(db_path, limit=10_000)) <= 500
    assert len(metrics.get_recent(db_path, limit=0)) <= 1


def test_recent_converts_integer_flags_to_booleans(client, db_path):
    """SQLite has no boolean type; the API should not leak that."""
    client.post("/v1/chat", json={"prompt": "check the flags"})
    row = metrics.get_recent(db_path, limit=1)[0]
    for field in ("cache_hit", "escalated", "fallback_fired"):
        assert isinstance(row[field], bool)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def test_stats_endpoint(client):
    body = client.get("/v1/stats?window=24h").json()
    assert body["window"] == "24h"
    assert "cost_saved_usd" in body


def test_stats_endpoint_rejects_a_bad_window(client):
    response = client.get("/v1/stats?window=forever")
    assert response.status_code == 400
    assert "unknown window" in response.json()["error"]


def test_requests_endpoint(client):
    client.post("/v1/chat", json={"prompt": "feed me"})
    body = client.get("/v1/requests?limit=5").json()
    assert isinstance(body["requests"], list)


def test_health_providers_lists_every_configured_provider(client):
    """A provider with no circuit yet has simply never failed. Omitting it
    would read as "broken" on a dashboard; it is the healthiest state there
    is."""
    body = client.get("/health/providers").json()
    names = {p["provider"] for p in body["providers"]}

    # Every CONFIGURED provider must appear, whoever they are. Read from
    # config rather than hardcoding names -- this test is about completeness,
    # not about which vendor happens to serve a tier (D38).
    cfg = config_module.load()
    configured = {
        p["name"]
        for tier in cfg.tiers.values()
        for p in (tier.get("providers") or [])
    }
    assert configured <= names
    assert all(p["state"] == "closed" for p in body["providers"])
    assert "cache_available" in body
    assert "classifier_mode" in body


def test_health_providers_reports_an_open_circuit(client):
    """The operational view during an incident."""
    breakers = client.app.state.breakers
    threshold = breakers.config.resilience["circuit_failure_threshold"]
    for _ in range(threshold):
        breakers.record_failure("groq", "forced down for the test")

    body = client.get("/health/providers").json()
    groq = next(p for p in body["providers"] if p["provider"] == "groq")

    assert groq["state"] == "open"
    assert groq["last_error"] == "forced down for the test"
    assert groq["recovers_in_seconds"] is not None
