#!/usr/bin/env python3
"""
query_ads_api.py — Batch TrendTrack Ad Search via the PUBLIC REST API.

Calls POST https://api.trendtrack.io/v1/ads/query directly (deterministic,
no MCP layer), running the 3 user-specified search methods verbatim.
Merges results, deduplicates by shop domain, and outputs a clean JSON
array to stdout (same normalized shape as query_ads.py).

Usage:
    python3 query_ads_api.py --date 2026-10-08            # all 3 methods
    python3 query_ads_api.py --date 2026-10-08 --method 1 # method 1 only (test mode)

API key: read from ~/.hermes/mcp-tokens/trendtrack_rest_key.txt
(one-time key created from the dashboard Settings → API; never print it).

Cost: ~30 credits per method (same backend as search_ads MCP).

Methods (verbatim from user spec, 2026-10-08):

  Méthode 1 — shopify_reach_growth:
    - created: today - 3 months .. today
    - technologies: shopify
    - countries: FR, DE, NL
    - languages: fr, de, nl
    - reach: min 36k, max 10M (total)
    - reach growth: >= 135% (last30d — public API accepts last30d, unlike MCP)
    - status: active
    - sortBy: reachDelta30d desc

  Méthode 2 — native_ads:
    - status: active
    - minDaysRunning: 20
    - mediaType: image
    - countries: FR, DE, NL
    - languages: fr, de, nl
    - adCopyLength min 1000 (minDescriptionLength)
    - sortBy: adOrder desc (= "Ads rank" descendant)

  Méthode 3 — volume_live:
    - exclude countries: IN, PK, US (public API accepts exclude, unlike MCP)
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
# OAuth token (fallback auth + refresh source for nothing here; REST key is primary)
TOKEN_PATH = HERMES_HOME / "mcp-tokens" / "trendtrack.json"


def load_api_key() -> str:
    if REST_KEY_PATH.exists():
        return REST_KEY_PATH.read_text().strip()
    raise SystemExit(f"API key not found at {REST_KEY_PATH}. Create it from the "
                     "TrendTrack dashboard Settings → API → Create key.")


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
def build_queries(ref_date: datetime) -> List[Dict[str, Any]]:
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
        "adLanguages": ["fr", "de", "nl"],
        # Reach au moins 36k à 10M+
        "minReach": 36000,
        "maxReach": 10000000,
        "reachPeriod": "total",
        # Évolution du reach: 135% à 1000%+
        # NOTE: le contrat public exige {anyOf:[{all:[{operator, value, period}]}]}
        # et le backend ne supporte que period=last7d (last30d rejeté
        # « not representable; use last7d » — limite serveur, 2026-10-08).
        "adReachGrowth": {"anyOf": [{"all": [{"operator": "gte", "value": 135, "period": "last7d"}]}]},
        # Pubs actives
        "status": "active",
        # Sort by évolution du reach
        # NOTE: sortBy=reachDelta30d rejeté par le backend (« not representable on
        # ads-search ») → équivalent le plus proche : reachDelta7d desc.
        "sortBy": "reachDelta7d",
        "order": "desc",
        "limit": 20,
    }

    method2 = {
        "_tag": "native_ads",
        # Pubs actives
        "status": "active",
        # Days running minimum 20
        "minDaysRunning": 20,
        # Media type: Image
        "mediaType": "image",
        # Pays: France Allemagne Pays-bas
        "adCountries": {"include": ["FR", "DE", "NL"]},
        # Langue: Français Allemand Néerlandais
        "adLanguages": ["fr", "de", "nl"],
        # Ads copy length 1000 minimum
        "minDescriptionLength": 1000,
        # Sort by Ads rank descendant
        "sortBy": "adOrder",
        "order": "desc",
        "limit": 20,
    }

    method3 = {
        "_tag": "volume_live",
        # Exclure les pays: Inde, Pakistan, US
        # NOTE: adCountries.exclude est rejeté par le backend (« not representable
        # on ads-search »). Équivalent supporté le plus proche (2026-10-08) :
        # exclure les shops originaires d'IN/PK/US via shopCreationCountries.exclude.
        "shopCreationCountries": {"exclude": ["IN", "PK", "US"]},
        # Last 30 days
        "createdAfter": j_30,
        # Live ads: 50 ads last 24 hours
        "minActiveAds": 50,
        "adsTimePeriod": "last24h",
        "status": "active",
        "limit": 20,
    }

    return [method1, method2, method3]


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


def extract_ad_fields(ad: Dict[str, Any], method_tag: str) -> Optional[Dict[str, Any]]:
    if not isinstance(ad, dict):
        return None
    content = ad.get("content") or {}
    advertiser = ad.get("advertiser") or {}
    metrics = ad.get("metrics") or {}
    media = ad.get("media") or {}
    audience = ad.get("audience") or {}
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
        "image_url": media.get("mediaUrl") or media.get("thumbnailUrl") or "",
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
        description="Batch TrendTrack ad search via public REST API — 3 methods verbatim."
    )
    parser.add_argument("--date", "-d", default=datetime.now().strftime("%Y-%m-%d"),
                        help="Reference date YYYY-MM-DD (default: today).")
    parser.add_argument("--method", "-m", type=int, choices=[1, 2, 3], default=None,
                        help="Run only this method (1, 2 or 3) — for cheap test runs.")
    args = parser.parse_args()

    ref_date = datetime.strptime(args.date, "%Y-%m-%d")
    api_key = load_api_key()

    queries = build_queries(ref_date)
    if args.method:
        queries = [q for q in queries if q["_tag"] in {
            1: "shopify_reach_growth", 2: "native_ads", 3: "volume_live"}[args.method]]

    all_ads: List[Dict[str, Any]] = []
    for query in queries:
        method_tag = query.pop("_tag")
        print(f"[query_ads_api] Running method: {method_tag}...", file=sys.stderr)
        try:
            resp = api_query(query, api_key)
            raw_ads = resp.get("data") or []
            print(f"[query_ads_api]   → {len(raw_ads)} raw results "
                  f"(total {resp.get('pagination', {}).get('total', '?')})", file=sys.stderr)
            for raw_ad in raw_ads:
                parsed = extract_ad_fields(raw_ad, method_tag)
                if parsed and parsed["shop_domain"]:
                    all_ads.append(parsed)
        except Exception as e:
            print(f"[query_ads_api] ERROR in {method_tag}: {e}", file=sys.stderr)

    print(f"[query_ads_api] Total raw results: {len(all_ads)}", file=sys.stderr)

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
