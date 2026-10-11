#!/usr/bin/env python3
"""run_pipeline.py — Master orchestrator for TrendTrack Product Research.

Runs the entire deterministic ecommerce research pipeline end-to-end:
  Étape 0  : Vérification crédits TrendTrack & extraction URL Sheet existantes
  Étape 1  : Recherche batch TrendTrack (query_ads_api.py)
  Étape 1.5: Dédoublonnage exact par domaine (pre_filter.py)
  Étape 2  : Extraction multithreadée des prix de vente web (extract_prices.py)
  Étape 2b : Pré-filtrage prix < 30€ & near-misses (price_filter.py --mode price-only)
  Étape 3  : Recherche COGS en cascade 3 niveaux (cogs_image_search.py)
  Étape 3b : Validation stricte ratio X (price_filter.py --mode full)
  Étape 4  : Enrichissement des top ads TrendTrack format ju-2 (enrich_api.py)
  Étape 5  : Formatage des 75 colonnes sans score (assemble_rows.py)
  Étape 6  : Écriture atomique dans Google Sheets (write_sheet.py)
  Étape 7  : Envoi de l'email récapitulatif (send_recap.py)

Usage:
    python3 run_pipeline.py [--pages 5] [--dry-run] [--date YYYY-MM-DD]
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
REST_KEY_PATH = HERMES_HOME / "mcp-tokens" / "trendtrack_rest_key.txt"
USAGE_URL = "https://api.trendtrack.io/v1/usage"
SHEET_ID = "1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM"

RUN_DIR = Path("/tmp/tt_run")
RUN_DIR.mkdir(parents=True, exist_ok=True)


def check_and_save_usage(dest_path: Path) -> Dict[str, Any]:
    """Fetch current TrendTrack usage and save to JSON."""
    if not REST_KEY_PATH.exists():
        print(f"[run_pipeline] Warning: No REST key at {REST_KEY_PATH}", file=sys.stderr)
        return {}
    key = REST_KEY_PATH.read_text().strip()
    req = urllib.request.Request(USAGE_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
            dest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return data
    except Exception as e:
        print(f"[run_pipeline] Warning: Could not check usage ({e})", file=sys.stderr)
        return {}


def run_cmd(cmd: List[str], desc: str) -> None:
    """Run a sub-command with logging and error abort."""
    t0 = time.time()
    print(f"\n[pipeline] === {desc} ===", file=sys.stderr)
    res = subprocess.run(cmd, text=True)
    if res.returncode != 0:
        raise SystemExit(f"[pipeline] ERROR: Step '{desc}' failed with exit code {res.returncode}")
    print(f"[pipeline] ✓ {desc} completed in {time.time()-t0:.1f}s", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Master TrendTrack product research pipeline.")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Run date (YYYY-MM-DD)")
    parser.add_argument("--pages", type=int, default=5, help="Number of pages per search method (default 5)")
    parser.add_argument("--dry-run", action="store_true", help="Run without modifying Sheet or sending real email")
    args = parser.parse_args()

    t_start = time.time()
    print(f"[pipeline] Starting TrendTrack Product Research Pipeline for {args.date}...", file=sys.stderr)

    # ---------------------------------------------------------
    # Étape 0 : Pré-checks
    # ---------------------------------------------------------
    usage_start_path = Path("/tmp/tt_usage_start.json")
    u0 = check_and_save_usage(usage_start_path)
    rem_credits = -1
    try:
        rem_credits = int(u0.get("credits", {}).get("totalRemaining") or u0.get("includedQuota", {}).get("remaining", -1))
    except Exception:
        pass

    if rem_credits >= 0 and rem_credits < 300:
        raise SystemExit(
            f"[pipeline] ABORT: Low TrendTrack credits ({rem_credits} < 300). "
            f"Stopping run to protect balance."
        )

    # Extraction des domaines existants du Sheet
    sheet_existing_path = Path("/tmp/tt_sheet_existing.json")
    gapi_py = HERMES_HOME / "venvs/gws/bin/python"
    gapi_script = HERMES_HOME / "skills/productivity/google-workspace/scripts/google_api.py"

    fetch_sheet_cmd = [
        str(gapi_py), str(gapi_script),
        "sheets", "get", SHEET_ID, "Products!B2:C2000"
    ]
    with open(sheet_existing_path, "w", encoding="utf-8") as f_out:
        res = subprocess.run(fetch_sheet_cmd, stdout=f_out, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            raise SystemExit(f"[pipeline] Failed to fetch existing Sheet rows: {res.stderr}")

    # ---------------------------------------------------------
    # Étape 1 : Recherche batch TrendTrack API
    # ---------------------------------------------------------
    raw_candidates_path = Path("/tmp/tt_candidates_raw.json")
    query_cmd = [
        sys.executable, str(SCRIPT_DIR / "query_ads_api.py"),
        "--date", args.date,
        "--pages", str(args.pages),
    ]
    with open(raw_candidates_path, "w", encoding="utf-8") as f_out:
        res = subprocess.run(query_cmd, stdout=f_out, text=True)
        if res.returncode != 0:
            raise SystemExit("[pipeline] query_ads_api.py failed")

    # ---------------------------------------------------------
    # Étape 1.5 : Dédoublonnage par rapport au Sheet
    # ---------------------------------------------------------
    filtered_path = Path("/tmp/tt_candidates_filtered.json")
    prefilter_cmd = [
        sys.executable, str(SCRIPT_DIR / "pre_filter.py"),
        "--candidates", str(raw_candidates_path),
        "--sheet-data", str(sheet_existing_path),
    ]
    with open(filtered_path, "w", encoding="utf-8") as f_out:
        res = subprocess.run(prefilter_cmd, stdout=f_out, text=True)
        if res.returncode != 0:
            raise SystemExit("[pipeline] pre_filter.py failed")

    # ---------------------------------------------------------
    # Étape 2 : Extraction des prix de vente web
    # ---------------------------------------------------------
    priced_path = Path("/tmp/tt_candidates_with_prices.json")
    extract_prices_cmd = [
        sys.executable, str(SCRIPT_DIR / "extract_prices.py"),
        "--input", str(filtered_path),
        "--output", str(priced_path),
    ]
    run_cmd(extract_prices_cmd, "Extraction des prix de vente web (extract_prices.py)")

    # ---------------------------------------------------------
    # Étape 2bis : Pré-filtrage prix < 30 € & near-misses
    # ---------------------------------------------------------
    near_misses_path = Path("/tmp/tt_near_misses.json")
    candidates_priced_kept = Path("/tmp/tt_candidates_priced.json")
    price_filter_cmd = [
        sys.executable, str(SCRIPT_DIR / "price_filter.py"),
        "--mode", "price-only",
        "--input", str(priced_path),
        "--near-misses-file", str(near_misses_path),
    ]
    with open(candidates_priced_kept, "w", encoding="utf-8") as f_out:
        res = subprocess.run(price_filter_cmd, stdout=f_out, text=True)
        if res.returncode != 0:
            raise SystemExit("[pipeline] price_filter.py (price-only) failed")

    # ---------------------------------------------------------
    # Étape 3 : Recherche COGS en cascade 3 niveaux
    # ---------------------------------------------------------
    with open(candidates_priced_kept, "r", encoding="utf-8") as f:
        candidates_to_cogs = json.load(f)

    print(f"\n[pipeline] === Recherche COGS pour {len(candidates_to_cogs)} candidats retenus ===", file=sys.stderr)
    cogs_script = SCRIPT_DIR / "cogs_image_search.py"
    with_cogs: List[Dict[str, Any]] = []

    NICHE_DIVISORS = {
        "cosmetique": 6.5,
        "maison": 5.0,
        "cuisine": 5.0,
        "apparel": 6.0,
        "sport": 5.0,
        "sante": 6.5,
        "puericulture": 6.0,
    }

    for idx, c in enumerate(candidates_to_cogs, start=1):
        p_name = c.get("product_title") or c.get("title") or c.get("shop_domain", "")
        img_url = c.get("product_image") or c.get("image_url", "")
        price_eur = float(c.get("price_eur", 0.0))
        print(f"[cogs] ({idx}/{len(candidates_to_cogs)}) {p_name[:35]} ({price_eur:.2f}€)...", file=sys.stderr)

        cogs_eur = None
        cogs_source = None
        cogs_note = ""

        if img_url:
            cmd = [
                sys.executable, str(cogs_script),
                "--image-url", img_url,
                "--product-name", p_name,
            ]
            cogs_res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            if cogs_res.returncode == 0 and cogs_res.stdout.strip():
                try:
                    cdata = json.loads(cogs_res.stdout)
                    if cdata.get("cogs_eur"):
                        cogs_eur = float(cdata["cogs_eur"])
                        cogs_source = cdata.get("source") or cdata.get("method")
                        cogs_note = f"COGS {cogs_source} {cogs_eur:.2f}€"
                except Exception:
                    pass

        # Fallback heuristique si non trouvé
        if cogs_eur is None:
            div = 5.5
            niche_lower = (c.get("niche") or "").lower()
            for k, d in NICHE_DIVISORS.items():
                if k in niche_lower:
                    div = d
                    break
            cogs_eur = round(price_eur / div, 2)
            cogs_source = "heuristique"
            cogs_note = f"COGS estimé (heuristique diviseur {div})"

        c["cogs_eur"] = cogs_eur
        c["cogs_source"] = cogs_source
        c["cogs_note"] = cogs_note
        with_cogs.append(c)

    candidates_with_cogs_path = Path("/tmp/tt_candidates_with_cogs.json")
    with open(candidates_with_cogs_path, "w", encoding="utf-8") as f:
        json.dump(with_cogs, f, indent=2, ensure_ascii=False)

    # ---------------------------------------------------------
    # Étape 3bis : Validation finale ratio X
    # ---------------------------------------------------------
    candidates_kept_path = Path("/tmp/tt_candidates_kept.json")
    price_full_cmd = [
        sys.executable, str(SCRIPT_DIR / "price_filter.py"),
        "--mode", "full",
        "--input", str(candidates_with_cogs_path),
        "--near-misses-file", str(near_misses_path),
    ]
    with open(candidates_kept_path, "w", encoding="utf-8") as f_out:
        res = subprocess.run(price_full_cmd, stdout=f_out, text=True)
        if res.returncode != 0:
            raise SystemExit("[pipeline] price_filter.py (full) failed")

    # ---------------------------------------------------------
    # Étape 4 : Enrichissement TrendTrack top ads
    # ---------------------------------------------------------
    enriched_path = Path("/tmp/tt_enriched.json")
    enrich_cmd = [
        sys.executable, str(SCRIPT_DIR / "enrich_api.py"),
        "--candidates", str(candidates_kept_path),
    ]
    with open(enriched_path, "w", encoding="utf-8") as f_out:
        res = subprocess.run(enrich_cmd, stdout=f_out, text=True)
        if res.returncode != 0:
            print("[pipeline] Warning: enrich_api.py failed, continuing without enrichment", file=sys.stderr)

    # ---------------------------------------------------------
    # Étape 5 : Formatage des 75 colonnes (assemble_rows.py)
    # ---------------------------------------------------------
    rows_path = RUN_DIR / "rows.json"
    final_products_path = RUN_DIR / "final_products.json"
    assemble_cmd = [
        sys.executable, str(SCRIPT_DIR / "assemble_rows.py"),
        "--candidates", str(candidates_kept_path),
        "--enriched", str(enriched_path),
        "--output", str(rows_path),
        "--products-out", str(final_products_path),
    ]
    run_cmd(assemble_cmd, "Assemblage des 75 colonnes Sheet (assemble_rows.py)")

    # ---------------------------------------------------------
    # Étape 6 : Écriture dans Google Sheets
    # ---------------------------------------------------------
    sheet_result_path = RUN_DIR / "sheet_write_result.json"
    write_cmd = [
        sys.executable, str(SCRIPT_DIR / "write_sheet.py"),
        "--rows", str(rows_path),
        "--result-out", str(sheet_result_path),
    ]
    if args.dry_run:
        write_cmd.append("--dry-run")
    run_cmd(write_cmd, "Écriture dans Google Sheets (write_sheet.py)")

    # ---------------------------------------------------------
    # Étape 7 : Envoi email récapitulatif
    # ---------------------------------------------------------
    usage_end_path = Path("/tmp/tt_usage_end.json")
    check_and_save_usage(usage_end_path)

    recap_cmd = [
        sys.executable, str(SCRIPT_DIR / "send_recap.py"),
        "--date", datetime.now().strftime("%d/%m/%Y"),
    ]
    if not args.dry_run:
        recap_cmd.append("--send")
    else:
        recap_cmd.append("--dry-run")
    run_cmd(recap_cmd, "Génération & Envoi du récapitulatif email (send_recap.py)")

    # ---------------------------------------------------------
    # Bilan Final
    # ---------------------------------------------------------
    final_products = []
    if final_products_path.exists():
        with open(final_products_path, "r", encoding="utf-8") as f:
            final_products = json.load(f)

    total_time = time.time() - t_start
    print(f"\n[pipeline] ====================================================", file=sys.stderr)
    print(f"[pipeline] Pipeline completed successfully in {total_time:.1f}s!", file=sys.stderr)
    print(f"[pipeline] Total products added: {len(final_products)}", file=sys.stderr)
    print(f"[pipeline] ====================================================", file=sys.stderr)

    summary = {
        "status": "success",
        "date": args.date,
        "duration_seconds": round(total_time, 1),
        "products_count": len(final_products),
        "dry_run": args.dry_run,
        "top_products": [
            {
                "title": p.get("title") or p.get("product_title"),
                "domain": p.get("shop_domain"),
                "price_eur": p.get("price_eur"),
                "cogs_eur": p.get("cogs_eur"),
                "x_coefficient": p.get("x_coefficient"),
                "url": p.get("product_url"),
            }
            for p in final_products[:3]
        ],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
