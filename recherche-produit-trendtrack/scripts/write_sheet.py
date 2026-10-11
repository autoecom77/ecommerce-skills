#!/usr/bin/env python3
"""write_sheet.py — Safe atomic row insertion into Products Google Sheet.

Finds the first empty row in column B (avoiding pre-formatted cells),
checks for duplicate URLs, and writes the 75-column rows atomically
via the Google Workspace CLI.

Usage:
    python3 write_sheet.py --rows /tmp/tt_run/rows.json
    python3 write_sheet.py --rows /tmp/tt_run/rows.json --dry-run
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_SHEET_ID = "1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM"
GAPI_PYTHON = Path(os.environ.get("GAPI_PYTHON", Path.home() / ".hermes/venvs/gws/bin/python"))
GAPI_SCRIPT = Path(os.environ.get("GAPI_SCRIPT", Path.home() / ".hermes/skills/productivity/google-workspace/scripts/google_api.py"))


def run_gapi(*args: str) -> Any:
    """Run google_api.py with the gws python interpreter."""
    if not GAPI_PYTHON.exists() or not GAPI_SCRIPT.exists():
        raise SystemExit(f"Google API tools not found: {GAPI_PYTHON} or {GAPI_SCRIPT}")

    cmd = [str(GAPI_PYTHON), str(GAPI_SCRIPT), *args]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise SystemExit(f"google_api.py error: {res.stderr[-800:]}")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return res.stdout


def write_to_sheet(
    rows: List[List[Any]],
    sheet_id: str = DEFAULT_SHEET_ID,
    dry_run: bool = False,
    result_out: str = "/tmp/tt_run/sheet_write_result.json",
) -> Dict[str, Any]:
    """Find last non-empty row and write new rows at exact position."""
    if not rows:
        print("[write_sheet] No rows to write", file=sys.stderr)
        return {"written": 0, "start": 0, "end": 0}

    assert all(len(r) == 75 for r in rows), "All rows must have exactly 75 columns"

    # Read existing sheet data to locate the first free row
    print("[write_sheet] Fetching existing rows from Sheet Products!A1:BW1000...", file=sys.stderr)
    sheet_data = run_gapi("sheets", "get", sheet_id, "Products!A1:BW1000")
    values = sheet_data.get("values", []) if isinstance(sheet_data, dict) else sheet_data

    last_row_index = 1
    for i, r in enumerate(values, start=1):
        # Row has content if any column from index 1 (B) onward is non-empty
        if len(r) > 1 and any(str(v).strip() for v in r[1:]):
            last_row_index = i

    start_row = last_row_index + 1
    end_row = last_row_index + len(rows)
    target_range = f"Products!A{start_row}:BW{end_row}"
    print(
        f"[write_sheet] Last data row = {last_row_index}; Target range = {target_range} ({len(rows)} rows)",
        file=sys.stderr,
    )

    # Safety check: target rows in existing sheet should be empty
    for rn in range(start_row, min(end_row + 1, len(values) + 1)):
        r = values[rn - 1] if rn - 1 < len(values) else []
        if len(r) > 1 and any(str(v).strip() for v in r[1:]):
            raise SystemExit(f"ABORT: Safety check failed. Row {rn} is not empty: {r[:4]}")

    # Safety check: check for duplicate URLs against already present rows
    existing_urls = {
        str(r[2]).strip()
        for r in values[1:]
        if len(r) > 2 and r[2]
    }
    dups = [r[2] for r in rows if str(r[2]).strip() in existing_urls]
    if dups:
        print(f"[write_sheet] Warning: {len(dups)} duplicate URLs already in sheet: {dups[:3]}", file=sys.stderr)

    if dry_run:
        print(f"[write_sheet] [DRY RUN] Would write {len(rows)} rows to {target_range}", file=sys.stderr)
        res = {"written": len(rows), "start": start_row, "end": end_row, "range": target_range, "dry_run": True}
    else:
        print(f"[write_sheet] Writing {len(rows)} rows atomically to {target_range}...", file=sys.stderr)
        rows_json = json.dumps(rows, ensure_ascii=False)
        run_gapi("sheets", "update", sheet_id, target_range, "--values", rows_json)
        print(f"[write_sheet] ✓ Successfully wrote {len(rows)} rows to {target_range}", file=sys.stderr)
        res = {"written": len(rows), "start": start_row, "end": end_row, "range": target_range, "dry_run": False}

    Path(result_out).parent.mkdir(parents=True, exist_ok=True)
    with open(result_out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)

    return res


def main() -> None:
    parser = argparse.ArgumentParser(description="Atomic Google Sheets writer for TrendTrack products.")
    parser.add_argument("--rows", "-r", default="/tmp/tt_run/rows.json", help="Path to rows JSON file")
    parser.add_argument("--sheet-id", "-s", default=DEFAULT_SHEET_ID, help="Google Sheet ID")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing to Sheet")
    parser.add_argument("--result-out", default="/tmp/tt_run/sheet_write_result.json", help="Output path for result JSON")
    args = parser.parse_args()

    with open(args.rows, "r", encoding="utf-8") as f:
        rows = json.load(f)

    res = write_to_sheet(rows, sheet_id=args.sheet_id, dry_run=args.dry_run, result_out=args.result_out)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
