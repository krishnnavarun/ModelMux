"""Score a graded blind spot-check (SPEC evaluation section, DECISIONS.md D23).

    python eval/grade_quality.py eval/results/spotcheck-<stamp>.json

`run_eval.py` exports pairs of answers with the tier withheld. A human fills in
`better` (A / B / same). This reads that back, unblinds it against the key, and
produces the quality column the savings figure is meaningless without.

-----------------------------------------------------------------------------
WHY BLIND, AND WHY IT IS UNBLINDED HERE RATHER THAN THERE
-----------------------------------------------------------------------------
A grader who knows "A is the cheap one" finds what they expect to find. The
export withholds the mapping and writes it to a separate `_KEY.json`; this
script is the only place the two are joined.

That separation is the whole method. If the key were in the same file, the
exercise would be theatre -- and nobody would be able to tell from the output.

-----------------------------------------------------------------------------
WHAT THIS DOES NOT DO
-----------------------------------------------------------------------------
No LLM-as-judge mode. That needs the `anthropic` SDK, which is not on the
approved dependency list in SPEC section 3, and an `ANTHROPIC_API_KEY`, which
this project has never had. Adding a dependency is a conversation, not a
default.

The human path needs neither, which is also why it was the recommended half of
D23: it is gradeable today.
"""

import argparse
import json
import sys
from pathlib import Path

VALID = {"A", "B", "SAME", "TIE", ""}


def load(path: Path) -> tuple[dict, list]:
    graded = json.loads(path.read_text(encoding="utf-8"))
    key_path = path.with_name(path.stem + "_KEY.json")
    if not key_path.exists():
        print(f"Key file not found: {key_path}\n"
              f"It is written alongside the spot-check by run_eval.py.",
              file=sys.stderr)
        raise SystemExit(2)
    return graded, json.loads(key_path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("spotcheck", type=Path)
    args = parser.parse_args()

    if not args.spotcheck.exists():
        print(f"Not found: {args.spotcheck}", file=sys.stderr)
        return 2

    graded, key = load(args.spotcheck)
    key_by_id = {k["id"]: k for k in key}

    items = graded.get("items", [])
    verdicts = []
    ungraded = []
    invalid = []

    for item in items:
        raw = str(item.get("better", "")).strip().upper()
        if raw == "":
            ungraded.append(item["id"])
            continue
        if raw not in VALID:
            invalid.append((item["id"], item.get("better")))
            continue

        mapping = key_by_id.get(item["id"])
        if mapping is None:
            invalid.append((item["id"], "no key entry"))
            continue

        if raw in ("SAME", "TIE"):
            verdicts.append("tie")
        else:
            # Unblinding happens HERE and only here.
            verdicts.append(mapping[raw])       # -> "routed" or "baseline"

    print("=" * 72)
    print("QUALITY -- blind spot-check, unblinded")
    print("=" * 72)

    if invalid:
        print("\n  INVALID ENTRIES (expected A / B / same):")
        for item_id, value in invalid:
            print(f"    #{item_id}: {value!r}")

    if ungraded:
        print(f"\n  {len(ungraded)} of {len(items)} items are still ungraded: "
              f"{ungraded[:12]}{'...' if len(ungraded) > 12 else ''}")

    if not verdicts:
        print("\n  NOTHING GRADED YET.")
        print("\n  Open the spot-check file and set `better` to A, B or same for")
        print("  each item. Do NOT open the _KEY file first -- knowing which")
        print("  answer is the cheap one is the bias the blind format removes.")
        print("=" * 72)
        return 1

    routed = verdicts.count("routed")
    baseline = verdicts.count("baseline")
    ties = verdicts.count("tie")
    n = len(verdicts)

    print(f"\n  graded: {n} of {len(items)}")
    print(f"\n  {'routed (ModelMux) better':<28} {routed:>3}   {routed/n:>5.0%}")
    print(f"  {'baseline (large) better':<28} {baseline:>3}   {baseline/n:>5.0%}")
    print(f"  {'indistinguishable':<28} {ties:>3}   {ties/n:>5.0%}")

    # "Not worse" is the question a cost router has to answer. Routing is
    # justified when the cheap answer is as good, not only when it wins.
    not_worse = routed + ties
    print(f"\n  ROUTED NOT WORSE: {not_worse}/{n} = {not_worse/n:.0%}")

    print("\n" + "-" * 72)
    if n < 20:
        print(f"  CAUTION: n={n}. One item is {1/n:.0%}. This is a smoke test,")
        print("  not a measurement. Grade at least 30 before quoting a figure.")
    elif baseline > routed + ties:
        print("  The baseline won more often than routing tied or won.")
        print("  The cost savings are being paid for in answer quality --")
        print("  raise the thresholds or route more prompts upward.")
    elif not_worse / n >= 0.9:
        print("  Routing holds quality on this sample. The savings figure is")
        print("  defensible, with the caveats below.")
    else:
        print("  Routing loses on a meaningful share of prompts. Report this")
        print("  number beside the savings, not instead of it.")

    print("""
  Two things this number is not:
    - Not independent. One grader, and the same person who built the router
      unless somebody else filled the file in.
    - Not a quality score. It is a PAIRWISE PREFERENCE between two answers to
      the same prompt -- it says routing held up against the baseline, not that
      either answer was good.
""")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
