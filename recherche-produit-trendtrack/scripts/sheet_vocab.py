#!/usr/bin/env python3
"""Read the complete dropdown vocabularies, including currently unused options.

Executed with the Google Workspace venv; no Sheets writes. Validation rules are
preferred over observed values. ONE_OF_RANGE (including named ranges) is resolved
through values.get. No fixed row cutoff. Missing dropdowns fail explicitly rather
than silently reducing the allowed choices to previously selected values.
"""
import json
import os
import sys
from pathlib import Path

SHEET_ID = "1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM"


def extract_vocab(metadata, read_range):
    choices = {"niches": [], "priorities": []}
    source_ranges = {}
    range_cache = {}
    for sheet in metadata.get("sheets", []):
        if sheet.get("properties", {}).get("title") != "Products":
            continue
        for grid in sheet.get("data", []):
            start_col = grid.get("startColumn", 0)
            start_row = grid.get("startRow", 0)
            for ri, row in enumerate(grid.get("rowData", [])):
                if start_row + ri == 0:
                    continue
                for ci, cell in enumerate(row.get("values", [])):
                    key = {4: "niches", 6: "priorities"}.get(start_col + ci)
                    if not key:
                        continue
                    cond = cell.get("dataValidation", {}).get("condition", {})
                    kind = cond.get("type")
                    values = []
                    for item in cond.get("values", []):
                        value = item.get("userEnteredValue", "")
                        if kind == "ONE_OF_LIST":
                            values.append(value)
                        elif kind == "ONE_OF_RANGE":
                            ref = value.lstrip("=")
                            # Relative references without sheet names belong to Products.
                            if "!" not in ref and ":" in ref:
                                ref = "'Products'!" + ref
                            if ref not in range_cache:
                                range_cache[ref] = read_range(ref)
                            values.extend(v for r in range_cache[ref] for v in r)
                            source_ranges.setdefault(key, [])
                            if ref not in source_ranges[key]:
                                source_ranges[key].append(ref)
                    for v in values:
                        v = str(v).strip()
                        if v and v not in choices[key]:
                            choices[key].append(v)
    for key in choices:
        if not choices[key]:
            raise RuntimeError(f"No dropdown options found for {key}; refusing incomplete observed-values fallback")
    return {**choices, "source": "google_sheets_data_validation", "source_ranges": source_ranges}


def fetch_vocab():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    creds = Credentials.from_authorized_user_file(str(home / "google_token.json"))
    session = AuthorizedSession(creds)
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
    response = session.get(base, params={
        "ranges": ["Products!E2:E", "Products!G2:G"],
        "includeGridData": "true",
        "fields": "sheets(properties(title),data(startColumn,startRow,rowData(values(dataValidation))))",
    }, timeout=90)
    response.raise_for_status()

    def read_range(ref):
        from urllib.parse import quote
        resp = session.get(base + "/values/" + quote(ref, safe=""), timeout=90)
        resp.raise_for_status()
        return resp.json().get("values", [])

    return extract_vocab(response.json(), read_range)


if __name__ == "__main__":
    try:
        print(json.dumps(fetch_vocab(), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"[sheet_vocab] {exc}", file=sys.stderr)
        sys.exit(1)
