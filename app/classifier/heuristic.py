"""Rule-based complexity scoring (SPEC section 8.2).

Combines the signals in signals.py into a single score in [0, 1] using explicit
weights. Returns `(score, signals)` -- always both, so the API can expose every
component of the decision (SPEC section 2: "every component of a complexity
score must be individually inspectable").

-----------------------------------------------------------------------------
HOW THE SCORE IS BUILT
-----------------------------------------------------------------------------
A weighted sum of six normalised components, then two adjustments:

    score = Σ (weight_i × normalised_signal_i)
    if is_lookup and not multi_question:  score ×= LOOKUP_DISCOUNT

Every weight is in WEIGHTS below and nowhere else. Changing routing behaviour
should never require reading this function.

-----------------------------------------------------------------------------
WHY THE WEIGHTS ARE WHAT THEY ARE
-----------------------------------------------------------------------------
They are not guesses. They come from the measured failures in DECISIONS.md D12:

  reasoning (0.34)  Highest, because reasoning markers appear in nearly every
                    one of the 28 "too cheap" misroutes and in none of the 15
                    correctly-routed lookups. It is the single signal that
                    separates "Prove Fermat's Last Theorem" (9 tokens, needs
                    the large tier) from "What is the capital of Japan?"
                    (7 tokens, small tier is fine).

  length (0.20)     Demoted from being the ONLY signal in Stage 2 to a fifth of
                    the score. It is real but weak -- it scored 15/15 where
                    length and difficulty agreed and 0/32 where they diverged.

  multi_question    Two questions genuinely need more capable handling than
       (0.14)       one, and Stage 2 scored 0/2 here.

  code (0.12)       Separates code_simple from code_mid.

  output_length     A request for 2,000 words is expensive on any tier, because
       (0.10)       output tokens dominate the bill. This is as much a cost
                    signal as a difficulty one.

  language (0.10)   Deliberately the weakest. Long words and long sentences
                    correlate with difficulty only loosely. A background nudge.
"""

from app.classifier import signals

# Weights sum to 1.0, so an all-maximum prompt scores exactly 1.0 before
# adjustments. Keeping that invariant makes the numbers interpretable: a score
# of 0.5 means "half of the maximum evidence for difficulty".
WEIGHTS = {
    "reasoning": 0.34,
    "length": 0.20,
    "multi_question": 0.14,
    "code": 0.12,
    "output_length": 0.10,
    "language": 0.10,
}

# Reasoning markers saturate here. Three distinct cues is already emphatic;
# a fourth tells us nothing new.
REASONING_SATURATION = 3

# A confirmed short factual lookup is multiplied down. It is a MULTIPLIER, not
# a subtraction, so it cannot drag a genuinely complex prompt below zero -- and
# it is skipped entirely for multi-part questions (see the note below).
LOOKUP_DISCOUNT = 0.45

# Floors set by the strongest single indicator, expressed in tier terms against
# thresholds small_max=0.33 / mid_max=0.66:
#   0.75 clears mid_max  -> large
#   0.45 clears small_max -> mid
FLOOR_HARD_REASONING = 0.75     # prove, derive, critique, defend, design a
FLOOR_MEDIUM_REASONING = 0.45   # explain why, compare, debug, walk me through
FLOOR_MULTI_QUESTION = 0.42     # several questions in one prompt


def score(prompt: str, saturation_tokens: int) -> tuple[float, dict]:
    """Return (complexity score in [0, 1], the signals it was built from)."""
    s = signals.extract_all(prompt)

    # --- length, corrected for pasted context ------------------------------
    # This is the fix for the `trap_long_but_easy` failures. Rather than adding
    # question_context_ratio as its own weighted term, it SCALES the length
    # term -- because the problem it solves is specifically that raw token
    # count over-counts when most of the prompt is pasted context.
    #
    #   "1,400-word diary entry ... what day was it?"
    #        340 tokens x 0.02 ratio =  ~7 effective tokens  -> near zero
    #   "Prove Fermat's Last Theorem."
    #          9 tokens x 1.00 ratio =   9 effective tokens  -> near zero too,
    #                                    and correctly so: its difficulty is
    #                                    carried by `reasoning`, not length.
    effective_tokens = s["token_count"] * s["question_context_ratio"]
    length_component = min(effective_tokens / saturation_tokens, 1.0)

    # --- the rest ----------------------------------------------------------
    components = {
        "reasoning": min(s["reasoning_markers"] / REASONING_SATURATION, 1.0),
        "length": length_component,
        "multi_question": 1.0 if s["multi_question"] else 0.0,
        "code": 1.0 if s["has_code"] else 0.0,
        # output_length_request is [-1, 1]; map to [0, 1] with 0.5 as neutral,
        # so an explicit request for brevity actively lowers the score.
        "output_length": (s["output_length_request"] + 1) / 2,
        "language": s["language_complexity"],
    }

    weighted = sum(WEIGHTS[name] * value for name, value in components.items())

    # --- floors: difficulty is not an average ------------------------------
    # A pure weighted sum measures how MANY things about a prompt look hard.
    # But a prompt is hard if ANY ONE strong indicator fires, and the others
    # then drag it back down.
    #
    # "Prove Fermat's Last Theorem." is 9 tokens, has no code, is not
    # multi-part, and requests no particular length. Four near-zero components
    # diluted the one that mattered, and the weighted sum landed around 0.18 --
    # the small tier. That was 0/11 on `trap_short_but_hard`.
    #
    # So the strongest single indicator sets a FLOOR the average cannot pull
    # below. `max`, not `+`: floors do not stack, because two independent
    # reasons to escalate are not twice as hard as one.
    floor = 0.0
    if s["reasoning_depth"] == 2:      # prove / derive / critique / design
        floor = max(floor, FLOOR_HARD_REASONING)
    elif s["reasoning_depth"] == 1:    # explain why / compare / debug
        floor = max(floor, FLOOR_MEDIUM_REASONING)
    if s["multi_question"]:
        floor = max(floor, FLOOR_MULTI_QUESTION)

    # Floor sets a BASE, and the weighted evidence then pushes above it into
    # the remaining headroom.
    #
    # `max(weighted, floor)` was wrong in a subtle way: once a floor fired, it
    # discarded every other signal. A 194-token multi-part architecture
    # question and a 10-token "compare A and B" both contain one medium marker,
    # so both scored exactly 0.45 -- identical, despite obviously differing in
    # difficulty. Blending keeps the floor's guarantee while letting length,
    # code and output-length still count for something.
    raw = floor + (1.0 - floor) * weighted

    # --- lookup discount ---------------------------------------------------
    # `and not multi_question` is load-bearing. Prompt #46 in the evaluation
    # set -- "What is the population of Brazil? What is its capital? What
    # language...?" -- satisfies is_lookup (short, starts with "what is", no
    # reasoning markers) but needs the mid tier. Without this guard the
    # discount would drag a genuine multi-part question back down to small.
    discounted = s["is_lookup"] and not s["multi_question"]
    final = raw * LOOKUP_DISCOUNT if discounted else raw

    # Expose the working, not just the answer. These end up in the API
    # response and the signals_json column, so a routing decision can be
    # reconstructed from a logged row alone.
    s["components"] = {k: round(v, 4) for k, v in components.items()}
    s["weighted_score"] = round(weighted, 4)
    s["floor_applied"] = round(floor, 4) if floor > weighted else 0.0
    s["raw_score"] = round(raw, 4)
    s["lookup_discount_applied"] = discounted

    return round(min(max(final, 0.0), 1.0), 4), s
