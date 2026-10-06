#!/usr/bin/env python3
"""
query_ads.py — Batch TrendTrack Ad Search

Runs all 3 search_ads query methods (Shopify reach growth, native ads,
volume of live ads) in a single batch via the TrendTrack MCP API.
Merges results, deduplicates by shop domain, and outputs a clean JSON
array to stdout for LLM processing.

Usage:
    python3 query_ads.py --date 2026-10-04
    python3 query_ads.py  # defaults to today

Credit cost: 3 × 30 = 90 credits (fixed, no retry loop).
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
TOKEN_PATH = HERMES_HOME / "mcp-tokens" / "trendtrack.json"
CLIENT_PATH = HERMES_HOME / "mcp-tokens" / "trendtrack.client.json"
META_PATH = HERMES_HOME / "mcp-tokens" / "trendtrack.meta.json"
MCP_ENDPOINT = "https://api.trendtrack.io/v1/mcp"


# ---------------------------------------------------------------------------
# OAuth token management
# ---------------------------------------------------------------------------
def load_token() -> Dict[str, Any]:
    """Load the stored TrendTrack OAuth token."""
    with open(TOKEN_PATH) as f:
        return json.load(f)


def save_token(token_data: Dict[str, Any]) -> None:
    """Persist updated token to disk."""
    with open(TOKEN_PATH, "w") as f:
        json.dump(token_data, f, indent=2)


def refresh_token_if_needed(token_data: Dict[str, Any]) -> Dict[str, Any]:
    """Refresh the access token if it has expired."""
    expires_at = token_data.get("expires_at", 0)
    if time.time() < expires_at - 60:
        return token_data  # Still valid (with 60s margin)

    print("[query_ads] Access token expired, refreshing...", file=sys.stderr)

    # Load client credentials and meta for the token endpoint
    with open(CLIENT_PATH) as f:
        client = json.load(f)
    with open(META_PATH) as f:
        meta = json.load(f)

    token_endpoint = meta["token_endpoint"]
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": token_data["refresh_token"],
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
    }).encode()

    req = urllib.request.Request(
        token_endpoint,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=15) as resp:
        new_token = json.loads(resp.read().decode())

    # Compute expires_at if not provided
    if "expires_at" not in new_token and "expires_in" in new_token:
        new_token["expires_at"] = time.time() + new_token["expires_in"]

    # Preserve refresh_token if not returned in response
    if "refresh_token" not in new_token:
        new_token["refresh_token"] = token_data["refresh_token"]

    new_token["hermes_issuer"] = token_data.get("hermes_issuer", "https://app.trendtrack.io")
    save_token(new_token)
    print("[query_ads] Token refreshed successfully.", file=sys.stderr)
    return new_token


# ---------------------------------------------------------------------------
# MCP call helper
# ---------------------------------------------------------------------------
def mcp_call(access_token: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
    """Call a TrendTrack MCP tool via the REST endpoint."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }).encode()

    req = urllib.request.Request(
        MCP_ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())

    if "error" in result:
        raise RuntimeError(f"MCP error: {result['error']}")

    return result.get("result", result)


# ---------------------------------------------------------------------------
# Query definitions
# ---------------------------------------------------------------------------
def build_queries(ref_date: datetime) -> List[Dict[str, Any]]:
    """Build the 3 search_ads query payloads based on the reference date."""
    j = ref_date.strftime("%Y-%m-%d")
    j_90 = (ref_date - timedelta(days=90)).strftime("%Y-%m-%d")
    j_30 = (ref_date - timedelta(days=30)).strftime("%Y-%m-%d")

    method1 = {
        "_tag": "shopify_reach_growth",
        "created_after": j_90,
        "created_before": j,
        "technologies": ["shopify"],
        "ad_countries": {"include": ["FR", "DE", "NL"]},
        "ad_languages": ["fr", "de", "nl"],
        "min_reach": 36000,
        "max_reach": 10000000,
        "reach_period": "total",
        "ad_reach_growth": [{"period": "last30d", "comparison": "greater", "value": 135}],
        "status": "active",
        "sort_by": "reachDelta30d",
        "order": "desc",
        "limit": 20,
    }

    method2 = {
        "_tag": "native_ads",
        "min_days_running": 20,
        "media_type": "image",
        "min_description_length": 1000,
        "ad_countries": {"include": ["FR", "DE", "NL"]},
        "ad_languages": ["fr", "de", "nl"],
        "ad_rank_mode": "percentile",
        "ad_rank_basis": "current",
        "max_ad_rank_value": 20,
        "reach_period": "last30d",
        "sort_by": "reach",
        "order": "desc",
        "status": "active",
        "limit": 20,
    }

    method3 = {
        "_tag": "volume_live",
        "created_after": j_30,
        "ad_countries": {"exclude": ["IN", "PK", "US"]},
        "min_active_ads": 50,
        "ads_time_period": "last24h",
        "status": "active",
        "trend_signal": "reach_growth_7d",
        "limit": 20,
    }

    return [method1, method2, method3]


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------
def normalize_domain(url: str) -> str:
    """Normalize a URL to a clean domain for dedup."""
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
    # Strip path, params, fragment
    url = url.split("/")[0].split("?")[0].split("#")[0]
    return url.rstrip(".")


def extract_ad_fields(ad: Dict[str, Any], method_tag: str) -> Optional[Dict[str, Any]]:
    """Extract and normalize fields from a raw search_ads result entry."""
    # The MCP response structure may vary — handle nested and flat formats
    if isinstance(ad, str):
        # Sometimes the MCP returns text content; skip those
        return None

    # Try to extract from common field names
    ad_id = (
        ad.get("ad_id")
        or ad.get("ad_identifier")
        or ad.get("id")
        or ""
    )
    trendtrack_url = (
        ad.get("trendtrack_url")
        or ad.get("url")
        or ""
    )
    shop_domain = (
        ad.get("shop_domain")
        or ad.get("domain")
        or ad.get("shop", {}).get("domain", "") if isinstance(ad.get("shop"), dict) else ""
        or ""
    )
    landing_url = (
        ad.get("landing_url")
        or ad.get("landing_page")
        or ad.get("destination_url")
        or ""
    )
    title = ad.get("title") or ad.get("name") or ""
    description = ad.get("description") or ad.get("body_text") or ""
    image_url = (
        ad.get("image_url")
        or ad.get("image")
        or ad.get("creative_url")
        or ad.get("thumbnail")
        or ""
    )

    # Metrics
    reach_total = ad.get("reach_total") or ad.get("reach") or 0
    reach_delta_30d = ad.get("reach_delta_30d") or ad.get("reachDelta30d") or 0
    active_ads = ad.get("active_ads") or ad.get("activeAds") or 0
    days_running = ad.get("days_running") or ad.get("daysRunning") or 0
    country_breakdown = ad.get("country_breakdown") or ad.get("countries") or {}
    media_type = ad.get("media_type") or ad.get("mediaType") or ""
    first_seen = ad.get("first_seen") or ad.get("firstSeen") or ad.get("created_at") or ""
    last_seen = ad.get("last_seen") or ad.get("lastSeen") or ""

    # If no domain could be extracted, try to derive from landing_url
    if not shop_domain and landing_url:
        shop_domain = normalize_domain(landing_url)

    return {
        "ad_id": str(ad_id),
        "trendtrack_url": trendtrack_url,
        "shop_domain": normalize_domain(shop_domain) if shop_domain else "",
        "landing_url": landing_url,
        "title": title,
        "description": description[:500] if description else "",
        "image_url": image_url,
        "reach_total": reach_total,
        "reach_delta_30d": reach_delta_30d,
        "active_ads": active_ads,
        "days_running": days_running,
        "country_breakdown": country_breakdown,
        "media_type": media_type,
        "method": method_tag,
        "first_seen": first_seen,
        "last_seen": last_seen,
    }


def parse_mcp_response(response: Any) -> List[Dict[str, Any]]:
    """Parse MCP tool response into a list of ad dicts.

    The MCP response from tools/call wraps the data in a 'content' array
    where each item has a 'type' and a 'text' field (for text content).
    The actual ad data is JSON embedded in the text field.
    """
    # Handle the standard MCP content array format
    if isinstance(response, dict) and "content" in response:
        contents = response["content"]
        ads = []
        for item in contents:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        ads.extend(parsed)
                    elif isinstance(parsed, dict):
                        # Could be a single result or a wrapper
                        if "data" in parsed:
                            data = parsed["data"]
                            if isinstance(data, list):
                                ads.extend(data)
                            else:
                                ads.append(data)
                        elif "results" in parsed:
                            ads.extend(parsed["results"])
                        else:
                            ads.append(parsed)
                except json.JSONDecodeError:
                    pass  # Non-JSON text content, skip
        return ads

    # Direct array response
    if isinstance(response, list):
        return response

    # Wrapped in data/results key
    if isinstance(response, dict):
        if "data" in response:
            data = response["data"]
            return data if isinstance(data, list) else [data]
        if "results" in response:
            return response["results"]

    return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Batch TrendTrack ad search — runs 3 methods, merges, deduplicates."
    )
    parser.add_argument(
        "--date", "-d",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Reference date YYYY-MM-DD (default: today).",
    )
    args = parser.parse_args()

    ref_date = datetime.strptime(args.date, "%Y-%m-%d")

    # Load and refresh token
    token_data = load_token()
    token_data = refresh_token_if_needed(token_data)
    access_token = token_data["access_token"]

    # Build and execute all 3 queries
    queries = build_queries(ref_date)
    all_ads: List[Dict[str, Any]] = []

    for query in queries:
        method_tag = query.pop("_tag")
        print(f"[query_ads] Running method: {method_tag}...", file=sys.stderr)

        try:
            response = mcp_call(access_token, "search_ads", query)
            raw_ads = parse_mcp_response(response)
            print(f"[query_ads]   → {len(raw_ads)} raw results", file=sys.stderr)

            for raw_ad in raw_ads:
                parsed = extract_ad_fields(raw_ad, method_tag)
                if parsed and parsed["shop_domain"]:
                    all_ads.append(parsed)
        except Exception as e:
            print(f"[query_ads] ERROR in {method_tag}: {e}", file=sys.stderr)

    print(f"[query_ads] Total raw results: {len(all_ads)}", file=sys.stderr)

    # Deduplicate by shop domain — keep highest reach entry
    seen: Dict[str, Dict[str, Any]] = {}
    for ad in all_ads:
        domain = ad["shop_domain"]
        if not domain:
            continue
        existing = seen.get(domain)
        if existing is None:
            seen[domain] = ad
        else:
            # Keep the one with higher reach; merge method tags
            if ad.get("reach_total", 0) > existing.get("reach_total", 0):
                methods = f"{existing['method']},{ad['method']}"
                ad["method"] = methods
                seen[domain] = ad
            else:
                existing["method"] = f"{existing['method']},{ad['method']}"

    deduped = list(seen.values())
    print(f"[query_ads] After dedup: {len(deduped)} unique shops", file=sys.stderr)

    # Sort by reach_total descending
    deduped.sort(key=lambda x: x.get("reach_total", 0), reverse=True)

    # Output JSON to stdout
    json.dump(deduped, sys.stdout, indent=2, ensure_ascii=False)
    print(file=sys.stdout)  # trailing newline


if __name__ == "__main__":
    main()
