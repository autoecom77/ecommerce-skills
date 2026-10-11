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
import urllib.parse
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    api_url = f"https://api.apify.com/v2/acts/{actor_id}/runs"

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

    req = urllib.request.Request(api_url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            run_data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"[cogs] Apify run start failed: {e}", file=sys.stderr)
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
    args = parser.parse_args()

    image_input = args.image_url or args.image_path
    is_url = args.image_url is not None

    product_name = args.product_name
    if product_name:
        print(f"[cogs] Searching COGS for: {product_name}", file=sys.stderr)

    # Determine Apify token
    apify_token = args.apify_token or load_apify_token()

    result = None

    # ---- Method 1: Apify 1688 Image Search ----
    if apify_token and (args.image_url or args.image_path):
        try:
            result = search_apify_1688(
                image_url=args.image_url,
                image_path=args.image_path,
                apify_token=apify_token,
            )
        except Exception as e:
            print(f"[cogs] Apify error: {e}", file=sys.stderr)

    if result:
        print(f"[cogs] ✓ Found via Apify 1688: {result['cogs_eur']}€", file=sys.stderr)
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        print(file=sys.stdout)
        return

    # ---- Method 2: AliExpress Image Search (fallback) ----
    # For URL images, download to temp file first for aliexpress script
    temp_path = None
    try:
        if is_url:
            print("[cogs] Downloading image for AliExpress search...", file=sys.stderr)
            temp_path = download_image_to_temp(args.image_url)
            ali_input = temp_path
        else:
            ali_input = args.image_path

        result = search_aliexpress_image(ali_input, is_url=False)
    except Exception as e:
        print(f"[cogs] AliExpress error: {e}", file=sys.stderr)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    if result:
        print(f"[cogs] ✓ Found via AliExpress: {result['cogs_eur']}€", file=sys.stderr)
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        print(file=sys.stdout)
        return

    # ---- Not found ----
    print("[cogs] ✗ No results from any method", file=sys.stderr)
    not_found = {
        "method": "not_found",
        "source": None,
        "cogs_eur": None,
        "cogs_original": None,
        "cogs_currency": None,
        "product_url": None,
        "confidence": None,
        "matches": [],
    }
    json.dump(not_found, sys.stdout, indent=2, ensure_ascii=False)
    print(file=sys.stdout)
    sys.exit(1)


if __name__ == "__main__":
    main()
