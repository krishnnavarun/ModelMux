"""Embedding-based classification (SPEC section 8.3).

Embeds the prompt, compares it against a set of hand-labelled examples, and
lets the k nearest neighbours vote on a tier.

-----------------------------------------------------------------------------
WHY THIS EXISTS ALONGSIDE THE HEURISTIC
-----------------------------------------------------------------------------
heuristic.py matches keywords. That works, and it took accuracy from 34% to
78% -- but it can only ever catch phrasings somebody thought to list.

    "Is mathematics discovered or invented? Defend your position."

`defend` is in the marker list, so the heuristic catches it. Change one word:

    "Do numbers exist independently of human minds, or do we make them up?"

No marker. The heuristic sees an ordinary question. But its EMBEDDING lands
near the other philosophy-of-mathematics prompts, sharing no vocabulary with
them at all. That generalisation is the whole point.

-----------------------------------------------------------------------------
WHY THIS IS NOT AN LLM CALL
-----------------------------------------------------------------------------
SPEC section 2 forbids an LLM call in the classification path -- the routing
decision must be cheaper than the request it routes. `all-MiniLM-L6-v2` is a
22M-parameter encoder running locally on CPU: no network, no per-token cost,
~10ms. It is a matrix multiply, not a generation.
"""

import json
import sys
import time
from pathlib import Path

from app.classifier import signals

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LABELLED_PATH = PROJECT_ROOT / "eval" / "labelled.json"

MODEL_NAME = "all-MiniLM-L6-v2"
DEFAULT_K = 5

_model = None
_vectors = None          # numpy array, one row per labelled example
_tiers: list[str] = []   # tier for each row, same order
_available = False
_load_error: str | None = None


def init() -> None:
    """Load the model and embed the labelled set. Call once, at startup.

    Both halves are expensive and neither changes per request:
      - the model is ~80MB and downloads on first use
      - embedding ~60 examples takes seconds

    Doing either per request would blow the 20ms classification budget by
    orders of magnitude.

    A failure here is NOT fatal. The model download needs network, and a
    machine that is offline the first time should still be able to start the
    service -- routing falls back to the heuristic and says so.
    """
    global _model, _vectors, _tiers, _available, _load_error

    try:
        from sentence_transformers import SentenceTransformer

        started = time.perf_counter()
        _model = SentenceTransformer(MODEL_NAME)

        data = json.loads(LABELLED_PATH.read_text(encoding="utf-8"))
        examples = data["examples"]
        prompts = [e["prompt"] for e in examples]
        _tiers = [e["tier"] for e in examples]

        # normalize_embeddings=True makes every vector unit length, so cosine
        # similarity becomes a plain dot product -- one matrix multiply per
        # request instead of computing norms every time.
        _vectors = _model.encode(
            prompts, normalize_embeddings=True, show_progress_bar=False
        )
        _available = True
        _load_error = None

        elapsed = int((time.perf_counter() - started) * 1000)
        print(
            f"[embedding] loaded {MODEL_NAME}, embedded {len(prompts)} "
            f"labelled examples in {elapsed}ms",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001 -- must not prevent startup
        _model = None
        _vectors = None
        _available = False
        _load_error = str(exc)
        print(
            f"[embedding] unavailable ({exc}); routing will use the heuristic "
            f"only",
            file=sys.stderr,
        )


def available() -> bool:
    """Whether embedding classification can actually run.

    Exposed so the router can degrade honestly rather than silently, and so
    the eval harness can say which mode really produced a number.
    """
    return _available


def classify(prompt: str, k: int = DEFAULT_K) -> tuple[str | None, float]:
    """Return (tier, confidence) from the k nearest labelled examples.

    Confidence is the fraction of the k neighbours that agreed -- 5/5 is 1.0,
    3/5 is 0.6. That is a measure of how *clustered* the neighbourhood is, not
    a probability, and it is what `escalate_on_low_confidence` acts on.

    Returns (None, 0.0) when the model is unavailable.
    """
    if not _available:
        return None, 0.0

    # Embed the QUESTION, not the whole prompt. Two reasons, and both matter:
    #
    # 1. CORRECTNESS. The labelled examples are questions. Comparing them
    #    against a prompt that is 95% pasted diary entry measures similarity
    #    to the diary, not to the request.
    # 2. LATENCY. Transformer cost grows with sequence length. Embedding a
    #    340-token prompt took ~55ms against a 20ms budget; the trailing
    #    question is usually a dozen tokens.
    #
    # all-MiniLM-L6-v2 truncates at 256 tokens anyway, so a long prompt was
    # being silently clipped -- at an arbitrary point, keeping the context and
    # discarding the question at the end. This makes the choice deliberate.
    target = signals.question_part(prompt)

    query = _model.encode([target], normalize_embeddings=True,
                          show_progress_bar=False)[0]

    # Both sides are unit vectors, so the dot product IS cosine similarity.
    similarities = _vectors @ query

    # argsort ascending, so the last k are the most similar.
    top_k = similarities.argsort()[-k:][::-1]
    neighbour_tiers = [_tiers[i] for i in top_k]

    counts: dict[str, int] = {}
    for tier in neighbour_tiers:
        counts[tier] = counts.get(tier, 0) + 1

    # Ties break towards the MORE expensive tier. SPEC 8.4 rule 3: fail towards
    # quality. A 2-2-1 split between small and mid should not silently pick the
    # cheap side just because it appeared first.
    order = {"small": 0, "mid": 1, "large": 2}
    best_count = max(counts.values())
    winners = [t for t, c in counts.items() if c == best_count]
    tier = max(winners, key=lambda t: order[t])

    return tier, best_count / k
