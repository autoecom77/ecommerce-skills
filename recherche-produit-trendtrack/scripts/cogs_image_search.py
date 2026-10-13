#!/usr/bin/env python3
"""
cogs_image_search.py — COGS Product Image Search

Finds the manufacturing cost (COGS) of a product by searching Chinese
sourcing platforms using the product's image.

Primary method: Apify 1688 Image Search API
Fallback method: AliExpress Image Search (aliexpress_image_search.py)

Usage:
    python3 cogs_image_search.py \\
      --image-url "https://cdn.shopify.com/.../product.jpg" \\
      --product-name "Knee Pillow"

    python3 cogs_image_search.py \\
      --image-path /tmp/product.jpg \\
      --product-name "Knee Pillow"

Output (JSON to stdout):
    {
      "method": "apify_1688|aliexpress_image|not_found",
      "source": "1688|aliexpress",
      "cogs_eur": 3.65,
      "cogs_original": 28.5,
      "cogs_currency": "CNY",
      "product_url": "https://detail.1688.com/offer/...",
      "confidence": "high|medium|low",
      "matches": [...]
    }
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
APIFY_TOKEN_PATH = HERMES_HOME / "apify_token"
SCRIPT_DIR = Path(__file__).resolve().parent
ALIEXPRESS_SCRIPT = SCRIPT_DIR / "aliexpress_image_search.py"

# Conversion rates (fixed estimates — good enough for COGS screening)
CONVERSION_RATES = {
    "CNY": 0.128,   # ¥1 ≈ €0.128
    "USD": 0.92,
    "EUR": 1.0,
    "GBP": 1.16,
}

# Reject an estimate when Prix / COGS exceeds this: such a hit is almost surely a spare
# part, a sample or a ¥1 accessory listing, not the product (2026-10-13 run: 0.11 € for a
# 1 999 € e-bike, 0.13 € for a 30.99 € advent calendar). Highest X among the curated
# Sheet rows is 28.3.
MAX_PLAUSIBLE_X = 30.0

# Apify run lifecycle. Runs are started with ?timeout=&memory= so they stop by themselves:
# a run we stop polling otherwise keeps running (actor defaults: 2400 s / 3600 s) and holds
# one of the 5 concurrent-run slots of the free plan → every new run gets HTTP 402.
APIFY_IMAGE_RUN_TIMEOUT_S = 100
APIFY_KEYWORD_RUN_TIMEOUT_S = 75
LAST_ERROR: Dict[str, str] = {}  # method -> last infrastructure error (shown in attempts)


def _start_apify_run(api_url: str, payload: bytes, headers: Dict[str, str], label: str) -> Optional[Dict[str, Any]]:
    """POST an actor run; one retry after 20 s on HTTP 402 (concurrent-run limit)."""
    for attempt in range(2):
        req = urllib.request.Request(api_url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 402 and attempt == 0:
                print(f"[cogs] {label}: Apify HTTP 402 (concurrent-run/credit limit) — retry in 20s", file=sys.stderr)
                time.sleep(20)
                continue
            LAST_ERROR[label] = f"Apify HTTP {e.code}"
            print(f"[cogs] {label} run start failed: HTTP {e.code}", file=sys.stderr)
            return None
        except Exception as e:
            LAST_ERROR[label] = f"Apify start error: {str(e)[:60]}"
            print(f"[cogs] {label} run start failed: {e}", file=sys.stderr)
            return None
    return None


def _abort_apify_run(run_id: str, apify_token: str) -> None:
    """Free the concurrent-run slot of a run we stopped waiting for."""
    try:
        req = urllib.request.Request(f"https://api.apify.com/v2/actor-runs/{run_id}/abort",
                                     headers={"Authorization": f"Bearer {apify_token}"}, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        pass


def _median(vals: List[float]) -> Optional[float]:
    s = sorted(vals)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def robust_estimate(
    result: Optional[Dict[str, Any]],
    price_eur: Optional[float],
) -> Tuple[Optional[Dict[str, Any]], str]:
    """COGS = median of the priced matches (not the cheapest), then a plausibility check.

    Returns (result or None, rejection reason).
    """
    if not result:
        return None, "no match"
    priced = [m for m in (result.get("matches") or [])
              if isinstance(m.get("price_eur"), (int, float)) and m["price_eur"] > 0]
    if not priced:
        return None, "no priced match"
    med = _median([m["price_eur"] for m in priced])
    ref = min(priced, key=lambda m: abs(m["price_eur"] - med))
    orig_key = "price_cny" if result.get("cogs_currency") == "CNY" else "price_original"
    origs = [m.get(orig_key) for m in priced if isinstance(m.get(orig_key), (int, float))]
    result.update({
        "cogs_eur": round(med, 2),
        "cogs_min_eur": round(min(m["price_eur"] for m in priced), 2),
        "cogs_original": round(_median(origs), 2) if origs else result.get("cogs_original"),
        "product_url": ref.get("url") or result.get("product_url"),
        "estimator": f"median of {len(priced)} match(es)",
    })
    if price_eur and med > 0 and price_eur / med > MAX_PLAUSIBLE_X:
        return None, f"implausible: X={price_eur / med:.0f} > {MAX_PLAUSIBLE_X:.0f} (median {med:.2f}€)"
    return result, ""


# ---------------------------------------------------------------------------
# Apify 1688 Image Search
# ---------------------------------------------------------------------------
def load_apify_token() -> Optional[str]:
    """Load Apify API token from environment, ~/.hermes/apify_token, or ~/.hermes/.env."""
    # 1. Check environment variables
    token = os.environ.get("APIFY_TOKEN", "").strip() or os.environ.get("APIFY_API_TOKEN", "").strip()
    if token:
        return token

    # 2. Check dedicated token file (~/.hermes/apify_token)
    if APIFY_TOKEN_PATH.exists():
        file_token = APIFY_TOKEN_PATH.read_text().strip()
        if file_token:
            return file_token

    # 3. Check ~/.hermes/.env
    env_file = HERMES_HOME / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("#"):
                    continue
                if line.startswith("APIFY_TOKEN=") or line.startswith("APIFY_API_TOKEN="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
        except Exception:
            pass

    return None


def download_image_to_temp(image_url: str) -> str:
    """Download image URL to a temporary file, return path."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    req = urllib.request.Request(image_url, headers=headers)
    suffix = ".jpg"
    clean_url = image_url.split("?")[0].lower()
    for ext in [".png", ".webp", ".jpeg", ".jpg"]:
        if clean_url.endswith(ext):
            suffix = ext
            break

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            tmp.write(resp.read())
        tmp.flush()
        return tmp.name
    finally:
        tmp.close()


def search_apify_1688(
    image_url: Optional[str] = None,
    image_path: Optional[str] = None,
    apify_token: str = "",
    max_results: int = 5,
) -> Optional[Dict[str, Any]]:
    """Search 1688 for product matches using Apify crawleast/1688-image-search-scraper actor.

    Supports image URL or local image path (encoded as base64).
    Returns structured result dict or None if search fails/no results.
    """
    if not image_url and not image_path:
        return None

    print("[cogs] Trying Apify 1688 Image Search (crawleast/1688-image-search-scraper)...", file=sys.stderr)

    actor_id = "crawleast~1688-image-search-scraper"
    api_url = f"https://api.apify.com/v2/acts/{actor_id}/runs?timeout={APIFY_IMAGE_RUN_TIMEOUT_S}&memory=1024"

    payload_data: Dict[str, Any] = {
        "maxImages": 1,
        "maxResultsPerImage": max_results,
        "enrichDetails": False,
    }

    if image_url:
        payload_data["imageUrls"] = [image_url]
    elif image_path:
        try:
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")
            payload_data["imagesBase64"] = [img_b64]
        except Exception as e:
            print(f"[cogs] Failed to read local image for base64: {e}", file=sys.stderr)
            return None

    payload = json.dumps(payload_data).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {apify_token}",
    }

    run_data = _start_apify_run(api_url, payload, headers, "apify_1688")
    if run_data is None:
        return None

    run_id = run_data.get("data", {}).get("id")
    if not run_id:
        print("[cogs] Apify: no run ID returned", file=sys.stderr)
        return None

    # Poll for completion (max 90 seconds)
    dataset_id = None
    for _ in range(18):  # 18 × 5s = 90s
        time.sleep(5)
        status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={apify_token}"
        try:
            with urllib.request.urlopen(status_url, timeout=10) as resp:
                status_data = json.loads(resp.read().decode())
            status = status_data.get("data", {}).get("status", "")
            if status == "SUCCEEDED":
                dataset_id = status_data["data"].get("defaultDatasetId")
                break
            elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                print(f"[cogs] Apify run {status}", file=sys.stderr)
                return None
        except Exception as e:
            print(f"[cogs] Apify poll error: {e}", file=sys.stderr)

    if not dataset_id:
        print("[cogs] Apify: run did not complete in time", file=sys.stderr)
        LAST_ERROR["apify_1688"] = "Apify run timeout"
        _abort_apify_run(run_id, apify_token)
        return None

    # Fetch results from dataset
    results_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={apify_token}"
    try:
        with urllib.request.urlopen(results_url, timeout=15) as resp:
            dataset_rows = json.loads(resp.read().decode())
    except Exception as e:
        print(f"[cogs] Apify results fetch failed: {e}", file=sys.stderr)
        return None

    if not dataset_rows:
        print("[cogs] Apify: dataset is empty", file=sys.stderr)
        return None

    # crawleast actor writes imageResult rows with results array
    raw_items: List[Dict[str, Any]] = []
    for row in dataset_rows:
        if isinstance(row, dict):
            if row.get("type") == "imageResult" and "results" in row:
                raw_items.extend(row.get("results") or [])
            elif "results" in row and isinstance(row["results"], list):
                raw_items.extend(row["results"])
            else:
                raw_items.append(row)

    if not raw_items:
        print("[cogs] Apify: no matching products found on 1688", file=sys.stderr)
        return None

    # Parse results
    matches = []
    for item in raw_items[:max_results]:
        # Handle various price fields from crawleast actor
        price_cny = None
        for key in ["priceYuan", "consignPriceYuan", "price", "unitPrice"]:
            val = item.get(key)
            if val is not None:
                if isinstance(val, (int, float)):
                    price_cny = float(val)
                    break
                elif isinstance(val, dict):
                    # Enriched detail price dict: {"min": 1.9, "max": 2.2, ...}
                    min_p = val.get("min") or val.get("price")
                    if min_p is not None:
                        try:
                            price_cny = float(min_p)
                            break
                        except (ValueError, TypeError):
                            pass
                elif isinstance(val, str):
                    try:
                        price_cny = float(val.replace(",", "").replace("¥", "").strip())
                        break
                    except ValueError:
                        pass

        price_eur = round(price_cny * CONVERSION_RATES["CNY"], 2) if price_cny is not None else None

        detail_url = item.get("detailUrl") or item.get("url") or ""
        title = item.get("title") or item.get("titleEn") or item.get("titleCn") or item.get("name") or ""
        img_url = item.get("imageUrl") or item.get("image") or ""
        moq = item.get("moq") or item.get("minOrder") or ""

        supplier_info = item.get("supplier")
        supplier_name = ""
        if isinstance(supplier_info, dict):
            supplier_name = supplier_info.get("name") or supplier_info.get("companyName") or ""
        elif isinstance(supplier_info, str):
            supplier_name = supplier_info

        matches.append({
            "url": detail_url,
            "price_cny": price_cny,
            "price_eur": price_eur,
            "title": title,
            "image_url": img_url,
            "min_order": moq,
            "supplier": supplier_name,
        })

    # Sort by price ascending (cheapest unit price)
    matches.sort(key=lambda x: x["price_eur"] if x["price_eur"] is not None else 999999)

    valid_matches = [m for m in matches if m["price_eur"] is not None]
    if not valid_matches:
        return None

    best = valid_matches[0]
    return {
        "method": "apify_1688",
        "source": "1688",
        "cogs_eur": best["price_eur"],
        "cogs_original": best["price_cny"],
        "cogs_currency": "CNY",
        "product_url": best["url"],
        "confidence": "high" if len(valid_matches) >= 3 else "medium",
        "matches": matches,
    }


# ---------------------------------------------------------------------------
# AliExpress Image Search (fallback)
# ---------------------------------------------------------------------------
def search_aliexpress_image(
    image_input: str,
    is_url: bool = True,
) -> Optional[Dict[str, Any]]:
    """Search AliExpress using the aliexpress_image_search.py script.

    This is the fallback method when Apify 1688 search fails or is unavailable.
    """
    print("[cogs] Falling back to AliExpress Image Search...", file=sys.stderr)

    if not ALIEXPRESS_SCRIPT.exists():
        print(f"[cogs] AliExpress script not found at {ALIEXPRESS_SCRIPT}", file=sys.stderr)
        return None

    # If it's a URL, download to temp file first (script accepts both)
    image_arg = image_input

    cmd = [
        sys.executable,
        str(ALIEXPRESS_SCRIPT),
        "--image", image_arg,
        "--currency", "EUR",
        "--country", "FR",
        "--all",
        "--limit", "5",
        "--json",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        print("[cogs] AliExpress search timed out", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[cogs] AliExpress search error: {e}", file=sys.stderr)
        return None

    if result.returncode != 0:
        stderr_msg = result.stderr.strip()[:200] if result.stderr else ""
        print(f"[cogs] AliExpress search failed (exit {result.returncode}): {stderr_msg}", file=sys.stderr)
        # Try to parse error from stdout (script outputs JSON errors)
        try:
            err_data = json.loads(result.stdout)
            if "error" in err_data:
                print(f"[cogs] AliExpress error: {err_data['error']}", file=sys.stderr)
        except (json.JSONDecodeError, KeyError):
            pass
        return None

    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("[cogs] AliExpress: could not parse JSON output", file=sys.stderr)
        return None

    if not items:
        print("[cogs] AliExpress: no matching products found", file=sys.stderr)
        return None

    # Handle single result (first_only mode) vs list
    if isinstance(items, dict):
        items = [items]

    # Convert to our standard format
    matches = []
    for item in items:
        price_eur = item.get("price")
        currency = item.get("currency", "EUR")

        # Convert if not EUR
        if currency != "EUR" and price_eur is not None:
            rate = CONVERSION_RATES.get(currency, 1.0)
            if currency == "CNY":
                price_eur = round(price_eur * CONVERSION_RATES["CNY"], 2)

        matches.append({
            "url": item.get("url", ""),
            "price_eur": price_eur,
            "price_original": item.get("price"),
            "currency": currency,
            "title": item.get("title", ""),
            "image_url": item.get("image_url", ""),
            "sales_count": item.get("sales_count", 0),
            "rating": item.get("rating"),
        })

    # Sort by price ascending
    matches.sort(key=lambda x: x["price_eur"] if x["price_eur"] is not None else 999999)

    if not matches or matches[0]["price_eur"] is None:
        return None

    best = matches[0]
    return {
        "method": "aliexpress_image",
        "source": "aliexpress",
        "cogs_eur": best["price_eur"],
        "cogs_original": best.get("price_original"),
        "cogs_currency": best.get("currency", "EUR"),
        "product_url": best["url"],
        "confidence": "medium" if len(matches) >= 2 else "low",
        "matches": matches,
    }


# ---------------------------------------------------------------------------
# 1688 Keyword Search (fallback 2)
# ---------------------------------------------------------------------------
def search_1688_keyword(
    keyword: str,
    apify_token: str,
    max_results: int = 5,
) -> Optional[Dict[str, Any]]:
    """Search 1688 products by keyword using Apify crawleast/1688-product-scraper actor."""
    if not keyword or not apify_token:
        return None

    actor_id = "crawleast~1688-product-scraper"
    api_url = f"https://api.apify.com/v2/acts/{actor_id}/runs?timeout={APIFY_KEYWORD_RUN_TIMEOUT_S}&memory=1024"

    payload_data = {
        "search": keyword,
        "maxItems": max_results,
    }
    payload = json.dumps(payload_data).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {apify_token}",
    }

    run_data = _start_apify_run(api_url, payload, headers, "apify_1688_keyword")
    if run_data is None:
        return None

    run_id = run_data.get("data", {}).get("id")
    if not run_id:
        return None

    dataset_id = None
    for _ in range(12):  # 12 × 5s = 60s
        time.sleep(5)
        status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={apify_token}"
        try:
            with urllib.request.urlopen(status_url, timeout=10) as resp:
                status_data = json.loads(resp.read().decode())
            status = status_data.get("data", {}).get("status", "")
            if status == "SUCCEEDED":
                dataset_id = status_data["data"].get("defaultDatasetId")
                break
            elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                return None
        except Exception:
            pass

    if not dataset_id:
        LAST_ERROR["apify_1688_keyword"] = "Apify run timeout"
        _abort_apify_run(run_id, apify_token)
        return None

    results_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={apify_token}"
    try:
        with urllib.request.urlopen(results_url, timeout=15) as resp:
            dataset_rows = json.loads(resp.read().decode())
    except Exception:
        return None

    if not dataset_rows:
        return None

    matches = []
    for item in dataset_rows[:max_results]:
        price_cny = item.get("priceYuan") or item.get("price") or item.get("consignPriceYuan")
        if price_cny is not None:
            try:
                price_cny = float(str(price_cny).replace("¥", "").replace(",", "").strip())
                price_eur = round(price_cny * CONVERSION_RATES["CNY"], 2)
            except ValueError:
                continue
            matches.append({
                "url": item.get("detailUrl") or item.get("url") or "",
                "price_cny": price_cny,
                "price_eur": price_eur,
                "title": item.get("title") or item.get("titleEn") or "",
                "image_url": item.get("imageUrl") or "",
            })

    if not matches:
        return None

    matches.sort(key=lambda x: x["price_eur"])
    best = matches[0]
    return {
        "method": "apify_1688_keyword",
        "source": "1688",
        "cogs_eur": best["price_eur"],
        "cogs_original": best["price_cny"],
        "cogs_currency": "CNY",
        "product_url": best["url"],
        "confidence": "medium",
        "matches": matches,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="COGS image search: Apify 1688 primary, AliExpress fallback."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--image-url", "-u",
        help="URL of the product image.",
    )
    group.add_argument(
        "--image-path", "-p",
        help="Local path to the product image.",
    )
    parser.add_argument(
        "--product-name", "-n",
        default="",
        help="Product name (for logging/context, not used in image search).",
    )
    parser.add_argument(
        "--apify-token",
        help="Apify API token (overrides ~/.hermes/apify_token).",
    )
    parser.add_argument(
        "--price-eur",
        type=float,
        default=None,
        help=f"Selling price in EUR: enables the plausibility check (reject Prix/COGS > {MAX_PLAUSIBLE_X:.0f}).",
    )
    args = parser.parse_args()

    image_input = args.image_url or args.image_path
    is_url = args.image_url is not None

    product_name = args.product_name
    if product_name:
        print(f"[cogs] Searching COGS for: {product_name}", file=sys.stderr)

    # Determine Apify token
    apify_token = args.apify_token or load_apify_token()
    attempts: List[Dict[str, str]] = []

    def accept(raw: Optional[Dict[str, Any]], method: str) -> bool:
        """Median + plausibility on one method's matches; print the JSON result if accepted."""
        res, why = robust_estimate(raw, args.price_eur)
        if raw is None and LAST_ERROR.get(method):
            why = LAST_ERROR[method]
        attempts.append({"method": method, "outcome": "ok" if res else why})
        if not res:
            print(f"[cogs] ✗ {method}: {why}", file=sys.stderr)
            return False
        res["attempts"] = attempts
        print(f"[cogs] ✓ Found via {method}: {res['cogs_eur']}€ ({res['estimator']})", file=sys.stderr)
        json.dump(res, sys.stdout, indent=2, ensure_ascii=False)
        print(file=sys.stdout)
        return True

    # ---- Method 1: Apify 1688 Image Search ----
    if apify_token and image_input:
        raw = None
        try:
            raw = search_apify_1688(
                image_url=args.image_url,
                image_path=args.image_path,
                apify_token=apify_token,
            )
        except Exception as e:
            print(f"[cogs] Apify error: {e}", file=sys.stderr)
        if accept(raw, "apify_1688"):
            return

    # ---- Method 2: AliExpress Image Search (fallback) ----
    # For URL images, download to temp file first for aliexpress script
    temp_path = None
    raw = None
    try:
        if is_url:
            print("[cogs] Downloading image for AliExpress search...", file=sys.stderr)
            temp_path = download_image_to_temp(args.image_url)
            ali_input = temp_path
        else:
            ali_input = args.image_path

        raw = search_aliexpress_image(ali_input, is_url=False)
    except Exception as e:
        print(f"[cogs] AliExpress error: {e}", file=sys.stderr)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
    if accept(raw, "aliexpress_image"):
        return

    # ---- Method 3: 1688 Keyword Search (fallback 2) ----
    if product_name and apify_token:
        raw = None
        try:
            print(f"[cogs] Falling back to 1688 Keyword Search for: {product_name}...", file=sys.stderr)
            raw = search_1688_keyword(product_name, apify_token)
        except Exception as e:
            print(f"[cogs] 1688 Keyword search error: {e}", file=sys.stderr)
        if accept(raw, "apify_1688_keyword"):
            return

    # ---- Not found ----
    print("[cogs] ✗ No plausible result from any method", file=sys.stderr)
    not_found = {
        "method": "not_found",
        "source": None,
        "cogs_eur": None,
        "cogs_original": None,
        "cogs_currency": None,
        "product_url": None,
        "confidence": None,
        "matches": [],
        "attempts": attempts,
    }
    json.dump(not_found, sys.stdout, indent=2, ensure_ascii=False)
    print(file=sys.stdout)
    sys.exit(1)


if __name__ == "__main__":
    main()
