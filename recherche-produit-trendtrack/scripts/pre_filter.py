#!/usr/bin/env python3
"""
pre_filter.py — Programmatic Pre-Filter for TrendTrack Candidates

Takes the JSON output from query_ads.py + existing Sheet data, and filters
out candidates whose shop domain already exists in the spreadsheet.

This runs BEFORE the LLM evaluates candidates, eliminating obvious duplicates
via deterministic domain matching.

Usage:
    python3 pre_filter.py \\
      --candidates /tmp/tt_candidates_raw.json \\
      --sheet-data /tmp/tt_sheet_existing.json

    # Or via stdin for candidates:
    cat /tmp/tt_candidates_raw.json | python3 pre_filter.py \\
      --sheet-data /tmp/tt_sheet_existing.json

Output:
    stdout: filtered candidates JSON array
    stderr: rejection log lines
"""

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Set


def normalize_domain(url: str) -> str:
    """Normalize a URL to a clean domain for comparison.

    Strips protocol, www., path, params, fragment.
    Lowercases everything.
    """
    if not url:
        return ""
    url = url.lower().strip()
    # Strip protocol
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    # Strip www.
    if url.startswith("www."):
        url = url[4:]
    # Extract just the domain (before first /)
    url = url.split("/")[0].split("?")[0].split("#")[0]
    return url.rstrip(".")


def build_existing_domains(sheet_data: Any) -> Set[str]:
    """Build a set of normalized domains from Sheet data.

    Accepts the JSON output from:
      $GAPI sheets get <ID> 'Products!B2:C2000'

    Which returns a structure like:
      {"values": [["Name", "URL"], ...]}
    or just a flat list of lists.
    """
    domains: Set[str] = set()

    # Handle dict wrapper from google_api.py
    if isinstance(sheet_data, dict):
        rows = sheet_data.get("values", [])
    elif isinstance(sheet_data, list):
        rows = sheet_data
    else:
        return domains

    for row in rows:
        if not isinstance(row, list):
            continue
        # Column C (URL) is index 1 in B2:C range (B=0, C=1)
        if len(row) >= 2 and row[1]:
            domain = normalize_domain(str(row[1]))
            if domain:
                domains.add(domain)
        # Also try column B (Name) index 0 — some might be domain-like
        if len(row) >= 1 and row[0]:
            val = str(row[0]).strip()
            # Only add if it looks like a domain (contains a dot)
            if "." in val and " " not in val:
                domains.add(normalize_domain(val))

    return domains


def pre_filter(
    candidates: List[Dict[str, Any]],
    existing_domains: Set[str],
) -> List[Dict[str, Any]]:
    """Filter candidates programmatically.

    Returns the list of candidates that pass all filters.
    Logs rejections to stderr.
    """
    filtered: List[Dict[str, Any]] = []

    for c in candidates:
        shop_domain = c.get("shop_domain", "")
        landing_url = c.get("landing_url", "")
        ad_id = c.get("ad_id", "?")

        # Use shop_domain first, fall back to domain from landing_url
        domain = shop_domain or normalize_domain(landing_url)

        if not domain:
            print(
                f"SKIP [no-domain] ad_id={ad_id} — no domain could be extracted",
                file=sys.stderr,
            )
            continue

        # 1. Already in Sheet?
        if domain in existing_domains:
            print(
                f"SKIP [already-in-sheet] {domain} — domain already in spreadsheet",
                file=sys.stderr,
            )
            continue

        # Candidate passes all filters
        filtered.append(c)

    return filtered


def main():
    parser = argparse.ArgumentParser(
        description="Programmatic pre-filter for TrendTrack candidates."
    )
    parser.add_argument(
        "--candidates",
        "-c",
        help="Path to JSON file with candidates from query_ads.py. "
        "If omitted, reads from stdin.",
    )
    parser.add_argument(
        "--sheet-data",
        "-s",
        required=True,
        help="Path to JSON file with existing Sheet data (B2:C2000).",
    )
    args = parser.parse_args()

    # Load candidates
    if args.candidates:
        with open(args.candidates) as f:
            candidates = json.load(f)
    else:
        candidates = json.load(sys.stdin)

    # Load sheet data
    with open(args.sheet_data) as f:
        sheet_data = json.load(f)

    existing_domains = build_existing_domains(sheet_data)
    print(
        f"[pre_filter] Loaded {len(existing_domains)} existing domains from sheet",
        file=sys.stderr,
    )
    print(
        f"[pre_filter] Processing {len(candidates)} candidates...",
        file=sys.stderr,
    )

    # Run filter
    filtered = pre_filter(candidates, existing_domains)

    print(
        f"[pre_filter] Result: {len(filtered)} pass / "
        f"{len(candidates) - len(filtered)} rejected",
        file=sys.stderr,
    )

    # Output filtered candidates
    json.dump(filtered, sys.stdout, indent=2, ensure_ascii=False)
    print(file=sys.stdout)


if __name__ == "__main__":
    main()
