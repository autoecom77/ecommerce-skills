#!/usr/bin/env python3
"""
enrich_api.py — Programmatic TrendTrack enrichment via the PUBLIC REST API.

Replaces the MCP-based enrichment (search_ads + find_similar_shops MCP tools).
Input : JSON array of candidates with a `shop_domain` field (output of
        query_ads_api.py / pre_filter.py).
Output: JSON array, same candidates enriched with:
  - ads            : list of ads (id, trendtrack_url, reach, spend, first/last
                     seen, media_url, landing_url) sorted by reach desc
  - ads_count      : total active ads found (capped count)
  - CONCURRENTS ARE DROPPED (user removed the competitors enrichment 2026-10-09)

For each shop:
  POST /v1/ads/query  {search, searchType:"domain", status:"active",
                       limit 20 (default, no pagination), sortBy reach desc}
  → ads = rows back, each with trendtrack_url = https://app.trendtrack.io/ads/<id>
    (this is the TrendTrack ad link, NOT the native final ad URL — user
     requested 2026-10-09).

Usage:
  python3 enrich_api.py --candidates /tmp/tt_candidates_filtered.json > /tmp/enriched.json
  python3 enrich_api.py --shop luveon.com            # single shop test (cheap)

API key: ~/.hermes/mcp-tokens/trendtrack_rest_key.txt
Cost: same backend as MCP search_ads (~5 credits per small query reviewed;
      full 20-ads query per shop comparable to the previous 30-credit MCP call).
"""

import argparse
import json
import sys
from typing import Any, Dict, List

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from query_ads_api import api_query, load_api_key  # noqa: E402


def enrich_shop(domain: str, api_key: str, limit: int = 20) -> Dict[str, Any]:
    """Fetch active ads for a shop domain via the public REST API."""
    payload = {
        "search": domain,
        "searchType": "domain",
        "status": "active",
        "limit": limit,
        "sortBy": "reach",
        "order": "desc",
    }
    resp = api_query(payload, api_key)
    rows = resp.get("data") or []
    ads = []
    for ad in rows:
        content = ad.get("content") or {}
        metrics = ad.get("metrics") or {}
        media = ad.get("media") or {}
        ad_id = str(ad.get("id") or "")
        ads.append({
            "ad_id": ad_id,
            "trendtrack_url": f"https://app.trendtrack.io/en/ju-2/explorer?tab=ads&ad={ad_id}&adSource=meta" if ad_id else "",
            "reach": metrics.get("reach") or 0,
            "estimated_spend": metrics.get("estimatedSpend") or 0,
            "first_seen": (ad.get("firstSeenAt") or "")[:10],
            "last_seen": (ad.get("lastSeenAt") or "")[:10],
            "days_running": ad.get("daysRunning") or 0,
            "media_url": media.get("mediaUrl") or media.get("thumbnailUrl") or "",
            "landing_url": content.get("landingPageUrl") or "",
            "body": (content.get("body") or "")[:200],
        })
    return {"domain": domain, "ads": ads, "ads_count": len(ads), "error": ""}


def main() -> None:
    parser = argparse.ArgumentParser(description="TrendTrack enrichment via public REST API (ads only).")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--candidates", "-c", help="JSON array of candidates with shop_domain.")
    src.add_argument("--shop", "-s", help="Single shop domain to enrich (test mode).")
    parser.add_argument("--limit", type=int, default=20, help="Max ads per shop (default 20).")
    args = parser.parse_args()

    api_key = load_api_key()

    if args.shop:
        result = enrich_shop(args.shop, api_key, args.limit)
        json.dump([result], sys.stdout, indent=2, ensure_ascii=False)
        print(file=sys.stdout)
        return

    with open(args.candidates) as f:
        candidates: List[Dict[str, Any]] = json.load(f)

    enriched: List[Dict[str, Any]] = []
    for i, cand in enumerate(candidates, 1):
        domain = cand.get("shop_domain") or ""
        if not domain:
            cand["ads"], cand["ads_count"], cand["ads_error"] = [], 0, "no domain"
            enriched.append(cand)
            continue
        print(f"[enrich_api] ({i}/{len(candidates)}) enriching {domain}...", file=sys.stderr)
        try:
            result = enrich_shop(domain, api_key, args.limit)
            cand["ads"] = result["ads"]
            cand["ads_count"] = result["ads_count"]
            cand["ads_error"] = ""
        except Exception as e:
            print(f"[enrich_api] ERROR {domain}: {e}", file=sys.stderr)
            cand["ads"], cand["ads_count"], cand["ads_error"] = [], 0, str(e)
        enriched.append(cand)

    json.dump(enriched, sys.stdout, indent=2, ensure_ascii=False)
    print(file=sys.stdout)


if __name__ == "__main__":
    main()
