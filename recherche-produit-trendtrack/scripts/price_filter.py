#!/usr/bin/env python3
"""
price_filter.py — Deterministic Price & Coefficient Filter

Applies the price rules and coefficient (X = Prix / COGS) thresholds
programmatically so the LLM doesn't have to do arithmetic.

Can be used in two modes:
  1. PRICE-ONLY (before COGS is known): filters by selling price threshold
  2. FULL (after COGS is found): filters by price + coefficient

Usage — price-only mode (Étape 2, selling price from landing page):
    python3 price_filter.py --mode price-only \\
      --input /tmp/tt_candidates_with_prices.json

Usage — full mode (after Étape 3, with COGS):
    python3 price_filter.py --mode full \\
      --input /tmp/tt_candidates_with_cogs.json

Input JSON: array of objects, each MUST have:
  - "price_eur": float (selling price in EUR)
  For full mode, also:
  - "cogs_eur": float (cost of goods in EUR)

Output:
  stdout: JSON array of candidates that PASS (with added "x_coefficient" and "priority_price" fields)
  stderr: rejection log + near-miss flagging

Rules applied:
  - Prix < 30€ → EXCLUDED
  - Near-miss 29.90–29.99€ → EXCLUDED (flagged separately for email recap)
  - Prix 30–40€ → X > 4 required (strictly greater than 4)
  - Prix > 40€ → X ≥ 3.5 required
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


def is_near_miss(price: float) -> bool:
    """Check if price falls in the near-miss zone (29.90–29.99€)."""
    return 29.90 <= price <= 29.99


def compute_coefficient(price: float, cogs: float) -> float:
    """Compute X = Price / COGS. Returns 0 if COGS is zero/None."""
    if not cogs or cogs <= 0:
        return 0.0
    return round(price / cogs, 2)


def check_coefficient(price: float, x: float) -> Tuple[bool, str]:
    """Check if coefficient X meets the threshold for price.
    Rules:
      - 30 <= price <= 40 -> X > 4.0 (strictly greater)
      - price > 40 -> X >= 3.5
    """
    if price <= 40.0:
        return (x > 4.0), "X > 4.0"
    return (x >= 3.5), "X >= 3.5"


def filter_price_only(
    candidates: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Filter by selling price threshold only (COGS not yet known).

    Returns (passed_candidates, near_miss_flags).
    """
    passed = []
    near_misses: List[str] = []

    for c in candidates:
        price = c.get("price_eur")
        name = c.get("title") or c.get("shop_domain") or c.get("ad_id") or "?"

        # Skip if no price data yet (will be checked later)
        if price is None:
            c["price_filter_status"] = "pending"
            passed.append(c)
            continue

        price = float(price)

        # Near-miss check (29.90–29.99€)
        if is_near_miss(price):
            near_misses.append(f"{name} — {price:.2f}€")
            print(
                f"SKIP [near-miss] {name} — {price:.2f}€ (29.90–29.99€ zone)",
                file=sys.stderr,
            )
            continue

        # Hard price floor
        if price < 30.0:
            print(
                f"SKIP [price-floor] {name} — {price:.2f}€ < 30€",
                file=sys.stderr,
            )
            continue

        c["price_filter_status"] = "passed"
        _, req_desc = check_coefficient(price, 0.0)
        c["min_coefficient_required"] = req_desc
        passed.append(c)

    return passed, near_misses


def filter_full(
    candidates: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Filter by price AND coefficient (after COGS is known).

    Returns (passed_candidates, near_miss_flags).
    """
    passed = []
    near_misses: List[str] = []

    for c in candidates:
        price = c.get("price_eur")
        cogs = c.get("cogs_eur")
        name = c.get("title") or c.get("shop_domain") or c.get("ad_id") or "?"

        if price is None:
            print(
                f"SKIP [no-price] {name} — no selling price available",
                file=sys.stderr,
            )
            continue

        price = float(price)

        # Near-miss check
        if is_near_miss(price):
            near_misses.append(f"{name} — {price:.2f}€")
            print(
                f"SKIP [near-miss] {name} — {price:.2f}€ (29.90–29.99€ zone)",
                file=sys.stderr,
            )
            continue

        # Hard price floor
        if price < 30.0:
            print(
                f"SKIP [price-floor] {name} — {price:.2f}€ < 30€",
                file=sys.stderr,
            )
            continue

        # Coefficient check (only if COGS is available)
        if cogs is not None and float(cogs) > 0:
            cogs = float(cogs)
            x = compute_coefficient(price, cogs)
            passed_coef, req_desc = check_coefficient(price, x)

            if not passed_coef:
                bracket = "30–40€" if price <= 40 else ">40€"
                print(
                    f"SKIP [coefficient] {name} — X={x:.2f} fails {req_desc} "
                    f"(prix={price:.2f}€, COGS={cogs:.2f}€, bracket={bracket})",
                    file=sys.stderr,
                )
                continue

            c["x_coefficient"] = x
            c["coefficient_status"] = "passed"
        else:
            # COGS not available — compute will happen with heuristic later
            c["x_coefficient"] = None
            c["coefficient_status"] = "pending_cogs"

        _, req_desc = check_coefficient(price, 0.0)
        c["min_coefficient_required"] = req_desc
        c["price_filter_status"] = "passed"
        passed.append(c)

    return passed, near_misses


def main():
    parser = argparse.ArgumentParser(
        description="Deterministic price & coefficient filter."
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["price-only", "full"],
        default="full",
        help="Filter mode: 'price-only' (before COGS) or 'full' (after COGS).",
    )
    parser.add_argument(
        "--input", "-i",
        help="Path to JSON file with candidates. Reads from stdin if omitted.",
    )
    parser.add_argument(
        "--near-misses-file",
        help="Optional path to write the list of near-miss products (for email recap).",
    )
    parser.add_argument(
        "--envelope",
        action="store_true",
        help="Wrap output in {candidates, near_misses, stats} dict instead of raw list.",
    )
    args = parser.parse_args()

    # Load candidates
    if args.input:
        with open(args.input) as f:
            raw_data = json.load(f)
    else:
        raw_data = json.load(sys.stdin)

    # Unwrap if wrapped in an envelope
    if isinstance(raw_data, dict) and "candidates" in raw_data:
        candidates = raw_data["candidates"]
    elif isinstance(raw_data, list):
        candidates = raw_data
    else:
        candidates = []

    print(
        f"[price_filter] Mode: {args.mode}, processing {len(candidates)} candidates",
        file=sys.stderr,
    )

    if args.mode == "price-only":
        passed, near_misses = filter_price_only(candidates)
    else:
        passed, near_misses = filter_full(candidates)

    rejected_count = len(candidates) - len(passed)
    print(
        f"[price_filter] Result: {len(passed)} pass / {rejected_count} rejected",
        file=sys.stderr,
    )

    if near_misses:
        print(
            f"[price_filter] Near-miss products (for email recap):",
            file=sys.stderr,
        )
        for nm in near_misses:
            print(f"  → {nm}", file=sys.stderr)

    # Optionally persist near-misses for email reporting
    if args.near_misses_file:
        try:
            with open(args.near_misses_file, "w") as f:
                json.dump(near_misses, f, indent=2, ensure_ascii=False)
            print(f"[price_filter] Near-misses written to {args.near_misses_file}", file=sys.stderr)
        except Exception as e:
            print(f"[price_filter] Error writing near-misses: {e}", file=sys.stderr)

    # Output to stdout
    if args.envelope:
        output = {
            "candidates": passed,
            "near_misses": near_misses,
            "stats": {
                "input_count": len(candidates),
                "passed_count": len(passed),
                "rejected_count": rejected_count,
                "near_miss_count": len(near_misses),
            },
        }
        json.dump(output, sys.stdout, indent=2, ensure_ascii=False)
    else:
        # Standard composable list of candidates
        json.dump(passed, sys.stdout, indent=2, ensure_ascii=False)

    print(file=sys.stdout)


if __name__ == "__main__":
    main()
