"""Tier selection (SPEC section 8.4).

THIS IS THE FILE I HAVE TO DEFEND. Every decision in it should be readable
without needing to hold anything else in your head.

-----------------------------------------------------------------------------
STAGE 2: THE NAIVE RULE
-----------------------------------------------------------------------------
Right now this routes on **token count alone**. That is deliberate, and it is
deliberately bad.

SPEC section 11 defines Stage 2's completion criterion as "a written list of
misroutes." The naive rule exists to be *measured failing*, so that Stage 3's
classifier is built against real observed failures rather than guesses about
what might go wrong.

The rule in one sentence:

    longer prompt  ->  higher score  ->  more expensive tier

The obvious flaw, stated up front: **prompt length is not prompt difficulty.**

    "Prove Fermat's Last Theorem."        6 tokens  -> small tier  (very wrong)
    "Here is my 800-word diary entry.
     What day of the week was Tuesday?"  ~1000 tk  -> large tier  (very wrong)

Both are misroutes, in opposite directions. Cataloguing them is the point of
this stage. See DECISIONS.md D12 for the measured list.

-----------------------------------------------------------------------------
WHY A SCORE AND NOT A DIRECT TOKEN->TIER MAPPING
-----------------------------------------------------------------------------
Token count is normalised into a 0-1 complexity score, and the score is then
compared against the thresholds already in config.yaml
(`classifier.thresholds.small_max` / `mid_max`).

The alternative -- putting token thresholds directly in config -- would work
today and would have to be torn out at Stage 3, when the heuristic scorer
starts producing a real 0-1 score. Keeping the score abstraction now means
Stage 3 replaces *how the score is computed* and touches nothing else: not the
thresholds, not the config shape, not this function's signature.
"""

from dataclasses import dataclass, field

from app import tokens
from app.classifier import embedding, heuristic

TIER_ORDER = {"small": 0, "mid": 1, "large": 2}
_BY_ORDER = ["small", "mid", "large"]


@dataclass
class RoutingDecision:
    """Everything about how a request was routed.

    Every intermediate value is kept, not just the final tier. SPEC section 2:
    "Every component of a complexity score must be individually inspectable."
    A decision you cannot take apart is one you cannot defend.
    """

    tier: str
    provider: dict                      # the resolved config entry
    complexity_score: float
    signals: dict = field(default_factory=dict)
    escalated: bool = False
    forced: bool = False
    reason: str = ""


def score_from_tokens(token_count: int, saturation_tokens: int) -> float:
    """Map a token count onto a 0-1 complexity score.

    Linear up to `saturation_tokens`, then flat at 1.0. Saturation exists
    because past a certain length, longer tells you nothing extra -- a
    2,000-token prompt and a 20,000-token one are both simply "long", and
    without a ceiling the latter would dominate any future weighted blend of
    signals.
    """
    if saturation_tokens <= 0:
        raise ValueError("saturation_tokens must be positive")
    return min(token_count / saturation_tokens, 1.0)


def _one_tier_up(tier: str) -> str:
    """Next tier up, or the same tier if already at the top."""
    return _BY_ORDER[min(TIER_ORDER[tier] + 1, len(_BY_ORDER) - 1)]


def _score_for_tier(tier: str, thresholds: dict, current: float) -> float:
    """Nudge the reported score into the band of the tier actually chosen.

    Without this, a hybrid escalation produces a row saying `score 0.12` next
    to `tier: large`, which reads as a bug to anyone auditing the log. The
    original heuristic value is preserved in signals["raw_score"].
    """
    if tier_for_score(current, thresholds) == tier:
        return current
    if tier == "small":
        return min(current, thresholds["small_max"])
    if tier == "mid":
        return max(min(current, thresholds["mid_max"]), thresholds["small_max"] + 0.01)
    return max(current, thresholds["mid_max"] + 0.01)


def tier_for_score(score: float, thresholds: dict) -> str:
    """Bucket a score into a tier.

    Boundaries are inclusive at the top of each band: a score exactly equal to
    `small_max` routes to small. Cheaper on ties -- the whole point of the
    project -- and it means the thresholds read the way they are named.
    """
    if score <= thresholds["small_max"]:
        return "small"
    if score <= thresholds["mid_max"]:
        return "mid"
    return "large"


def select_tier(prompt: str, config, force_tier: str | None = None) -> RoutingDecision:
    """Choose a tier for `prompt`.

    `force_tier` bypasses routing entirely. It exists for testing and for the
    dashboard's comparison view (SPEC section 5) and is not part of normal
    operation -- but it is honoured here, in one place, so no caller has to
    special-case it.
    """
    saturation = config.classifier["naive"]["saturation_tokens"]
    mode = config.classifier.get("mode", "heuristic")

    if mode == "naive_tokens":
        # Stage 2's rule, kept so the baseline stays runnable for comparison.
        token_count = tokens.count(prompt)
        score = score_from_tokens(token_count, saturation)
        signals = {
            "token_count": token_count, "has_code": None,
            "reasoning_markers": None, "multi_question": None,
            "embedding_tier": None, "embedding_confidence": None,
        }
    else:
        # Stage 3: the full signal set, combined by explicit weights.
        score, signals = heuristic.score(prompt, saturation)

    escalated = False

    if mode in ("embedding", "hybrid") and embedding.available():
        emb_tier, confidence = embedding.classify(prompt)
        signals["embedding_tier"] = emb_tier
        signals["embedding_confidence"] = round(confidence, 4)

        heuristic_tier = tier_for_score(score, config.classifier["thresholds"])

        if mode == "embedding":
            resolved = emb_tier
        else:
            # HYBRID RECONCILIATION -- SPEC 8.4 rule 3.
            #
            # When the two classifiers disagree, take the HIGHER tier. Not a
            # tie-break for convenience: it is the project's central value
            # judgement, that a wrong cheap answer costs more in trust than a
            # wrong expensive one costs in money.
            #
            # It is also why accuracy alone is the wrong scoreboard. This rule
            # deliberately trades some "too expensive" errors to avoid "too
            # cheap" ones -- the misroute report counts them separately for
            # exactly this reason.
            resolved = max(
                (heuristic_tier, emb_tier), key=lambda t: TIER_ORDER[t]
            )
            if resolved != heuristic_tier:
                escalated = True

        # Low agreement among the k neighbours means the prompt sits in no
        # clear cluster. Escalating on that is the same fail-towards-quality
        # instinct applied to uncertainty rather than disagreement.
        if (config.classifier.get("escalate_on_low_confidence")
                and confidence < config.classifier.get("confidence_threshold", 0.6)):
            bumped = _one_tier_up(resolved)
            if bumped != resolved:
                resolved, escalated = bumped, True

        # Report the score the resolved tier implies, so complexity_score and
        # tier never contradict each other in a logged row.
        score = _score_for_tier(resolved, config.classifier["thresholds"], score)
        signals["heuristic_tier"] = heuristic_tier
        signals["resolved_tier"] = resolved

    if force_tier:
        # The score is still computed and reported. A forced request should
        # show what the router *would* have chosen -- that comparison is
        # exactly what the dashboard's compare view is for.
        return RoutingDecision(
            tier=force_tier,
            provider=config.provider_for(force_tier),
            complexity_score=score,
            signals=signals,
            forced=True,
            reason=(
                f"forced to '{force_tier}'; router would have chosen "
                f"'{tier_for_score(score, config.classifier['thresholds'])}'"
            ),
        )

    tier = signals.get("resolved_tier") or tier_for_score(
        score, config.classifier["thresholds"]
    )

    return RoutingDecision(
        tier=tier,
        escalated=escalated,
        provider=config.provider_for(tier),
        complexity_score=score,
        signals=signals,
        reason=_explain(mode, score, tier, signals, saturation),
    )


def _explain(mode: str, score: float, tier: str, signals: dict, saturation: int) -> str:
    """One human-readable line saying why this tier was chosen.

    SPEC section 2 requires every routing decision to be explainable. A score
    on its own is not an explanation -- this names the components that produced
    it, so a logged row can be understood without re-running the classifier.
    """
    if mode == "naive_tokens":
        return (f"{signals['token_count']} tokens / {saturation} saturation "
                f"= {score:.3f} -> {tier}")

    parts = [f"{signals['token_count']} tk"]
    if signals.get("reasoning_markers"):
        parts.append(f"{signals['reasoning_markers']} reasoning marker(s)")
    if signals.get("multi_question"):
        parts.append("multi-part")
    if signals.get("has_code"):
        parts.append("code")
    if signals.get("lookup_discount_applied"):
        parts.append("lookup discount")
    ratio = signals.get("question_context_ratio")
    if ratio is not None and ratio < 0.5:
        parts.append(f"question is {ratio:.0%} of prompt")

    return f"{', '.join(parts)} = {score:.3f} -> {tier}"
