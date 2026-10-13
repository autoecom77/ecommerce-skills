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
  → ads = rows back, each with trendtrack_url = https://app.trendtrack.io/en/ju-2/explorer?tab=ads&ad=facebook_<id>&adSource=meta
    (this is the TrendTrack explorer ad link — user workspace ju-2).

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
from query_ads_api import api_query, load_api_key, extract_image_url, request_cost  # noqa: E402


def format_trendtrack_ad_url(ad_id: str, ad_source: str = "meta") -> str:
    """Format TrendTrack explorer ad URL.
    Format: https://app.trendtrack.io/en/ju-2/explorer?tab=ads&ad=facebook_<id>&adSource=meta
    """
    if not ad_id:
        return ""
    clean_id = str(ad_id).strip()
    full_id = clean_id if clean_id.startswith("facebook_") else f"facebook_{clean_id}"
    return f"https://app.trendtrack.io/en/ju-2/explorer?tab=ads&ad={full_id}&adSource={ad_source}"


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
    cost = request_cost(resp)
    rows = resp.get("data") or []
    ads = []
    for ad in rows:
        content = ad.get("content") or {}
        metrics = ad.get("metrics") or {}
        media = ad.get("media") or {}
        ad_id = str(ad.get("id") or "")
        ads.append({
            "ad_id": ad_id,
            "trendtrack_url": format_trendtrack_ad_url(ad_id, "meta"),
            "reach": metrics.get("reach") or 0,
            "estimated_spend": metrics.get("estimatedSpend") or 0,
            "first_seen": (ad.get("firstSeenAt") or "")[:10],
            "last_seen": (ad.get("lastSeenAt") or "")[:10],
            "days_running": ad.get("daysRunning") or 0,
            "image_url": extract_image_url(media),
            "media_url": media.get("mediaUrl") or media.get("thumbnailUrl") or "",
            "media_type": media.get("type") or "",
            "landing_url": content.get("landingPageUrl") or "",
            "body": (content.get("body") or "")[:200],
        })
    return {"domain": domain, "ads": ads, "ads_count": len(ads), "error": "", "credits": cost}


def main() -> None:
    parser = argparse.ArgumentParser(description="TrendTrack enrichment via public REST API (ads only).")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--candidates", "-c", help="JSON array of candidates with shop_domain.")
    src.add_argument("--shop", "-s", help="Single shop domain to enrich (test mode).")
    parser.add_argument("--limit", type=int, default=8,
                        help="Max ads per shop (default 8 = the AD1-AD8 Sheet blocks; billing is per returned ad).")
    parser.add_argument("--max-credits", type=int, default=0,
                        help="Credit budget for the enrichment (0 = unlimited). Shops beyond it keep only their search ad.")
    parser.add_argument("--stats-out", default="/tmp/tt_enrich_stats.json", help="Where to write enrichment stats.")
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
    spent, max_cost, n_ok, n_skipped, n_err = 0, 0, 0, 0, 0
    est_cost = int(round(args.limit * 1.5))
    for i, cand in enumerate(candidates, 1):
        domain = cand.get("shop_domain") or ""
        if not domain:
            cand["ads"], cand["ads_count"], cand["ads_error"] = [], 0, "no domain"
            enriched.append(cand)
            continue
        if args.max_credits and spent + (max_cost or est_cost) > args.max_credits:
            cand["ads"], cand["ads_count"], cand["ads_error"] = [], 0, "credit budget reached"
            n_skipped += 1
            enriched.append(cand)
            continue
        print(f"[enrich_api] ({i}/{len(candidates)}) enriching {domain}...", file=sys.stderr)
        try:
            result = enrich_shop(domain, api_key, args.limit)
            cand["ads"] = result["ads"]
            cand["ads_count"] = result["ads_count"]
            cand["ads_error"] = ""
            spent += result.get("credits", 0)
            max_cost = max(max_cost, result.get("credits", 0))
            n_ok += 1
        except Exception as e:
            print(f"[enrich_api] ERROR {domain}: {e}", file=sys.stderr)
            cand["ads"], cand["ads_count"], cand["ads_error"] = [], 0, str(e)
            n_err += 1
        enriched.append(cand)

    if n_skipped:
        print(f"[enrich_api] Credit budget {args.max_credits} reached: {n_skipped} shop(s) keep only their search ad.",
              file=sys.stderr)
    print(f"[enrich_api] Done: {n_ok} enriched, {n_err} errors, {n_skipped} skipped — {spent} credits", file=sys.stderr)
    if args.stats_out:
        try:
            with open(args.stats_out, "w", encoding="utf-8") as f:
                json.dump({"shops": len(candidates), "enriched": n_ok, "errors": n_err, "skipped_budget": n_skipped,
                           "credits_spent": spent, "budget": args.max_credits, "limit": args.limit}, f, indent=2)
        except Exception as e:
            print(f"[enrich_api] Warning: could not write stats ({e})", file=sys.stderr)

    json.dump(enriched, sys.stdout, indent=2, ensure_ascii=False)
    print(file=sys.stdout)


if __name__ == "__main__":
    main()
