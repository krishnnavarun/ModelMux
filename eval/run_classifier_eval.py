"""Stage 3 accuracy report (SPEC section 11).

Runs every classifier mode against both evaluation sets and reports accuracy
side by side.

    python eval/run_classifier_eval.py

-----------------------------------------------------------------------------
WHY TWO SETS, AND WHY THE SECOND ONE IS THE REAL NUMBER
-----------------------------------------------------------------------------
`prompts.json` was used to TUNE the heuristic weights, marker lists and floors.
Reporting accuracy on it measures how well the weights were fitted to those 50
prompts -- not how the classifier will behave on anything new. It is a training
score.

`holdout.json` was written after tuning finished and has never influenced a
single constant. It is the only figure here that estimates generalisation.

A large gap between the two IS the finding: it is the size of the overfitting.

No provider is called and no money is spent -- classification is a pure
function of the prompt.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as config_module  # noqa: E402
from app import router, tokens  # noqa: E402
from app.classifier import embedding  # noqa: E402

TIER_ORDER = {"small": 0, "mid": 1, "large": 2}
EVAL_DIR = Path(__file__).parent
MODES = ["naive_tokens", "heuristic", "embedding", "hybrid"]


def load(name: str) -> list[dict]:
    data = json.loads((EVAL_DIR / name).read_text(encoding="utf-8"))
    return data["prompts"] if "prompts" in data else data["examples"]


def evaluate(prompts: list[dict], config, mode: str) -> dict:
    config.classifier["mode"] = mode
    correct = too_cheap = too_expensive = 0

    for item in prompts:
        decision = router.select_tier(item["prompt"], config)
        delta = TIER_ORDER[decision.tier] - TIER_ORDER[item["expected_tier"]]
        if delta == 0:
            correct += 1
        elif delta < 0:
            too_cheap += 1
        else:
            too_expensive += 1

    total = len(prompts)
    return {
        "accuracy": correct / total,
        "correct": correct,
        "too_cheap": too_cheap,
        "too_expensive": too_expensive,
        "total": total,
    }


def main() -> int:
    config = config_module.load()
    tokens.init()
    embedding.init()

    if not embedding.available():
        print("\n!! Embedding model unavailable -- 'embedding' and 'hybrid' "
              "rows below fall back to the heuristic and are NOT valid "
              "measurements of those modes.\n")

    tuned = load("prompts.json")
    holdout = load("holdout.json")

    print("=" * 78)
    print("STAGE 3 -- CLASSIFIER ACCURACY")
    print("=" * 78)

    for label, dataset, warning in (
        ("TUNED SET (prompts.json)", tuned,
         "weights were fitted to these -- a TRAINING score, not generalisation"),
        ("HELD-OUT SET (holdout.json)", holdout,
         "never used for tuning -- THIS is the honest number"),
    ):
        print(f"\n{label}   n={len(dataset)}")
        print(f"  ({warning})")
        print(f"  {'mode':<14}{'accuracy':>10}{'correct':>9}"
              f"{'too cheap':>11}{'too exp':>9}")
        print("  " + "-" * 53)
        for mode in MODES:
            r = evaluate(dataset, config, mode)
            print(f"  {mode:<14}{r['accuracy']:>9.0%}{r['correct']:>9}"
                  f"{r['too_cheap']:>11}{r['too_expensive']:>9}")

    # The gap between the two is the overfitting, stated as a number.
    print("\n" + "=" * 78)
    print("OVERFITTING CHECK")
    print("=" * 78)
    print(f"  {'mode':<14}{'tuned':>9}{'held-out':>11}{'gap':>9}")
    print("  " + "-" * 43)
    for mode in MODES:
        a = evaluate(tuned, config, mode)["accuracy"]
        b = evaluate(holdout, config, mode)["accuracy"]
        print(f"  {mode:<14}{a:>8.0%}{b:>11.0%}{a - b:>+9.0%}")

    print("""
A positive gap means the classifier does better on the prompts it was tuned
against than on new ones. Some gap is expected and honest. A large gap means
the weights encode those 50 prompts rather than any general notion of
difficulty -- and the held-out column is the only one worth quoting.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
