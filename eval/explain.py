"""Show the full routing decision for any prompt (a dev tool, not the dashboard).

    python eval/explain.py "Prove Fermat's Last Theorem."
    python eval/explain.py                  # interactive, one prompt per line

Prints every signal, every weighted component, the floor, the reconciliation
between the two classifiers, and the resulting tier and cost. Calls no
provider and spends nothing -- routing is a pure function of the prompt.

This is the practical form of SPEC section 2's requirement that every routing
decision be explainable: if you cannot see why a prompt went where it did, the
classifier is a black box no matter how accurate it is.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as config_module  # noqa: E402
from app import router, tokens  # noqa: E402
from app.classifier import embedding, heuristic  # noqa: E402

BAR_WIDTH = 24


def bar(value: float, width: int = BAR_WIDTH) -> str:
    filled = int(round(value * width))
    return "#" * filled + "." * (width - filled)


def explain(prompt: str, config, warm: bool = True) -> None:
    # The FIRST classification in a process is several times slower than the
    # rest -- lazy imports, tokenizer warm-up, the model's first forward pass.
    # Timing it would report ~40ms against a 20ms budget and imply a violation
    # that does not exist in steady state. Warm first, then measure.
    if warm:
        router.select_tier(prompt, config)

    started = time.perf_counter()
    decision = router.select_tier(prompt, config)
    elapsed_ms = (time.perf_counter() - started) * 1000
    s = decision.signals

    print("\n" + "=" * 72)
    preview = " ".join(prompt.split())
    print(f'PROMPT  "{preview[:64]}{"..." if len(preview) > 64 else ""}"')
    print("=" * 72)

    # --- raw signals -------------------------------------------------------
    print("\nSIGNALS")
    rows = [
        ("token_count", s.get("token_count")),
        ("reasoning_markers", s.get("reasoning_markers")),
        ("reasoning_depth", f"{s.get('reasoning_depth')}  (0 none / 1 medium / 2 hard)"),
        ("is_lookup", s.get("is_lookup")),
        ("multi_question", s.get("multi_question")),
        ("has_code", s.get("has_code")),
        ("question_context_ratio", s.get("question_context_ratio")),
        ("output_length_request", s.get("output_length_request")),
        ("language_complexity", s.get("language_complexity")),
    ]
    for name, value in rows:
        print(f"  {name:<24} {value}")

    # --- weighted components ----------------------------------------------
    components = s.get("components")
    if components:
        print("\nWEIGHTED COMPONENTS")
        for name, value in components.items():
            weight = heuristic.WEIGHTS[name]
            contribution = weight * value
            print(f"  {name:<16} {value:>6.3f} x {weight:.2f} = {contribution:>6.3f}  "
                  f"{bar(value)}")
        print(f"  {'':<16} {'':>6} {'':>6}   {s['weighted_score']:>6.3f}  weighted sum")

        if s.get("floor_applied"):
            print(f"\n  FLOOR {s['floor_applied']:.2f} applied "
                  f"(strongest single signal overrides the average)")
            print(f"  blended -> {s['raw_score']:.3f}")
        if s.get("lookup_discount_applied"):
            print(f"  lookup discount x{heuristic.LOOKUP_DISCOUNT} applied")

    # --- classifier reconciliation ----------------------------------------
    if s.get("embedding_tier"):
        print("\nRECONCILIATION")
        print(f"  heuristic says   {s.get('heuristic_tier')}")
        print(f"  embedding says   {s['embedding_tier']}  "
              f"(confidence {s['embedding_confidence']:.0%} "
              f"= {round(s['embedding_confidence'] * 5)}/5 neighbours agreed)")
        print(f"  resolved         {decision.tier}"
              f"{'   <- ESCALATED' if decision.escalated else ''}")

    # --- outcome -----------------------------------------------------------
    provider = decision.provider
    cost_small = config.cost_usd("small", 500, 300)
    cost_this = config.cost_usd(decision.tier, 500, 300)
    cost_large = config.cost_usd(config.baseline_tier, 500, 300)

    print("\nDECISION")
    print(f"  score            {decision.complexity_score:.3f}   {bar(decision.complexity_score)}")
    t = config.classifier["thresholds"]
    print(f"  thresholds       small <= {t['small_max']} < mid <= {t['mid_max']} < large")
    print(f"  TIER             {decision.tier.upper()}")
    print(f"  provider/model   {provider['name']} / {provider['model']}")
    print(f"  why              {decision.reason}")
    print(f"  classify time    {elapsed_ms:.1f} ms   (budget 20 ms)")

    print("\nCOST for a hypothetical 500-in / 300-out exchange")
    print(f"  this tier ({decision.tier:<5})  ${cost_this:.6f}")
    print(f"  baseline (large)   ${cost_large:.6f}")
    if cost_this < cost_large:
        saved = cost_large - cost_this
        print(f"  saved              ${saved:.6f}  ({saved / cost_large:.0%} cheaper)")


def main() -> int:
    config = config_module.load()
    tokens.init()
    embedding.init()

    if len(sys.argv) > 1:
        explain(" ".join(sys.argv[1:]), config)
        return 0

    print("Type a prompt and press enter. Ctrl-C to quit.")
    try:
        while True:
            line = input("\n> ").strip()
            if line:
                explain(line, config)
    except (KeyboardInterrupt, EOFError):
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
