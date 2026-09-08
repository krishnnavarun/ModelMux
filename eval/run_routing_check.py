"""Stage 2 misroute report (SPEC section 11).

Runs every prompt in prompts.json through the router and compares the chosen
tier against the hand-labelled expectation. Prints the disagreements.

**No provider is called and no money is spent.** Routing is a pure function of
the prompt, so measuring it needs no network at all -- which is why this runs
today, while the Groq key is still invalid.

    python eval/run_routing_check.py

The output of this script is Stage 2's completion criterion. It is supposed to
find failures: the naive rule routes on length, and length is not difficulty.
"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as config_module  # noqa: E402
from app import router, tokens  # noqa: E402

TIER_ORDER = {"small": 0, "mid": 1, "large": 2}
PROMPTS_PATH = Path(__file__).parent / "prompts.json"


def main() -> int:
    config = config_module.load()
    tokens.init()

    if tokens.using_fallback():
        print("WARNING: tiktoken unavailable; token counts are char estimates.\n")

    data = json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
    prompts = data["prompts"]

    results = []
    for item in prompts:
        decision = router.select_tier(item["prompt"], config)
        expected = item["expected_tier"]
        actual = decision.tier
        # Direction matters more than the fact of a mismatch. Routing DOWN
        # (cheaper than needed) risks a bad answer; routing UP wastes money.
        # They are not equally bad, and the spec says to fail towards quality.
        delta = TIER_ORDER[actual] - TIER_ORDER[expected]
        results.append({
            "id": item["id"],
            "category": item["category"],
            "tokens": decision.signals["token_count"],
            "score": decision.complexity_score,
            "expected": expected,
            "actual": actual,
            "delta": delta,
            "prompt": item["prompt"],
        })

    correct = [r for r in results if r["delta"] == 0]
    too_cheap = [r for r in results if r["delta"] < 0]
    too_expensive = [r for r in results if r["delta"] > 0]

    print("=" * 78)
    print("STAGE 2 -- NAIVE TOKEN-COUNT ROUTER: MISROUTE REPORT")
    print("=" * 78)
    print(f"\nPrompts: {len(results)}")
    print(f"  correct        {len(correct):>3}  ({len(correct)/len(results):.0%})")
    print(f"  TOO CHEAP      {len(too_cheap):>3}  ({len(too_cheap)/len(results):.0%})"
          "   <- quality risk: sent a hard prompt to a weak model")
    print(f"  too expensive  {len(too_expensive):>3}  ({len(too_expensive)/len(results):.0%})"
          "   <- cost waste: sent an easy prompt to a strong model")

    print("\n" + "-" * 78)
    print("ACCURACY BY CATEGORY")
    print("-" * 78)
    by_cat: dict[str, list] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)
    for category in sorted(by_cat):
        rows = by_cat[category]
        ok = sum(1 for r in rows if r["delta"] == 0)
        bar = "#" * int(20 * ok / len(rows))
        print(f"  {category:<22} {ok:>2}/{len(rows):<3} {bar}")

    for title, group in (
        ("ROUTED TOO CHEAP (quality risk)", too_cheap),
        ("ROUTED TOO EXPENSIVE (cost waste)", too_expensive),
    ):
        if not group:
            continue
        print("\n" + "-" * 78)
        print(f"{title} -- {len(group)} cases")
        print("-" * 78)
        for r in sorted(group, key=lambda x: abs(x["delta"]), reverse=True):
            preview = " ".join(r["prompt"].split())[:64]
            print(f"  #{r['id']:<3} {r['expected']:>5} -> {r['actual']:<5} "
                  f"({r['tokens']:>4} tk, score {r['score']:.2f})  {preview}...")

    print("\n" + "=" * 78)
    print("WHAT THIS SHOWS")
    print("=" * 78)
    trap_cheap = sum(1 for r in too_cheap if r["category"].startswith("trap_"))
    trap_exp = sum(1 for r in too_expensive if r["category"].startswith("trap_"))
    print(f"""
Of {len(too_cheap) + len(too_expensive)} misroutes, {trap_cheap + trap_exp} are in
the deliberately-designed `trap_` categories. That is the naive rule working
exactly as predicted: it measures LENGTH, and the traps are cases where length
and DIFFICULTY point in opposite directions.

The {len(too_cheap)} too-cheap cases are the dangerous ones. A short, hard prompt
looks trivial to a token counter, so it goes to the weakest model and comes back
with a confidently wrong answer. Cost savings are worthless if bought this way,
which is why SPEC 8.4 rule 3 says to fail towards quality.

Stage 3 must add signals that see difficulty rather than size: reasoning
markers, code detection, multi-part questions, and embedding similarity to
labelled examples.
""")

    counts = Counter(r["actual"] for r in results)
    print(f"Tier distribution chosen: {dict(counts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
