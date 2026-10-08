#!/usr/bin/env python3
"""List rows of the Tableau de Recherche Produit whose Validation column (A) is ticked.

Reads via the gws-backed GAPI wrapper (Google auth is durable). Output: JSON array
[{row, name, url, price}] printed to stdout. row = 1-based spreadsheet row.
"""
import json, os, subprocess, sys

SHEET_ID = "1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM"
RANGE = "'Products'!A1:J2000"
GAPI = [
    os.path.expanduser("~/.hermes/venvs/gws/bin/python"),
    os.path.expanduser("~/.hermes/skills/productivity/google-workspace/scripts/google_api.py"),
]
TICKED = {"true", "✔", "✓", "yes", "1"}

def main():
    out = subprocess.run(GAPI + ["sheets", "get", SHEET_ID, RANGE],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        print(json.dumps({"error": out.stderr.strip() or out.stdout.strip()}))
        sys.exit(1)
    rows = json.loads(out.stdout)
    results = []
    for i, r in enumerate(rows):
        if i == 0:
            continue  # header
        cells = (r + [""] * 10)[:10]
        flag = str(cells[0]).strip().lower()
        if flag in TICKED and str(cells[1]).strip():
            results.append({
                "row": i + 1,
                "name": str(cells[1]).strip(),
                "url": str(cells[2]).strip(),
                "pays": str(cells[3]).strip(),
                "priority": str(cells[6]).strip(),
                "cogs": str(cells[8]).strip(),
                "price": str(cells[9]).strip(),
            })
    print(json.dumps(results, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
