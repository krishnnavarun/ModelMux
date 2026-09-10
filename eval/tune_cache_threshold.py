"""Choose the cache similarity threshold by measurement (SPEC section 8.5).

    python eval/tune_cache_threshold.py

SPEC calls `similarity_threshold` "the most dangerous setting in this project".
This script is how it stops being a guess.

-----------------------------------------------------------------------------
WHAT IS BEING TRADED
-----------------------------------------------------------------------------
Two error types, and they are NOT symmetric:

  MISS on an equivalent pair   -> a wasted provider call. Costs money.
  HIT on a dangerous pair      -> A WRONG ANSWER SERVED TO A USER.

The second is unbounded harm: a user asking about paracetamol receives an
answer about ibuprofen, with no indication anything was substituted. No hit
rate justifies that.

So this is not an accuracy optimisation. It is a safety threshold with a hit
rate attached, and the right reading of the table below is:

    find the lowest threshold with ZERO false hits, then see what
    hit rate is left over.

Uses the same embedding model as the classifier -- no extra load.
"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.classifier import embedding  # noqa: E402

PAIRS_PATH = Path(__file__).parent / "cache_pairs.json"
THRESHOLDS = [0.80, 0.84, 0.86, 0.88, 0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98]


def similarity(a: str, b: str) -> float:
    va, vb = embedding.encode(a), embedding.encode(b)
    return float(np.dot(va, vb))


def main() -> int:
    embedding.init()
    if not embedding.available():
        print("Embedding model unavailable -- cannot tune.")
        return 1

    data = json.loads(PAIRS_PATH.read_text(encoding="utf-8"))
    equivalent = [(a, b, similarity(a, b)) for a, b in data["equivalent"]]
    dangerous = [(a, b, similarity(a, b)) for a, b in data["dangerous"]]

    print("=" * 78)
    print("CACHE SIMILARITY THRESHOLD -- MEASURED")
    print("=" * 78)
    print(f"\n{len(equivalent)} equivalent pairs (should HIT), "
          f"{len(dangerous)} dangerous pairs (must NOT hit)\n")

    eq = np.array([s for _, _, s in equivalent])
    dg = np.array([s for _, _, s in dangerous])

    print("  similarity distribution")
    print(f"    equivalent  min {eq.min():.4f}  median {np.median(eq):.4f}  max {eq.max():.4f}")
    print(f"    dangerous   min {dg.min():.4f}  median {np.median(dg):.4f}  max {dg.max():.4f}")

    overlap = eq.min() < dg.max()
    print(f"\n  DISTRIBUTIONS OVERLAP: {overlap}")
    if overlap:
        print(f"    The most similar DANGEROUS pair ({dg.max():.4f}) scores higher")
        print(f"    than the least similar EQUIVALENT pair ({eq.min():.4f}).")
        print("    No threshold separates them perfectly. Every choice trades")
        print("    hit rate against wrong answers.")

    # --- the sweep ---------------------------------------------------------
    print("\n" + "-" * 78)
    print("  threshold | hit rate (equivalent) | FALSE HITS (dangerous) | verdict")
    print("-" * 78)

    safe_thresholds = []
    for t in THRESHOLDS:
        hits = int((eq >= t).sum())
        false_hits = int((dg >= t).sum())
        hit_rate = hits / len(eq)
        if false_hits == 0:
            verdict = "safe"
            safe_thresholds.append((t, hit_rate))
        elif false_hits <= 2:
            verdict = f"!! {false_hits} WRONG ANSWERS"
        else:
            verdict = f"!!! {false_hits} WRONG ANSWERS"
        print(f"     {t:.2f}   |   {hits:>2}/{len(eq)}  ({hit_rate:>4.0%})       |"
              f"        {false_hits:>2}/{len(dg)}          | {verdict}")

    # --- with the inversion guard -----------------------------------------
    from app.cache import is_inverted

    print(chr(10) + "-" * 78)
    print("WITH THE INVERSION GUARD (threshold AND a lexical check)")
    print("-" * 78)
    print("  threshold | hit rate (equivalent) | FALSE HITS (dangerous) | verdict")
    print("-" * 78)

    eq_guard = np.array([
        s for (a, b, s) in equivalent if not is_inverted(a, b)
    ])
    dg_guard = np.array([
        s for (a, b, s) in dangerous if not is_inverted(a, b)
    ])
    blocked_eq = len(equivalent) - len(eq_guard)
    blocked_dg = len(dangerous) - len(dg_guard)

    safe_guarded = []
    for t in THRESHOLDS:
        hits = int((eq_guard >= t).sum())
        false_hits = int((dg_guard >= t).sum())
        rate = hits / len(equivalent)
        verdict = "safe" if false_hits == 0 else f"!! {false_hits} WRONG"
        if false_hits == 0:
            safe_guarded.append((t, rate))
        print(f"     {t:.2f}   |   {hits:>2}/{len(equivalent)}  ({rate:>4.0%})       |"
              f"        {false_hits:>2}/{len(dangerous)}          | {verdict}")

    print(f"{chr(10)}  guard blocked {blocked_dg}/{len(dangerous)} dangerous pairs outright")
    print(f"  guard cost    {blocked_eq}/{len(equivalent)} equivalent pairs (false positives)")

    if safe_guarded:
        t, rate = min(safe_guarded, key=lambda x: x[0])
        print(f"{chr(10)}  >>> LOWEST SAFE THRESHOLD WITH GUARD: {t:.2f}  (hit rate {rate:.0%})")
    else:
        print(chr(10) + "  >>> STILL NO SAFE THRESHOLD even with the guard.")

    # --- the dangerous pairs that would have been served -------------------
    print("\n" + "-" * 78)
    print("DANGEROUS PAIRS RANKED BY SIMILARITY -- the top of this list is the risk")
    print("-" * 78)
    for a, b, s in sorted(dangerous, key=lambda x: -x[2])[:8]:
        print(f"  {s:.4f}  \"{a[:36]}\"")
        print(f"          vs \"{b[:36]}\"")

    # --- equivalent pairs we would lose ------------------------------------
    print("\n" + "-" * 78)
    print("EQUIVALENT PAIRS RANKED LOWEST -- these are the hits a high threshold loses")
    print("-" * 78)
    for a, b, s in sorted(equivalent, key=lambda x: x[2])[:5]:
        print(f"  {s:.4f}  \"{a[:36]}\"")
        print(f"          vs \"{b[:36]}\"")

    print("\n" + "=" * 78)
    if safe_thresholds:
        best_t, best_rate = min(safe_thresholds, key=lambda x: x[0])
        print(f"RECOMMENDATION: {best_t:.2f}")
        print(f"  Lowest threshold with ZERO false hits on this set.")
        print(f"  Hit rate on equivalent pairs: {best_rate:.0%}")
    else:
        print("NO SAFE THRESHOLD on this set -- every value serves a wrong answer.")
    print("=" * 78)
    print("""
CAVEAT, and it matters: these pairs were written by one person, and 20
dangerous pairs cannot map the whole space of confusable prompts. A threshold
with zero false hits HERE is not a threshold with zero false hits in
production. Treat it as a lower bound on risk, not a proof of safety, and pair
it with the `bypass_cache` flag for callers who cannot tolerate any chance of
substitution.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
