"""Aggregate queries for the dashboard (SPEC section 8.9).

**Nothing here computes routing.** This module only reads rows that the request
path already wrote. Keeping that boundary means a bug in a dashboard query can
never change what a caller is served.

SPEC: "Percentiles: compute in SQL where possible, in Python otherwise. Do not
approximate silently."

SQLite has no `PERCENTILE_CONT`, so p50/p95 are computed in Python from the
latency column. That is exact, not approximate, and the cost is pulling the
latencies for the window into memory -- fine at this scale, and the honest
trade is stated rather than hidden behind a plausible-looking number.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

WINDOWS = {
    "1h": 1, "6h": 6, "12h": 12, "24h": 24, "7d": 168, "30d": 720,
}


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _percentile(sorted_values: list[float], fraction: float) -> float | None:
    """Exact nearest-rank percentile.

    Nearest-rank rather than interpolated: with a handful of requests,
    interpolation invents a latency that nobody experienced. The rank method
    always returns a value that actually occurred.
    """
    if not sorted_values:
        return None
    index = min(int(round(fraction * len(sorted_values) + 0.5)) - 1,
                len(sorted_values) - 1)
    return sorted_values[max(index, 0)]


def get_stats(db_path: Path, window: str = "24h") -> dict:
    """Aggregates for GET /v1/stats.

    Returns zeros and nulls for an empty window rather than raising -- a
    dashboard asking about a quiet night should render "nothing happened", not
    an error.
    """
    hours = WINDOWS.get(window)
    if hours is None:
        raise ValueError(f"unknown window '{window}'; valid: {sorted(WINDOWS)}")

    since = f"-{hours} hours"

    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT status, tier, cache_hit, fallback_fired, escalated,
                   latency_ms, cost_usd, cost_if_large_usd
            FROM requests
            WHERE timestamp >= datetime('now', ?)
            """,
            (since,),
        ).fetchall()

    total = len(rows)
    if total == 0:
        return {
            "window": window, "requests": 0, "cache_hit_rate": 0.0,
            "error_rate": 0.0, "fallback_count": 0, "escalated_count": 0,
            "cost_usd": 0.0, "cost_if_all_large_usd": 0.0, "cost_saved_usd": 0.0,
            "savings_pct": 0.0, "latency_p50_ms": None, "latency_p95_ms": None,
            "tier_distribution": {},
        }

    cache_hits = sum(1 for r in rows if r["cache_hit"])
    errors = sum(1 for r in rows if r["status"] == "error")
    fallbacks = sum(1 for r in rows if r["fallback_fired"])
    escalations = sum(1 for r in rows if r["escalated"])

    cost = sum(r["cost_usd"] or 0.0 for r in rows)
    baseline = sum(r["cost_if_large_usd"] or 0.0 for r in rows)

    # Latency from SUCCESSFUL requests only. Including errors would let a fast
    # 400 flatter the p50 and a timeout distort the p95 -- neither tells you
    # how long an answer takes.
    latencies = sorted(
        r["latency_ms"] for r in rows
        if r["status"] in ("success", "cached") and r["latency_ms"] is not None
    )

    tiers: dict[str, int] = {}
    for r in rows:
        if r["tier"]:
            tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1

    return {
        "window": window,
        "requests": total,
        "cache_hit_rate": round(cache_hits / total, 4),
        "error_rate": round(errors / total, 4),
        "fallback_count": fallbacks,
        "escalated_count": escalations,
        "cost_usd": round(cost, 8),
        "cost_if_all_large_usd": round(baseline, 8),
        "cost_saved_usd": round(baseline - cost, 8),
        "savings_pct": round((baseline - cost) / baseline * 100, 2) if baseline else 0.0,
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "tier_distribution": tiers,
        # SPEC section 6 / DECISIONS.md D3: cost_if_large_usd reprices the
        # tokens we actually observed at baseline rates. It is "same tokens,
        # baseline prices", NOT what the large tier would truly have cost. Any
        # UI showing savings must show this too.
        "savings_caveat": (
            "cost_if_all_large_usd reprices observed token counts at baseline "
            "rates; a larger model would likely produce a different output "
            "length. See DECISIONS.md D3."
        ),
    }


def get_recent(db_path: Path, limit: int = 50) -> list[dict]:
    """Recent rows for the live feed (SPEC section 8.9).

    Returns `prompt_preview`, never the full prompt -- the log does not store
    one (SPEC section 10).
    """
    limit = max(1, min(limit, 500))

    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT id, timestamp, prompt_preview, complexity_score, tier,
                   provider, model, cache_hit, escalated, fallback_fired,
                   attempts, tokens_in, tokens_out, cost_usd,
                   cost_if_large_usd, latency_ms, classify_ms,
                   cache_lookup_ms, status, error_message
            FROM requests
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
        {**dict(r),
         "cache_hit": bool(r["cache_hit"]),
         "escalated": bool(r["escalated"]),
         "fallback_fired": bool(r["fallback_fired"])}
        for r in rows
    ]
