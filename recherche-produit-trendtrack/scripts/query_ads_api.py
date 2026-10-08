#!/usr/bin/env python3
"""
query_ads_api.py — Batch TrendTrack Ad Search via the PUBLIC REST API.

Calls POST https://api.trendtrack.io/v1/ads/query directly (deterministic,
no MCP layer), running the 2 user-specified search methods across multiple pages.
Merges results, deduplicates by shop domain, and outputs a clean JSON
array to stdout (same normalized shape as query_ads.py).

Usage:
    python3 query_ads_api.py --date 2026-10-08            # 2 methods, 5 pages each (limit 100/page)
    python3 query_ads_api.py --date 2026-10-08 --pages 2  # custom page count (2 pages/method)
    python3 query_ads_api.py --date 2026-10-08 --method 1 # method 1 only (test mode)
    python3 query_ads_api.py --limit 50                   # custom limit per page (default: 100)

API key: read from ~/.hermes/mcp-tokens/trendtrack_rest_key.txt
(one-time key created from the dashboard Settings → API; never print it).

Cost:
    ~30 credits per query call.
    Default (5 pages × 2 methods = 10 calls): ~300 credits for search.

Methods (user spec, 2026-10-08):

  Méthode 1 — shopify_reach_growth:
    - created: today - 3 months .. today
    - technologies: shopify
    - countries: FR, DE, NL
    - languages: fr, de, nl
    - reach: min 36k, no upper cap (covers 10M+)
    - reach growth: >= 135% (last7d backend window)
    - status: active
    - sortBy: reachDelta7d desc

  Méthode 2 — volume_live:
    - exclude countries: IN, PK, US
    - createdAfter: today - 30 days
    - minActiveAds: 50, adsTimePeriod: last24h
    - status: active
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
REST_KEY_PATH = HERMES_HOME / "mcp-tokens" / "trendtrack_rest_key.txt"
ADS_QUERY_URL = "https://api.trendtrack.io/v1/ads/query"
USAGE_URL = "https://api.trendtrack.io/v1/usage"
TOKEN_PATH = HERMES_HOME / "mcp-tokens" / "trendtrack.json"

DEFAULT_PAGES = 5
MIN_CREDITS_THRESHOLD = 300


def load_api_key() -> str:
    if REST_KEY_PATH.exists():
        return REST_KEY_PATH.read_text().strip()
    raise SystemExit(f"API key not found at {REST_KEY_PATH}. Create it from the "
                     "TrendTrack dashboard Settings → API → Create key.")


def check_credits(api_key: str, min_credits: int = MIN_CREDITS_THRESHOLD) -> int:
    """Check remaining TrendTrack credits. Aborts if below min_credits."""
    req = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
            credits_info = data.get("credits") or {}
            quota = data.get("includedQuota") or {}
            remaining = credits_info.get("totalRemaining")
            if remaining is None:
                remaining = quota.get("remaining", 0)
            print(f"[query_ads_api] TrendTrack balance: {remaining} credits remaining", file=sys.stderr)
            if remaining < min_credits:
                raise SystemExit(
                    f"[query_ads_api] ERROR: Low TrendTrack credits ({remaining} < {min_credits}). "
                    f"Aborting run to protect credit budget."
                )
            return int(remaining)
    except SystemExit:
        raise
    except Exception as e:
        print(f"[query_ads_api] Warning: could not check credits balance ({e}), proceeding...", file=sys.stderr)
        return -1


def api_query(payload: Dict[str, Any], api_key: str, retries: int = 3) -> Dict[str, Any]:
    """POST /v1/ads/query with Bearer API key. Deterministic; retries on 5xx only."""
    body = json.dumps(payload).encode()
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(
            ADS_QUERY_URL, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:300]
            if e.code >= 500 and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code} on /v1/ads/query: {err_body}")
        except Exception as e:  # network
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"network error: {last_err}")
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# Query definitions — VERBATIM user spec (public camelCase contract)
# ---------------------------------------------------------------------------
def build_queries(ref_date: datetime, limit: int = 100) -> List[Dict[str, Any]]:
    j = ref_date.strftime("%Y-%m-%d")
    j_90 = (ref_date - timedelta(days=90)).strftime("%Y-%m-%d")
    j_30 = (ref_date - timedelta(days=30)).strftime("%Y-%m-%d")

    method1 = {
        "_tag": "shopify_reach_growth",
        # D'aujourd'hui à 3 mois avant
        "createdAfter": j_90,
        "createdBefore": j,
        # Shopify
        "technologies": ["shopify"],
        # Pays: France Allemagne Pays-bas
        "adCountries": {"include": ["FR", "DE", "NL"]},
        # Langue: Français Allemand Néerlandais
        "adLanguage": ["fr", "de", "nl"],
        # Reach au moins 36k à 10M+ (sans cap supérieur pour inclure les gagnants > 10M)
        "minReach": 36000,
        "reachPeriod": "total",
        # Évolution du reach: 135% à 1000%+
        # Backend supporte period=last7d
        "adReachGrowth": {"anyOf": [{"all": [{"operator": "gte", "value": 135, "period": "last7d"}]}]},
        # Pubs actives
        "status": "active",
        # Sort by évolution du reach
        "sortBy": "reachDelta7d",
        "order": "desc",
        "limit": limit,
    }

    method2 = {
        "_tag": "volume_live",
        # Exclure les pays: Inde, Pakistan, US
        "creationCountry": {"exclude": ["IN", "PK", "US"]},
        # Last 30 days
        "createdAfter": j_30,
        # Live ads: 50 ads last 24 hours
        "minActiveAds": 50,
        "adsTimePeriod": "last24h",
        "status": "active",
        "limit": limit,
    }

    return [method1, method2]


# ---------------------------------------------------------------------------
# Normalization (same output shape as query_ads.py structuredContent parsing)
# ---------------------------------------------------------------------------
def normalize_domain(url: str) -> str:
    if not url:
        return ""
    url = url.lower().strip()
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    if url.startswith("www."):
        url = url[4:]
    return url.split("/")[0].split("?")[0].split("#")[0].rstrip(".")


def extract_image_url(media: Dict[str, Any]) -> str:
    """Extract an image URL from the media object.
    If media type is 'image', use mediaUrl (if present), falling back to thumbnailUrl.
    If media type is not 'image' (e.g. 'video'), use thumbnailUrl (if present),
    falling back to mediaUrl.
    """
    if not isinstance(media, dict):
        return ""
    media_type = (media.get("type") or "").lower()
    media_url = media.get("mediaUrl") or ""
    thumb_url = media.get("thumbnailUrl") or ""

    is_video_ext = any(media_url.lower().split("?")[0].endswith(ext) for ext in [".mp4", ".mov", ".webm", ".m4v"])
    if media_type == "image" and not is_video_ext:
        return media_url or thumb_url
    return thumb_url or media_url


def extract_ad_fields(ad: Dict[str, Any], method_tag: str) -> Optional[Dict[str, Any]]:
    if not isinstance(ad, dict):
        return None
    content = ad.get("content") or {}
    advertiser = ad.get("advertiser") or {}
    metrics = ad.get("metrics") or {}
    media = ad.get("media") or {}
    audience = ad.get("audience") or {}

    # Client-side safeguard for Method 2: exclude IN, PK, US
    if method_tag == "volume_live":
        targeted = [c.upper() for c in (audience.get("targetedCountries") or [])]
        main_c = (audience.get("mainCountry") or "").upper()
        creation_c = (advertiser.get("creationCountry") or "").upper()
        if creation_c in {"IN", "PK", "US"} or main_c in {"IN", "PK", "US"}:
            return None
        if targeted and all(c in {"IN", "PK", "US"} for c in targeted):
            return None

    landing_url = content.get("landingPageUrl") or ""
    shop_domain = content.get("landingPageDomain") or normalize_domain(landing_url)
    body = content.get("body") or ""
    ad_id = ad.get("id") or ""
    return {
        "ad_id": str(ad_id),
        "collation_id": ad.get("collationId") or "",
        "trendtrack_url": f"https://app.trendtrack.io/ads/{str(ad_id).replace('facebook_', '', 1)}" if ad_id else "",
        "shop_domain": normalize_domain(shop_domain) if shop_domain else "",
        "landing_url": landing_url,
        "title": content.get("title") or advertiser.get("name") or "",
        "description": body[:500] if body else "",
        "image_url": extract_image_url(media),
        "reach_total": metrics.get("reach") or 0,
        "reach_delta_30d": metrics.get("reachDelta30d") or 0,
        "reach_delta_7d": metrics.get("reachDelta7d") or 0,
        "active_ads": advertiser.get("liveAdsCount") or 0,
        "days_running": ad.get("daysRunning") or 0,
        "country_breakdown": {
            "targeted": audience.get("targetedCountries") or [],
            "main": audience.get("mainCountry") or "",
        },
        "media_type": media.get("type") or "",
        "method": method_tag,
        "first_seen": ad.get("firstSeenAt") or "",
        "last_seen": ad.get("lastSeenAt") or "",
        "advertiser_name": advertiser.get("name") or "",
        "estimated_spend": metrics.get("estimatedSpend") or 0,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Batch TrendTrack ad search via public REST API — 2 methods across multiple pages."
    )
    parser.add_argument("--date", "-d", default=datetime.now().strftime("%Y-%m-%d"),
                        help="Reference date YYYY-MM-DD (default: today).")
    parser.add_argument("--pages", "-p", type=int, default=DEFAULT_PAGES,
                        help=f"Number of pages to search per method (default: {DEFAULT_PAGES}).")
    parser.add_argument("--method", "-m", type=int, choices=[1, 2], default=None,
                        help="Run only this method (1 or 2) — for cheap test runs.")
    parser.add_argument("--limit", "-l", type=int, default=100,
                        help="Max ads per method query (default: 100, API max).")
    parser.add_argument("--min-credits", type=int, default=MIN_CREDITS_THRESHOLD,
                        help=f"Minimum credits threshold required before searching (default: {MIN_CREDITS_THRESHOLD}).")
    parser.add_argument("--skip-credit-check", action="store_true",
                        help="Skip pre-run TrendTrack credit balance verification.")
    args = parser.parse_args()

    ref_date = datetime.strptime(args.date, "%Y-%m-%d")
    api_key = load_api_key()

    # Pre-check credit balance before executing queries
    if not args.skip_credit_check:
        check_credits(api_key, min_credits=args.min_credits)

    queries = build_queries(ref_date, limit=args.limit)
    if args.method:
        queries = [q for q in queries if q["_tag"] in {
            1: "shopify_reach_growth", 2: "volume_live"}[args.method]]

    estimated_credits = len(queries) * args.pages * 30
    print(f"[query_ads_api] Starting search: {len(queries)} method(s), up to {args.pages} page(s) each "
          f"(estimated search cost: ~{estimated_credits} credits)", file=sys.stderr)

    all_ads: List[Dict[str, Any]] = []

    for query_template in queries:
        method_tag = query_template.pop("_tag")
        print(f"[query_ads_api] Running method: {method_tag} (up to {args.pages} pages)...", file=sys.stderr)

        for page_num in range(1, args.pages + 1):
            query = dict(query_template)
            query["page"] = page_num
            print(f"[query_ads_api]   Fetching {method_tag} page {page_num}/{args.pages}...", file=sys.stderr)

            try:
                resp = api_query(query, api_key)
                raw_ads = resp.get("data") or []
                pagination = resp.get("pagination") or {}
                total_items = pagination.get("total", "?")
                total_pages = pagination.get("totalPages")

                page_matched = 0
                for raw_ad in raw_ads:
                    parsed = extract_ad_fields(raw_ad, method_tag)
                    if parsed and parsed["shop_domain"]:
                        all_ads.append(parsed)
                        page_matched += 1

                print(f"[query_ads_api]     → page {page_num}: {len(raw_ads)} ads received, "
                      f"{page_matched} valid shops (total matching: {total_items}, totalPages: {total_pages})",
                      file=sys.stderr)

                # Stop early if no results or reached totalPages
                if not raw_ads:
                    print(f"[query_ads_api]     → No more ads on page {page_num}, stopping pagination for {method_tag}.",
                          file=sys.stderr)
                    break
                if total_pages is not None and page_num >= total_pages:
                    print(f"[query_ads_api]     → Reached last page ({page_num}/{total_pages}) for {method_tag}.",
                          file=sys.stderr)
                    break

                if page_num < args.pages:
                    time.sleep(0.5)

            except Exception as e:
                print(f"[query_ads_api] ERROR in {method_tag} page {page_num}: {e}", file=sys.stderr)
                break

    print(f"[query_ads_api] Total raw collected candidates: {len(all_ads)}", file=sys.stderr)

    # Dedup by shop domain — keep highest reach entry, merge method tags
    seen: Dict[str, Dict[str, Any]] = {}
    for ad in all_ads:
        domain = ad["shop_domain"]
        if not domain:
            continue
        existing = seen.get(domain)
        if existing is None:
            seen[domain] = ad
        else:
            if ad.get("reach_total", 0) > existing.get("reach_total", 0):
                ad["method"] = f"{existing['method']},{ad['method']}"
                seen[domain] = ad
            else:
                existing["method"] = f"{existing['method']},{ad['method']}"

    deduped = list(seen.values())
    print(f"[query_ads_api] After dedup: {len(deduped)} unique shops", file=sys.stderr)
    deduped.sort(key=lambda x: x.get("reach_total", 0), reverse=True)

    json.dump(deduped, sys.stdout, indent=2, ensure_ascii=False)
    print(file=sys.stdout)


if __name__ == "__main__":
    main()
