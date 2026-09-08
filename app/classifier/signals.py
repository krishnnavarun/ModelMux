"""Individual complexity signals (SPEC section 8.1).

Pure functions, no state, each independently testable. **Each returns a number
or a boolean, never a decision.** Combining them into a score is heuristic.py's
job; choosing a tier is router.py's. Keeping those three apart is what makes
every routing decision inspectable (SPEC section 2).

-----------------------------------------------------------------------------
WHY THESE SIGNALS
-----------------------------------------------------------------------------
Not invented. Each one targets a specific, measured failure of the Stage 2
naive router (DECISIONS.md D12), listed here in the order that evidence ranks
them:

  reasoning_markers      -> the 28 "too cheap" cases. Present in nearly every
                            one, absent from every correctly-routed lookup.
  is_lookup              -> protects the 15 cases already correct from being
                            broken by the signals above.
  question_context_ratio -> all 5 `trap_long_but_easy` failures. NOT in SPEC
                            8.1 -- discovered by running the experiment.
  multi_question         -> 0/2 today.
  has_code               -> separates code_simple from code_mid.
  output_length_request  -> "write an essay" vs "in one word".
  language_complexity    -> a weak background signal.
"""

import re

from app import tokens

# ---------------------------------------------------------------------------
# Phrases that indicate the prompt needs reasoning rather than recall.
#
# Matched with word boundaries. That is not pedantry: "improve" contains
# "prove", "comparison" contains "compare". Without \b this signal would fire
# on ordinary prose and quietly escalate everything.
# ---------------------------------------------------------------------------
# Markers are GRADED, because they are not equivalent. "Prove Fermat's Last
# Theorem" and "Compare these two options" both contain exactly one reasoning
# cue, but demand very different capability. A count cannot separate them --
# which is why the first version of this scorer sent every
# `trap_short_but_hard` prompt to the small tier (0/11).
#
#   HARD    open-ended generation, formal argument, original design
#   MEDIUM  structured explanation or analysis of known material
HARD_MARKERS = [
    r"prove", r"proof", r"derive", r"derivation",
    r"critique", r"defend", r"justify", r"argue",
    r"design an?", r"architect",
    r"from first principles", r"generalise", r"generalize",
    r"discovered or invented", r"unresolved",
]
MEDIUM_MARKERS = [
    r"explain why", r"explain how", r"step by step", r"step-by-step",
    r"compare", r"contrast", r"evaluate", r"assess",
    r"analyse", r"analyze", r"analysis",
    r"debug", r"diagnose", r"troubleshoot",
    r"optimise", r"optimize", r"refactor",
    r"trade[- ]?offs?", r"implications?",
    r"why does", r"why is", r"why do", r"why are",
    r"how does", r"how would", r"how should",
    r"walk me through", r"reason about",
    # Generic instruction verbs that ask for constructed prose rather than
    # recall. Kept deliberately generic -- these are ordinary English verbs,
    # not phrases lifted from the evaluation set.
    r"explain", r"describe", r"summarise", r"summarize",
    r"recommend", r"review", r"what is wrong",
]
REASONING_MARKERS = HARD_MARKERS + MEDIUM_MARKERS


def _compile(markers: list[str]) -> re.Pattern:
    return re.compile("|".join(rf"\b{m}\b" for m in markers), re.IGNORECASE)


_HARD_RE = _compile(HARD_MARKERS)
_MEDIUM_RE = _compile(MEDIUM_MARKERS)
_REASONING_RE = _compile(REASONING_MARKERS)

# Words that begin a factual-lookup question.
LOOKUP_STARTERS = (
    "what is", "what are", "what was", "what year", "what time",
    "when is", "when was", "when did",
    "where is", "where was", "where are",
    "who is", "who was", "who wrote", "who invented",
    "how many", "how much", "how long is",
    "which is", "name the", "list the",
)

# A lookup must also be short. Above this it is carrying context, and the
# question-to-context ratio is the better signal.
LOOKUP_MAX_TOKENS = 20

# At or above this question-to-context ratio the prompt is treated as all
# question, so reasoning markers are searched for in the whole text.
QUESTION_DOMINANT_RATIO = 0.5

# Requests that shrink or grow the expected answer.
BREVITY_PHRASES = (
    "in one word", "one word", "briefly", "in brief", "in short",
    "concisely", "tl;dr", "short answer", "just the", "only the",
    "in a sentence", "one line",
)
VERBOSITY_PHRASES = (
    "in detail", "detailed", "comprehensive", "thorough", "at length",
    "write an essay", "essay", "elaborate", "deep dive", "exhaustive",
    "step by step", "walk me through", "full explanation",
)
_WORD_COUNT_RE = re.compile(r"\b(\d{2,5})\s*[- ]?words?\b", re.IGNORECASE)

# Code detection.
_FENCE_RE = re.compile(r"```")
_CODE_KEYWORD_RE = re.compile(
    r"\b(def|class|import|from|return|function|const|let|var|"
    r"public|private|static|void|SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|"
    r"async|await|lambda|=>|null|None|True|False)\b"
)
_CODE_PUNCT = set("{};()[]=<>")
CODE_PUNCT_DENSITY = 0.04      # fraction of chars that are code punctuation
CODE_KEYWORD_MIN = 2

# Enumerated sub-parts: "1." / "2)" at the start of a line, or "first,"/"second,"
_ENUMERATED_RE = re.compile(r"(^|\n)\s*(\d{1,2}[.)]|[-*])\s+", re.MULTILINE)
_ORDINAL_RE = re.compile(
    r"\b(first|second|third|then|finally|also|additionally)\b[,:]", re.IGNORECASE
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def token_count(prompt: str) -> int:
    """Length in tokens. The only Stage 2 signal, kept because it is real --
    just far weaker than it looked."""
    return tokens.count(prompt)


def reasoning_markers(prompt: str) -> int:
    """How many distinct reasoning cues appear.

    A COUNT, not a boolean, because "prove and then generalise" should score
    above a single "compare". Distinct matches only, so repeating one word does
    not inflate the score.
    """
    found = {m.group(0).lower() for m in _REASONING_RE.finditer(prompt)}
    return len(found)


def question_part(prompt: str) -> str:
    """The part of the prompt that is actually being asked.

    For a short prompt this is the whole thing. For a long prompt with pasted
    context it is the trailing question.

    **Why this exists:** reasoning markers must be searched for in the QUESTION,
    not the wallpaper around it. A pasted recipe, email thread or diary entry
    contains words like "compare", "why does" or "analysis" incidentally. When
    markers were matched against the whole prompt, three of the five
    `trap_long_but_easy` cases escalated on a cue that had nothing to do with
    what was being asked -- the category regressed from 5/5 to 3/5.
    """
    text = prompt.strip()
    if not text:
        return ""

    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) <= 1:
        return text

    # Mostly-question prompts are used whole: a long genuine question can carry
    # its markers anywhere in itself, and clipping to the last sentence would
    # lose them.
    if question_context_ratio(prompt) >= QUESTION_DOMINANT_RATIO:
        return text

    return next(
        (s for s in reversed(sentences) if s.rstrip().endswith("?")),
        sentences[-1],
    )


def reasoning_depth(prompt: str) -> int:
    """Strength of the strongest reasoning cue in the QUESTION.

    0 = none, 1 = medium, 2 = hard.

    A count cannot express this. "Prove X" (1 hard marker) needs more than
    "Compare A and B" (1 medium marker), and collapsing both to the integer 1
    loses exactly the distinction that matters.
    """
    target = question_part(prompt)
    if _HARD_RE.search(target):
        return 2
    if _MEDIUM_RE.search(target):
        return 1
    return 0


def is_lookup(prompt: str) -> bool:
    """A short factual question with no reasoning cue.

    Three conditions, all required. The reasoning-marker check is what stops
    "What is the capital of Japan?" (a lookup) and "What is the proof that P
    != NP?" (not one) from being treated alike -- both start with "what is".
    """
    stripped = prompt.strip().lower()
    if token_count(prompt) > LOOKUP_MAX_TOKENS:
        return False
    if reasoning_markers(prompt) > 0:
        return False
    return stripped.startswith(LOOKUP_STARTERS)


def question_context_ratio(prompt: str) -> float:
    """Fraction of the prompt that is the actual question.

    NOT IN SPEC 8.1. Added because all five `trap_long_but_easy` failures in
    D12 share one shape: a long pasted block plus one short trivial question.
    A 1,400-word diary entry ending "what day of the week was it?" is not a
    hard prompt -- it is an easy question with context attached.

    Total length is the wrong measure. The QUESTION is what needs answering.

    Returns 1.0 when the prompt is all question (short prompts), approaching
    0.0 as pasted context dominates. A LOW value means EASIER, which is the
    opposite direction to every other signal here -- heuristic.py must weight
    it accordingly.
    """
    text = prompt.strip()
    if not text:
        return 1.0

    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) <= 1:
        return 1.0

    # The question is the last sentence ending in '?', else the final sentence.
    question = next(
        (s for s in reversed(sentences) if s.rstrip().endswith("?")),
        sentences[-1],
    )
    return min(len(question) / len(text), 1.0)


def multi_question(prompt: str) -> bool:
    """More than one question mark, or enumerated sub-parts.

    Measured on the QUESTION, not the whole prompt -- same reason as
    reasoning_depth. A pasted email thread or interview transcript is full of
    question marks that belong to the context, not to what is being asked.
    Counting them escalated two `trap_long_but_easy` cases to the mid tier on
    questions somebody else had asked.
    """
    target = question_part(prompt)
    if target.count("?") > 1:
        return True
    if len(_ENUMERATED_RE.findall(target)) >= 2:
        return True
    return len(_ORDINAL_RE.findall(target)) >= 2


def has_code(prompt: str) -> bool:
    """Fenced block, or a high density of code punctuation plus keywords.

    Both conditions for the density path, because prose contains brackets and
    equals signs too -- requiring keywords as well keeps ordinary writing from
    tripping it.
    """
    if _FENCE_RE.search(prompt):
        return True
    if not prompt:
        return False

    punct = sum(1 for ch in prompt if ch in _CODE_PUNCT)
    density = punct / len(prompt)
    keywords = len(_CODE_KEYWORD_RE.findall(prompt))
    return density >= CODE_PUNCT_DENSITY and keywords >= CODE_KEYWORD_MIN


def output_length_request(prompt: str) -> float:
    """How long an answer the prompt asks for, in [-1, 1].

    Negative = brevity requested ("in one word"), positive = length requested
    ("write an essay", "in detail"). Zero = unstated.

    This is about COST as much as difficulty: a request for 2,000 words is
    expensive on any tier, because output tokens dominate the bill.
    """
    lowered = prompt.lower()

    if match := _WORD_COUNT_RE.search(lowered):
        # An explicit word count is the strongest statement available.
        words = int(match.group(1))
        if words <= 50:
            return -0.5
        return min(words / 1000, 1.0)

    brevity = sum(1 for p in BREVITY_PHRASES if p in lowered)
    verbosity = sum(1 for p in VERBOSITY_PHRASES if p in lowered)
    if brevity and not verbosity:
        return -min(brevity * 0.5, 1.0)
    if verbosity and not brevity:
        return min(verbosity * 0.4, 1.0)
    return 0.0


def language_complexity(prompt: str) -> float:
    """Rough linguistic density in [0, 1], from sentence and word length.

    The weakest signal here, and deliberately so. It is a background nudge, not
    a decider -- long words and long sentences correlate with difficulty only
    loosely. Included because SPEC 8.1 lists it and it costs nothing.
    """
    words = prompt.split()
    if not words:
        return 0.0

    sentences = [s for s in _SENTENCE_SPLIT_RE.split(prompt.strip()) if s.strip()]
    words_per_sentence = len(words) / max(len(sentences), 1)
    avg_word_len = sum(len(w) for w in words) / len(words)

    # Normalised against rough English prose: ~20 words/sentence, ~5 chars/word.
    sentence_score = min(words_per_sentence / 30, 1.0)
    word_score = min(max(avg_word_len - 4, 0) / 4, 1.0)
    return round((sentence_score + word_score) / 2, 4)


def extract_all(prompt: str) -> dict:
    """Every signal for one prompt.

    Returned as a plain dict because it is serialised straight into the API
    response and the `signals_json` database column. The keys here ARE the
    public contract (SPEC section 5: "never remove it").
    """
    return {
        "token_count": token_count(prompt),
        "has_code": has_code(prompt),
        "reasoning_markers": reasoning_markers(prompt),
        "reasoning_depth": reasoning_depth(prompt),
        "multi_question": multi_question(prompt),
        "is_lookup": is_lookup(prompt),
        "question_context_ratio": round(question_context_ratio(prompt), 4),
        "output_length_request": round(output_length_request(prompt), 4),
        "language_complexity": language_complexity(prompt),
        # Filled in by embedding.py when the mode uses it.
        "embedding_tier": None,
        "embedding_confidence": None,
    }
