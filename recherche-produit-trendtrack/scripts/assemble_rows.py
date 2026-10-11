#!/usr/bin/env python3
"""assemble_rows.py — Format 75-column Google Sheet rows sorted by X ratio.

Builds Google Sheet rows for the Products tab (columns A to BW, exactly 75 cols).
Sorts candidates by margin potential (X coefficient descending, then reach).
Score /100 and Priority columns are left empty as per user specification.

Inputs:
    --candidates / -c : Input candidates JSON (from price_filter full mode)
    --enriched / -e   : Enriched ads JSON (from enrich_api.py, optional)
    --output / -o     : Output path for rows JSON (default: /tmp/tt_run/rows.json)
    --products-out    : Output path for final products JSON (default: /tmp/tt_run/final_products.json)

Usage:
    python3 assemble_rows.py \
      --candidates /tmp/tt_candidates_kept.json \
      --enriched /tmp/tt_enriched.json \
      --output /tmp/tt_run/rows.json
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
]


def format_mmm_yy(iso_date: str) -> str:
    """Format ISO date to 'Mmm YY with leading apostrophe for Google Sheets."""
    if not iso_date:
        return ""
    try:
        dt = datetime.strptime(str(iso_date)[:10], "%Y-%m-%d")
        return f"'{MONTHS[dt.month - 1]} {dt.strftime('%y')}"
    except (ValueError, TypeError):
        return ""


def reach_to_bucket(reach: Any) -> str:
    """Convert raw reach number into standardized bucket string."""
    try:
        r = int(reach or 0)
    except (ValueError, TypeError):
        r = 0

    if r >= 10_000_000:
        return "10 M +"
    if r >= 7_000_000:
        return "7 - 10 M"
    if r >= 3_000_000:
        return "3 - 7 M"
    if r >= 1_000_000:
        return "1 - 3 M"
    return "< 1 M"


def format_thousands(n: Any) -> str:
    """Format integer with French non-breaking space separator."""
    try:
        val = int(n or 0)
        return f"{val:,}".replace(",", "\u202f")
    except (ValueError, TypeError):
        return "0"


def assemble_rows(
    candidates: List[Dict[str, Any]],
    enriched_data: Dict[str, Dict[str, Any]],
) -> Tuple_rows:
    """Convert candidates into 75-column Sheet rows sorted by X coefficient descending."""
    rows_meta = []

    for c in candidates:
        domain = c.get("shop_domain", "")
        price = float(c.get("price_eur") or 0.0)
        cogs = float(c.get("cogs_eur") or 0.0)
        x_coeff = float(c.get("x_coefficient") or (round(price / cogs, 2) if cogs > 0 else 0.0))
        c["x_coefficient"] = x_coeff

        title = c.get("title_fr") or c.get("product_title") or c.get("title") or domain
        product_url = c.get("product_url") or c.get("landing_url") or ""
        country = c.get("country_fr") or c.get("country") or "France"
        niche = c.get("niche") or "Divers"

        e = enriched_data.get(domain, {})
        ads = list(e.get("ads") or [])

        # Fallback: if enrichment returned no ads, include the search ad that surfaced the product
        if not ads and c.get("trendtrack_url"):
            ads = [{
                "trendtrack_url": c.get("trendtrack_url", ""),
                "reach": c.get("reach_total") or 0,
                "first_seen": (c.get("first_seen") or "")[:10],
                "last_seen": (c.get("last_seen") or "")[:10],
            }]
        elif c.get("trendtrack_url") and all(a.get("trendtrack_url") != c.get("trendtrack_url") for a in ads):
            ads.append({
                "trendtrack_url": c.get("trendtrack_url", ""),
                "reach": c.get("reach_total") or 0,
                "first_seen": (c.get("first_seen") or "")[:10],
                "last_seen": (c.get("last_seen") or "")[:10],
            })

        ads.sort(key=lambda a: a.get("reach") or 0, reverse=True)
        ads = ads[:8]

        live_ads = c.get("active_ads") or e.get("ads_count") or len(ads)
        ads_col_val = str(min(live_ads, 20) if live_ads else min(len(ads), 20))

        # Build columns 0 to 10 (A to K)
        # Note: Priority (col 6) and Score /100 (col 72) are left empty as requested
        row = [
            "FALSE",                 # Col 0 (A): Checkbox
            title,                   # Col 1 (B): Nom du Produit
            product_url,             # Col 2 (C): URL
            country,                 # Col 3 (D): Pays
            niche,                   # Col 4 (E): Niche
            ads_col_val,             # Col 5 (F): Ads
            "",                      # Col 6 (G): Priorité (laissé vide)
            "",                      # Col 7 (H): Kalodata / vide
            f"{cogs:.2f}€",          # Col 8 (I): COGS
            f"{price:.2f}€",         # Col 9 (J): Prix
            f"{x_coeff:.2f}",        # Col 10 (K): X
        ]

        # Blocs AD1 à AD15 (Cols 11 à 70) — 4 cols par ad, max 8 ads
        for a in ads:
            row.extend([
                a.get("trendtrack_url", ""),
                reach_to_bucket(a.get("reach")),
                format_mmm_yy(a.get("first_seen")),
                format_mmm_yy(a.get("last_seen")),
            ])

        # Pad up to col 71 (BT)
        row.extend([""] * (71 - len(row)))

        # Col 71 (BT): Commentaire standardisé
        reach_tot = c.get("reach_total") or 0
        delta_7d = c.get("reach_delta_7d") or 0
        cogs_note = c.get("cogs_note") or f"COGS {cogs:.2f}€ ({c.get('cogs_source', '1688')})"
        comment = (
            f"M1 reach growth. {cogs_note}. "
            f"{live_ads} ads actives, reach top ad {format_thousands(reach_tot)} "
            f"(+{format_thousands(delta_7d)} sur 7j). {c.get('note', '')}"
        ).strip()
        comment = " ".join(comment.split())

        row.extend([
            comment,  # Col 71 (BT): Commentaire
            "",       # Col 72 (BU): Score /100 (laissé vide)
            "",       # Col 73 (BV): Concurrent 1
            "",       # Col 74 (BW): Concurrent 2
        ])

        assert len(row) == 75, f"Row length must be exactly 75, got {len(row)}"
        rows_meta.append((x_coeff, reach_tot, row, c))

    # Sort strictly by X coefficient descending, then by total reach descending
    rows_meta.sort(key=lambda t: (-t[0], -t[1]))

    sorted_rows = [t[2] for t in rows_meta]
    sorted_candidates = [t[3] for t in rows_meta]
    return sorted_rows, sorted_candidates


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble 75-column Sheet rows sorted by X ratio.")
    parser.add_argument("--candidates", "-c", required=True, help="Path to /tmp/tt_candidates_kept.json")
    parser.add_argument("--enriched", "-e", help="Path to /tmp/tt_enriched.json (optional)")
    parser.add_argument("--output", "-o", default="/tmp/tt_run/rows.json", help="Output path for rows JSON")
    parser.add_argument("--products-out", default="/tmp/tt_run/final_products.json", help="Output path for products JSON")
    args = parser.parse_args()

    with open(args.candidates, "r", encoding="utf-8") as f:
        candidates = json.load(f)

    enriched_data: Dict[str, Dict[str, Any]] = {}
    if args.enriched and Path(args.enriched).exists():
        with open(args.enriched, "r", encoding="utf-8") as f:
            enr_list = json.load(f)
            for item in enr_list:
                enriched_data[item.get("shop_domain", "")] = item

    rows, final_products = assemble_rows(candidates, enriched_data)

    # Ensure output directory exists
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.products_out).parent.mkdir(parents=True, exist_ok=True)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)

    with open(args.products_out, "w", encoding="utf-8") as f:
        json.dump(final_products, f, indent=1, ensure_ascii=False)

    print(f"[assemble_rows] Assembled {len(rows)} rows (75 cols) sorted by X descending", file=sys.stderr)
    for p in final_products[:5]:
        print(
            f"  - X={p['x_coefficient']:.2f} | {p['price_eur']:.2f}€ / {p['cogs_eur']:.2f}€ | "
            f"{p.get('shop_domain', '')} | {p.get('title', '')[:40]}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
