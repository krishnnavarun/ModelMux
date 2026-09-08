"""Router unit tests (SPEC section 9).

Two kinds of test here, and the distinction matters:

  - Tests of the MECHANISM (scoring, bucketing, force_tier). These should keep
    passing through Stage 3.
  - Tests that PIN THE KNOWN FAILURES of the naive rule. These are supposed to
    fail once the real classifier lands -- that is how we will know it fixed
    something. They are marked `stage2_failure`.
"""

import pytest

from app import config as config_module
from app import router, tokens


@pytest.fixture(scope="module", autouse=True)
def _tokenizer():
    tokens.init()


@pytest.fixture(scope="module")
def config():
    return config_module.load()


# --- scoring mechanism -----------------------------------------------------

def test_score_is_linear_below_saturation():
    assert router.score_from_tokens(300, 600) == pytest.approx(0.5)
    assert router.score_from_tokens(150, 600) == pytest.approx(0.25)


def test_score_saturates_at_one():
    """Past saturation, longer tells you nothing extra."""
    assert router.score_from_tokens(600, 600) == 1.0
    assert router.score_from_tokens(60_000, 600) == 1.0


def test_zero_saturation_rejected():
    with pytest.raises(ValueError):
        router.score_from_tokens(10, 0)


# --- bucketing -------------------------------------------------------------

@pytest.mark.parametrize("score,expected", [
    (0.00, "small"), (0.33, "small"),   # boundary is inclusive: cheaper on ties
    (0.34, "mid"), (0.66, "mid"),
    (0.67, "large"), (1.00, "large"),
])
def test_tier_boundaries(score, expected):
    thresholds = {"small_max": 0.33, "mid_max": 0.66}
    assert router.tier_for_score(score, thresholds) == expected


# --- select_tier -----------------------------------------------------------

def test_short_prompt_routes_small(config):
    assert router.select_tier("What is 2+2?", config).tier == "small"


def test_length_alone_no_longer_reaches_large(config):
    """UPDATED AT STAGE 3, and the change is the point.

    Under the naive rule this 2,000-token prompt routed to `large` purely for
    being long. Length is now only 20% of the score, so filler text with no
    reasoning cue escalates off `small` but cannot reach the most expensive
    tier on size alone.

    That is deliberate: D12 measured that length and difficulty diverge, so
    length by itself must not be able to spend the most money.
    """
    decision = router.select_tier("word " * 2000, config)
    assert decision.tier != "large"
    assert decision.signals["reasoning_depth"] == 0


def test_signals_are_all_real_now(config):
    """UPDATED AT STAGE 3. Previously asserted has_code and embedding_tier
    were None, because no classifier computed them. All are real now."""
    decision = router.select_tier("What is the capital of Japan?", config)
    s = decision.signals
    assert s["token_count"] > 0
    assert isinstance(s["has_code"], bool)
    assert isinstance(s["reasoning_markers"], int)
    assert isinstance(s["question_context_ratio"], float)
    assert s["is_lookup"] is True          # short, factual, no reasoning cue


def test_reason_names_the_signals_that_fired(config):
    """A score is not an explanation. The reason must name what drove it, so a
    logged row can be understood without re-running the classifier."""
    reason = router.select_tier("Prove that there are infinitely many primes.",
                                config).reason
    assert "->" in reason
    assert "reasoning marker" in reason


def test_force_tier_overrides_but_still_scores(config):
    """A forced request must still report what the router WOULD have chosen."""
    decision = router.select_tier("What is 2+2?", config, force_tier="large")
    assert decision.tier == "large"
    assert decision.forced is True
    assert decision.provider["name"] == "anthropic"
    assert decision.complexity_score < 0.1      # score still computed
    assert "would have chosen 'small'" in decision.reason


def test_decision_resolves_a_real_provider(config):
    decision = router.select_tier("hi", config)
    assert decision.provider["name"] == "groq"
    assert "cost_per_1k_input" in decision.provider


# --- pinned Stage 2 failures ----------------------------------------------
# These document what the naive rule gets WRONG. Stage 3 should break them.

def test_short_hard_prompt_now_routes_up(config):
    """RESOLVED BY STAGE 3. Was `assert tier == "small"` and marked
    stage2_failure -- the naive router sent this 9-token prompt to the weakest
    model because it scored on length alone.

    It now escalates on the HARD reasoning marker "prove". The test was
    inverted rather than deleted, so the history of the fix stays visible.
    """
    decision = router.select_tier("Prove Fermat's Last Theorem.", config)
    assert decision.tier != "small"
    assert decision.signals["reasoning_depth"] == 2


def test_long_easy_prompt_now_routes_small(config):
    """RESOLVED BY STAGE 3. Was `assert tier != "small"` and marked
    stage2_failure -- length alone escalated a trivial question wrapped in
    pasted context.

    `question_context_ratio` now scales the length term down, so the router
    scores the QUESTION rather than the wallpaper around it.
    """
    prompt = "Here is my long diary entry. " + ("I made coffee and walked. " * 60)
    prompt += " What day of the week was it?"
    decision = router.select_tier(prompt, config)
    assert decision.tier == "small"
    assert decision.signals["question_context_ratio"] < 0.2


@pytest.mark.stage2_failure
def test_large_tier_is_effectively_unreachable(config):
    """DISCOVERED, not designed. See DECISIONS.md D12.

    Reaching `large` needs score > 0.66, i.e. > 396 tokens at saturation 600.
    No prompt in the 50-prompt evaluation set is that long, so across the whole
    set the large tier was chosen ZERO times. The naive rule does not merely
    misroute -- it cannot use its own most capable tier for realistic prompts.
    """
    saturation = config.classifier["naive"]["saturation_tokens"]
    mid_max = config.classifier["thresholds"]["mid_max"]
    tokens_needed = saturation * mid_max
    assert tokens_needed > 390, (
        f"{tokens_needed:.0f} tokens needed before anything routes to 'large'"
    )
