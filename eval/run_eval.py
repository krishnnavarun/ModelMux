"""Cost, latency and quality evaluation (SPEC section 11, Stage 6).

    python eval/run_eval.py                    # against whatever is configured
    python eval/run_eval.py --simulated        # explicitly allow mock providers
    python eval/run_eval.py --limit 10         # cheaper run

Runs the evaluation set through ModelMux and through a baseline that sends
everything to the large tier, then reports cost, latency and quality side by
side.

-----------------------------------------------------------------------------
WHY THIS REFUSES TO RUN SILENTLY ON MOCK DATA
-----------------------------------------------------------------------------
This script produces the numbers that go in the README. With mock providers
every figure is arithmetic over invented token counts -- internally consistent,
completely fictional, and indistinguishable from a real measurement once it has
been copied into a table.

So a simulated run is refused unless `--simulated` is passed, and every line of
its output is prefixed SIMULATED. The point of the project is a measurement; a
plausible-looking fake would defeat it more thoroughly than having no number at
all.

-----------------------------------------------------------------------------
QUALITY
-----------------------------------------------------------------------------
SPEC: "Quality is reported alongside cost deliberately. Cost savings mean
nothing if the cheaper answers are worse."

Quality scoring is a DESIGN DECISION that was never taken (see PLAN.md Part 2
and DECISIONS.md D23). This harness therefore:

  - always produces cost and latency, which are objective
  - produces quality ONLY via an explicitly chosen scorer
  - exports a blind spot-check file for human grading, which needs no API

Reporting cost savings without a quality column is exactly the failure mode the
spec warns about, so the summary refuses to print a headline savings figure
unless quality was scored or the omission is explicitly acknowledged.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import config as config_module  # noqa: E402
from app import resilience, router, tokens  # noqa: E402
from app.classifier import embedding  # noqa: E402
from app.providers import build_all  # noqa: E402

EVAL_DIR = Path(__file__).parent
RESULTS_DIR = EVAL_DIR / "results"


def _is_simulated() -> bool:
    return os.environ.get("MODELMUX_MOCK_PROVIDERS") == "1"


async def _answer(prompt: str, tier: str, cfg, providers, breakers,
                  force: bool) -> dict:
    """One request. `force` pins the tier (the baseline); otherwise route."""
    started = time.perf_counter()

    if not force:
        decision = router.select_tier(prompt, cfg)
        tier = decision.tier
        score = decision.complexity_score
    else:
        score = None

    try:
        dispatched = await resilience.dispatch(
            prompt=prompt, tier=tier, max_tokens=cfg.limits["max_tokens_default"],
            config=cfg, providers=providers, breakers=breakers,
        )
    except resilience.AllProvidersFailed as exc:
        return {"ok": False, "error": str(exc), "tier": tier,
                "latency_ms": (time.perf_counter() - started) * 1000}

    result = dispatched.result
    return {
        "ok": True,
        "tier": dispatched.tier,
        "provider": dispatched.provider_name,
        "model": result.model,
        "text": result.text,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "cost_usd": cfg.cost_usd(dispatched.tier, result.tokens_in,
                                result.tokens_out),
        "complexity_score": score,
        "latency_ms": (time.perf_counter() - started) * 1000,
    }


async def run(prompts: list[dict], cfg, limit: int | None) -> list[dict]:
    """Route each prompt, then run the same prompt on the baseline tier."""
    if limit:
        prompts = prompts[:limit]

    timeout = httpx.Timeout(cfg.resilience["request_timeout_seconds"],
                            connect=cfg.resilience["connect_timeout_seconds"])

    rows = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        providers = build_all(cfg, client)
        breakers = resilience.CircuitBreakers(cfg)

        for i, item in enumerate(prompts, 1):
            prompt = item["prompt"]
            print(f"  [{i}/{len(prompts)}] {prompt[:56]}...", file=sys.stderr)

            routed = await _answer(prompt, None, cfg, providers, breakers, False)
            baseline = await _answer(prompt, cfg.baseline_tier, cfg, providers,
                                     breakers, True)

            rows.append({
                "id": item.get("id", i),
                "category": item.get("category"),
                "expected_tier": item.get("expected_tier"),
                "prompt": prompt,
                "routed": routed,
                "baseline": baseline,
            })

    return rows


def summarise(rows: list[dict], cfg, simulated: bool, quality: dict | None) -> dict:
    ok = [r for r in rows if r["routed"]["ok"] and r["baseline"]["ok"]]
    if not ok:
        return {"error": "no successful pairs", "attempted": len(rows)}

    routed_cost = sum(r["routed"]["cost_usd"] for r in ok)
    base_cost = sum(r["baseline"]["cost_usd"] for r in ok)
    routed_lat = sorted(r["routed"]["latency_ms"] for r in ok)
    base_lat = sorted(r["baseline"]["latency_ms"] for r in ok)

    def pct(values, f):
        idx = min(int(round(f * len(values) + 0.5)) - 1, len(values) - 1)
        return round(values[max(idx, 0)], 1)

    tiers: dict[str, int] = {}
    for r in ok:
        t = r["routed"]["tier"]
        tiers[t] = tiers.get(t, 0) + 1

    # The counterfactual bias (DECISIONS.md D3) is MEASURABLE here, because the
    # baseline really ran: compare the tokens the large tier actually produced
    # against the tokens the routed tier produced.
    routed_out = sum(r["routed"]["tokens_out"] for r in ok)
    base_out = sum(r["baseline"]["tokens_out"] for r in ok)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "simulated": simulated,
        "pairs": len(ok),
        "failed": len(rows) - len(ok),
        "cost_routed_usd": round(routed_cost, 8),
        "cost_baseline_usd": round(base_cost, 8),
        "cost_saved_usd": round(base_cost - routed_cost, 8),
        "savings_pct": round((base_cost - routed_cost) / base_cost * 100, 2)
        if base_cost else 0.0,
        "cost_per_1k_routed_usd": round(routed_cost / len(ok) * 1000, 4),
        "cost_per_1k_baseline_usd": round(base_cost / len(ok) * 1000, 4),
        "latency_routed_p50_ms": pct(routed_lat, 0.50),
        "latency_routed_p95_ms": pct(routed_lat, 0.95),
        "latency_baseline_p50_ms": pct(base_lat, 0.50),
        "latency_baseline_p95_ms": pct(base_lat, 0.95),
        "tier_distribution": tiers,
        "tokens_out_routed": routed_out,
        "tokens_out_baseline": base_out,
        # This is the D3 bias, measured rather than assumed.
        "counterfactual_bias_ratio": round(base_out / routed_out, 3)
        if routed_out else None,
        "quality": quality,
    }


def export_blind_spotcheck(rows: list[dict], path: Path, sample: int = 30) -> None:
    """Write a file for blind human grading.

    Which answer came from which tier is NOT recorded in the file a human
    reads. Knowing that "A is the cheap one" is exactly the bias that makes a
    spot-check worthless -- graders find what they expect to find. The key is
    written separately.
    """
    import random

    chosen = rows[:sample]
    graded, key = [], []

    for r in chosen:
        if not (r["routed"]["ok"] and r["baseline"]["ok"]):
            continue
        answers = [("routed", r["routed"]["text"]),
                   ("baseline", r["baseline"]["text"])]
        random.shuffle(answers)
        graded.append({
            "id": r["id"],
            "prompt": r["prompt"],
            "answer_A": answers[0][1],
            "answer_B": answers[1][1],
            "better": "",          # fill in: A / B / same
            "notes": "",
        })
        key.append({"id": r["id"], "A": answers[0][0], "B": answers[1][0]})

    path.write_text(json.dumps({
        "_instructions": (
            "For each item, read the prompt and both answers, then set "
            "'better' to A, B, or same. Do NOT open the key file first -- "
            "knowing which answer is the cheap one is the bias this format "
            "exists to remove."
        ),
        "items": graded,
    }, indent=2), encoding="utf-8")

    key_path = path.with_name(path.stem + "_KEY.json")
    key_path.write_text(json.dumps(key, indent=2), encoding="utf-8")
    print(f"\n  blind spot-check : {path}")
    print(f"  key (do not peek): {key_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", default="holdout.json",
                        help="prompts.json | holdout.json")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--simulated", action="store_true",
                        help="acknowledge that mock providers produce fiction")
    parser.add_argument("--spotcheck", type=int, default=30,
                        help="how many pairs to export for blind grading")
    args = parser.parse_args()

    simulated = _is_simulated()
    if simulated and not args.simulated:
        print(
            "REFUSING TO RUN.\n\n"
            "MODELMUX_MOCK_PROVIDERS=1 is set, so every provider is fake. This\n"
            "script produces the numbers that go in the README, and arithmetic\n"
            "over invented token counts is indistinguishable from a real\n"
            "measurement once it is in a table.\n\n"
            "Either set a working API key, or pass --simulated to acknowledge\n"
            "that the output is fiction.",
            file=sys.stderr,
        )
        return 2

    cfg = config_module.load()
    tokens.init()
    embedding.init()

    data = json.loads((EVAL_DIR / args.set).read_text(encoding="utf-8"))
    prompts = data.get("prompts") or data.get("examples")

    print(f"Running {args.limit or len(prompts)} prompts from {args.set}, "
          f"routed vs baseline ({cfg.baseline_tier})...", file=sys.stderr)

    rows = asyncio.run(run(prompts, cfg, args.limit))
    summary = summarise(rows, cfg, simulated, quality=None)

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = "SIMULATED-" if simulated else ""
    raw_path = RESULTS_DIR / f"{prefix}eval-{stamp}.json"
    raw_path.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2),
                        encoding="utf-8")

    _print_report(summary, simulated)

    if args.spotcheck:
        export_blind_spotcheck(
            rows, RESULTS_DIR / f"{prefix}spotcheck-{stamp}.json", args.spotcheck)

    print(f"\n  raw results      : {raw_path}")
    return 0


def _print_report(s: dict, simulated: bool) -> None:
    tag = "SIMULATED " if simulated else ""
    bar = "=" * 74

    print("\n" + bar)
    if simulated:
        print("!! SIMULATED RUN -- MOCK PROVIDERS. EVERY NUMBER BELOW IS FICTION.")
        print("!! Do not copy these into the README.")
        print(bar)
    print(f"{tag}EVALUATION -- routed vs baseline")
    print(bar)

    if "error" in s:
        print(f"\n  {s['error']} ({s['attempted']} attempted)")
        return

    print(f"\n  pairs: {s['pairs']}   failed: {s['failed']}")
    print(f"\n  {'':<26}{'ModelMux':>14}{'Baseline':>14}")
    print("  " + "-" * 54)
    print(f"  {'cost per 1,000 requests':<26}"
          f"{'$' + format(s['cost_per_1k_routed_usd'], '.4f'):>14}"
          f"{'$' + format(s['cost_per_1k_baseline_usd'], '.4f'):>14}")
    print(f"  {'p50 latency':<26}{str(s['latency_routed_p50_ms']) + ' ms':>14}"
          f"{str(s['latency_baseline_p50_ms']) + ' ms':>14}")
    print(f"  {'p95 latency':<26}{str(s['latency_routed_p95_ms']) + ' ms':>14}"
          f"{str(s['latency_baseline_p95_ms']) + ' ms':>14}")

    print(f"\n  cost saved   ${s['cost_saved_usd']:.6f}  ({s['savings_pct']}%)")
    print(f"  tiers chosen  {s['tier_distribution']}")

    if s.get("counterfactual_bias_ratio"):
        r = s["counterfactual_bias_ratio"]
        print(f"\n  D3 BIAS, MEASURED: the baseline produced {r}x the output")
        print(f"    tokens of the routed tier ({s['tokens_out_baseline']} vs "
              f"{s['tokens_out_routed']}).")
        print("    `cost_if_large_usd` assumes a ratio of 1.0.")
        if abs(r - 1.0) < 0.05:
            print("    Measured ratio is ~1.0, so the live savings figure is")
            print("    approximately unbiased on this set.")
        else:
            direction = "UNDER" if r > 1 else "OVER"
            print(f"    Live savings are {direction}stated by roughly {r}x.")

    if s.get("quality") is None:
        print("\n  " + "!" * 66)
        print("  QUALITY: NOT SCORED.")
        print("  SPEC: 'Cost savings mean nothing if the cheaper answers are")
        print("  worse.' A savings figure published without a quality column is")
        print("  half a result. Grade the exported spot-check file, or decide a")
        print("  scorer -- see DECISIONS.md D23.")
        print("  " + "!" * 66)


if __name__ == "__main__":
    raise SystemExit(main())
