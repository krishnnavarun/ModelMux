"""Integration tests for the request path, against the mock provider.

SPEC section 9. These run with no network access and no API key, which is the
point: a suite whose results depend on a third party's uptime is not a suite.
"""

from conftest import rows


# --- happy path ------------------------------------------------------------

def test_health_never_calls_a_provider(client, mock_provider):
    assert client.get("/health").json() == {"status": "ok"}
    assert mock_provider.calls == 0


def test_successful_request_returns_full_contract(client):
    response = client.post("/v1/chat", json={"prompt": "hello"})
    assert response.status_code == 200
    body = response.json()

    assert body["response"] == "mocked answer"

    routing = body["routing"]
    # Every field SPEC section 5 promises must be present, even where the
    # value is None because that stage has not been built.
    for field in (
        "request_id", "tier", "provider", "model", "cache_hit",
        "complexity_score", "signals", "escalated", "fallback_fired",
        "attempts", "tokens_in", "tokens_out", "cost_usd",
        "cost_if_large_usd", "latency_ms", "classify_ms", "cache_lookup_ms",
    ):
        assert field in routing, f"missing routing field: {field}"

    # "Never remove it" -- SPEC section 5.
    assert "token_count" in routing["signals"]


def test_unbuilt_stages_report_none_not_zero(client):
    """None means "not computed"; 0.0 would claim we measured something.

    Updated at Stage 2: complexity_score and classify_ms are now real, because
    the naive router computes them. Only the cache fields remain unbuilt.
    """
    routing = client.post("/v1/chat", json={"prompt": "hi"}).json()["routing"]

    # Now computed (Stage 2)
    assert isinstance(routing["complexity_score"], float)
    assert isinstance(routing["classify_ms"], int)
    assert routing["signals"]["token_count"] > 0

    # Still genuinely unbuilt (Stage 4)
    assert routing["cache_lookup_ms"] is None
    assert routing["cache_hit"] is False

    # Real as of Stage 3 -- the classifier computes these now.
    assert isinstance(routing["signals"]["has_code"], bool)
    assert isinstance(routing["signals"]["reasoning_markers"], int)


def test_classification_is_within_budget(client):
    """SPEC section 2: classification must stay under 20ms.

    Measured on a REALISTIC prompt -- one with sentence structure, which is
    what `question_part()` needs in order to strip pasted context before
    embedding.

    Warm-up matters and is not one call: measured inside TestClient, the same
    prompt took 24, 20, 16, 17, 19ms across five consecutive calls. Timing the
    first one measures lazy imports and the model's first forward pass, not
    the cost of serving traffic.

    The pathological case is covered separately below.
    """
    prompt = ("Here is some context for you. " * 8) + " What day of the week was it?"
    for _ in range(3):
        client.post("/v1/chat", json={"prompt": prompt})

    routing = client.post("/v1/chat", json={"prompt": prompt}).json()["routing"]
    assert routing["classify_ms"] < 20, (
        f"classification took {routing['classify_ms']}ms, budget is 20ms"
    )


def test_classification_worst_case_is_bounded(client):
    """The pathological input, documented rather than hidden.

    1200 characters with NO sentence boundaries. `question_part()` cannot clip
    it -- there is nothing to clip to -- so the full EMBED_MAX_CHARS window is
    embedded every time. This is the slowest classification the system can be
    asked to do.

    It sits right at the 20ms budget rather than comfortably inside it. That is
    a real limitation, recorded in DECISIONS.md D16, not a test to be tuned
    until it goes green. The bound here is the worst case we accept; if it
    regresses past 30ms something has genuinely got slower.
    """
    prompt = "hello " * 200
    for _ in range(3):
        client.post("/v1/chat", json={"prompt": prompt})

    routing = client.post("/v1/chat", json={"prompt": prompt}).json()["routing"]
    assert routing["classify_ms"] < 30, (
        f"worst-case classification took {routing['classify_ms']}ms"
    )


def test_routing_reason_is_reported(client):
    """Every routing decision must be explainable (SPEC section 2)."""
    routing = client.post("/v1/chat", json={"prompt": "hi"}).json()["routing"]
    assert "->" in routing["routing_reason"]


def test_cost_uses_both_token_directions(client):
    """Mock returns 10 tokens in / 5 out.

    Rates are read from config rather than hardcoded. An earlier version of
    this test pinned the literal prices and broke the moment they were
    corrected against Groq's published figures -- which tested the config file,
    not the arithmetic. What actually needs asserting is that BOTH directions
    are counted.
    """
    from app import config as config_module

    provider = config_module.load().provider_for("small")
    routing = client.post("/v1/chat", json={"prompt": "hi"}).json()["routing"]

    expected = (
        10 / 1000 * provider["cost_per_1k_input"]
        + 5 / 1000 * provider["cost_per_1k_output"]
    )
    assert routing["cost_usd"] == round(expected, 8)

    # The regression this guards: pricing input only. It must be strictly
    # cheaper than the real figure, or output tokens are being ignored.
    input_only = 10 / 1000 * provider["cost_per_1k_input"]
    assert routing["cost_usd"] > input_only


def test_counterfactual_is_more_expensive_than_actual(client):
    """The savings claim is the gap between these two numbers."""
    routing = client.post("/v1/chat", json={"prompt": "hi"}).json()["routing"]
    assert routing["cost_if_large_usd"] > routing["cost_usd"]


# --- safety (SPEC section 9) ----------------------------------------------

def test_empty_prompt_returns_400_not_a_crash(client, mock_provider):
    response = client.post("/v1/chat", json={"prompt": "   "})
    assert response.status_code == 400
    assert "request_id" in response.json()
    # Must be rejected before spending money.
    assert mock_provider.calls == 0


def test_oversized_prompt_returns_400_not_a_crash(client, mock_provider):
    """An unbounded prompt is a cost attack (SPEC section 10)."""
    response = client.post("/v1/chat", json={"prompt": "x" * 100_001})
    assert response.status_code == 400
    assert mock_provider.calls == 0


def test_missing_prompt_field_is_422(client):
    """FastAPI's own validation, before our handler. Documented in DECISIONS D1."""
    assert client.post("/v1/chat", json={}).status_code == 422


# --- provider failures -----------------------------------------------------

def test_provider_failure_returns_502(client, mock_provider):
    mock_provider.fail_with = "server_error"
    response = client.post("/v1/chat", json={"prompt": "hi"})
    assert response.status_code == 502
    assert "request_id" in response.json()


def test_bad_request_from_provider_is_502_not_400(client, mock_provider):
    """DECISIONS.md D2 -- the mistake this guards against.

    ProviderBadRequest means OUR key or model name is wrong. Returning 400
    would blame the caller for our misconfiguration.
    """
    mock_provider.fail_with = "bad_request"
    assert client.post("/v1/chat", json={"prompt": "hi"}).status_code == 502


def test_timeout_returns_502(client, mock_provider):
    mock_provider.fail_with = "timeout"
    assert client.post("/v1/chat", json={"prompt": "hi"}).status_code == 502


# --- logging: "nothing is silently dropped" (SPEC section 2) ---------------

def test_success_writes_a_row(client, db_path):
    before = len(rows(db_path))
    client.post("/v1/chat", json={"prompt": "log me"})
    after = rows(db_path)
    assert len(after) == before + 1
    assert after[-1]["status"] == "success"
    assert after[-1]["tokens_out"] == 5


def test_provider_failure_still_writes_a_row(client, db_path, mock_provider):
    mock_provider.fail_with = "server_error"
    before = len(rows(db_path))
    client.post("/v1/chat", json={"prompt": "this fails"})
    after = rows(db_path)
    assert len(after) == before + 1
    assert after[-1]["status"] == "error"
    assert "mock 500" in after[-1]["error_message"]


def test_validation_failure_still_writes_a_row(client, db_path):
    before = len(rows(db_path))
    client.post("/v1/chat", json={"prompt": "  "})
    after = rows(db_path)
    assert len(after) == before + 1
    assert after[-1]["status"] == "error"


def test_full_prompt_is_never_stored(client, db_path):
    """SPEC section 10 -- privacy. Preview is capped at 80 chars, rest hashed."""
    secret = "SENSITIVE" + "y" * 200
    client.post("/v1/chat", json={"prompt": secret})
    row = rows(db_path)[-1]

    assert len(row["prompt_preview"]) == 80
    assert row["prompt_preview"] == secret[:80]
    assert secret not in row["prompt_preview"]
    assert len(row["prompt_hash"]) == 64  # sha256 hex


def test_request_id_in_response_matches_the_row(client, db_path):
    """A caller complaint must be traceable to its row."""
    routing = client.post("/v1/chat", json={"prompt": "trace me"}).json()["routing"]
    assert rows(db_path)[-1]["id"] == routing["request_id"]
