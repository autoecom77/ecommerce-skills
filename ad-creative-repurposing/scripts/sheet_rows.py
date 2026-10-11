#!/usr/bin/env python3
"""
List rows of the Tableau de Recherche Produit whose Validation column (A) is ticked.
Extracts product metadata and all competitor ad links (AD1..AD15).

Reads via the Google Workspace CLI (gws) or GAPI fallback.
Output: JSON array of products with ad links to stdout.
"""

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

SHEET_ID = "1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM"
RANGE = "'Products'!A1:AZ2000"
TICKED = {"true", "✔", "✓", "yes", "1"}

GWS_PATH = os.path.expanduser("~/.local/bin/gws")
GAPI_PYTHON = os.path.expanduser("~/.hermes/venvs/gws/bin/python")
GAPI_SCRIPT = os.path.expanduser("~/.hermes/skills/productivity/google-workspace/scripts/google_api.py")


def fetch_sheet_rows() -> List[List[Any]]:
    """Fetch sheet values using gws or GAPI."""
    # Method 1: gws CLI
    gws_bin = GWS_PATH if os.path.exists(GWS_PATH) else shutil.which("gws")
    if gws_bin:
        cmd = [
            gws_bin,
            "sheets",
            "spreadsheets",
            "values",
            "get",
            "--params",
            json.dumps({"spreadsheetId": SHEET_ID, "range": RANGE}),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            try:
                data = json.loads(res.stdout)
                return data.get("values", [])
            except Exception:
                pass

    # Method 2: GAPI wrapper
    if os.path.exists(GAPI_PYTHON) and os.path.exists(GAPI_SCRIPT):
        cmd = [GAPI_PYTHON, GAPI_SCRIPT, "sheets", "get", SHEET_ID, RANGE]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            try:
                return json.loads(res.stdout)
            except Exception:
                pass

    raise RuntimeError("Failed to fetch Google Sheet values via gws and GAPI.")


def get_ticked_products(filter_row: Optional[int] = None) -> List[Dict[str, Any]]:
    """Parse sheet rows and return products where Validation (Col A) is ticked."""
    rows = fetch_sheet_rows()
    results = []

    for i, r in enumerate(rows):
        if i == 0:
            continue  # header
        row_num = i + 1
        if filter_row is not None and row_num != filter_row:
            continue

        # Pad row to at least 52 columns
        cells = (r + [""] * 60)[:60]
        flag = str(cells[0]).strip().lower()

        # Check if ticked
        if flag not in TICKED and filter_row is None:
            continue

        name = str(cells[1]).strip()
        if not name:
            continue

        product_url = str(cells[2]).strip()
        country = str(cells[3]).strip()
        niche = str(cells[4]).strip()
        ads_count_reported = str(cells[5]).strip()
        priority = str(cells[6]).strip()
        price = str(cells[9]).strip()

        # Extract AD1..AD12 (Col L = index 11, Col P = index 15, step 4)
        ads: List[Dict[str, Any]] = []
        for slot in range(1, 13):
            col_idx = 11 + (slot - 1) * 4
            if col_idx < len(cells):
                ad_url = str(cells[col_idx]).strip()
                if ad_url and (ad_url.startswith("http://") or ad_url.startswith("https://")):
                    reach_tier = str(cells[col_idx + 1]).strip() if col_idx + 1 < len(cells) else ""
                    ads.append({
                        "slot": slot,
                        "url": ad_url,
                        "reach_tier": reach_tier,
                    })

        results.append({
            "row": row_num,
            "name": name,
            "url": product_url,
            "country": country,
            "niche": niche,
            "price": price,
            "priority": priority,
            "ads_count_reported": ads_count_reported,
            "ads": ads,
        })

    return results


def main() -> None:
    filter_row = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        filter_row = int(sys.argv[1])

    products = get_ticked_products(filter_row)
    print(json.dumps(products, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
