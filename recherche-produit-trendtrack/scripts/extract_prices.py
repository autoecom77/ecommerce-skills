#!/usr/bin/env python3
"""extract_prices.py — Fast multithreaded selling price extraction (Shopify & Web).

Extracts the retail selling price in EUR from landing pages / product URLs.
Prioritizes native Shopify endpoint (/products/<handle>.js), falling back to
OpenGraph (og:price:amount), JSON-LD (schema.org/Product), and HTML regex.

Usage:
    # 1. Pipeline mode (process JSON array of candidates):
    python3 extract_prices.py --input /tmp/tt_candidates.json --output /tmp/tt_priced.json

    # 2. Single URL test mode:
    python3 extract_prices.py --url "https://nubelle-shop.de/products/nubelle-premium-komfort-bh"

    # 3. Google Sheet mode (scan column URL from Sheet):
    python3 extract_prices.py --from-sheet 1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM --range "Products!B2:C500"
"""

import argparse
import gzip
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

# Standard conversion rates to EUR
CONVERSION_RATES: Dict[str, float] = {
    "EUR": 1.0,
    "USD": 0.92,
    "GBP": 1.16,
    "CHF": 1.06,
    "CAD": 0.66,
    "SEK": 0.087,
    "DKK": 0.134,
    "NOK": 0.085,
    "PLN": 0.23,
    "CZK": 0.04,
    "HUF": 0.0025,
    "AUD": 0.60,
}

BAD_HANDLES = {
    "gift-card",
    "carte-cadeau",
    "carte-bon-cadeau",
    "bon-cadeau",
    "geschenkkarte",
    "cadeaukaart",
    "shipping-protection",
    "route-package-protection",
    "insurance",
    "versandschutz",
}

GAPI_PYTHON = Path(
    os.environ.get(
        "GAPI_PYTHON",
        Path.home() / ".hermes/venvs/gws/bin/python",
    )
)
GAPI_SCRIPT = Path(
    os.environ.get(
        "GAPI_SCRIPT",
        Path.home() / ".hermes/skills/productivity/google-workspace/scripts/google_api.py",
    )
)


def has_wg0_interface() -> bool:
    """Check if wg0 network interface is available on this system."""
    try:
        res = subprocess.run(["ip", "link", "show", "wg0"], capture_output=True, text=True, timeout=2)
        return res.returncode == 0
    except Exception:
        return False

HAS_WG0 = has_wg0_interface()


def fetch_page(
    url: str,
    country: str = "FR",
    timeout: int = 20,
    accept: str = "text/html",
) -> Tuple[str, str]:
    """Fetch URL with proper headers, localization cookies, and wg0 interface if available."""
    cookie_str = f"localization={country}; cart_currency=EUR"
    accept_lang = {
        "FR": "fr-FR,fr;q=0.9",
        "DE": "de-DE,de;q=0.9",
        "NL": "nl-NL,nl;q=0.9",
    }.get(country, "fr-FR,fr;q=0.9") + ",en;q=0.6"

    cmd = [
        "curl",
        "-sL",
        "--compressed",
        "--max-time", str(timeout),
        "-A", UA,
        "-H", f"Accept: {accept},*/*;q=0.8",
        "-H", f"Accept-Language: {accept_lang}",
        "-H", f"Cookie: {cookie_str}",
        "-w", "\n%{url_effective}",
    ]
    if HAS_WG0:
        cmd.extend(["--interface", "wg0"])
    cmd.append(url)

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    if res.returncode != 0:
        raise RuntimeError(f"curl failed with code {res.returncode}: {res.stderr[:100]}")

    output = res.stdout
    if "\n" in output:
        parts = output.rsplit("\n", 1)
        body = parts[0]
        final_url = parts[1].strip() or url
    else:
        body = output
        final_url = url

    return final_url, body


def clean_url(u: Optional[str]) -> str:
    """Normalize and clean URL string."""
    if not u:
        return ""
    u = u.strip().replace(" ", "")
    if u.startswith("http://"):
        u = "https://" + u[7:]
    return u


def find_currency(page_html: str) -> Tuple[Optional[str], Optional[str]]:
    """Detect currency from Shopify config, OpenGraph, or JSON-LD."""
    m = re.search(
        r'Shopify\.currency\s*=\s*\{[^}]*"active"\s*:\s*"([A-Z]{3})"', page_html
    )
    if m:
        return m.group(1), "shopify_currency"
    m = re.search(
        r'property=["\']og:price:currency["\']\s+content=["\']([A-Z]{3})',
        page_html,
    )
    if m:
        return m.group(1), "og"
    m = re.search(r'"priceCurrency"\s*:\s*"([A-Z]{3})"', page_html)
    if m:
        return m.group(1), "jsonld"
    return None, None


def find_handle(
    final_url: str,
    page_html: str,
    country: str = "FR",
) -> Tuple[Optional[str], Optional[str]]:
    """Extract product handle from URL or links in page."""
    path = urllib.parse.urlparse(final_url).path
    m = re.search(r"/products/([^/?#]+)", path)
    if m:
        return urllib.parse.unquote(m.group(1)), "url"

    # Search for product links on the page (advertorials / presell pages)
    handles = re.findall(r"/products/([a-zA-Z0-9\-_%\.]+)", page_html)
    counts: Dict[str, int] = {}
    for h in handles:
        h = urllib.parse.unquote(h).split(".")[0]
        if not h or h in BAD_HANDLES or h.endswith("-js"):
            continue
        counts[h] = counts.get(h, 0) + 1
    if counts:
        best = sorted(counts.items(), key=lambda kv: -kv[1])[0][0]
        return best, "link"

    # Search for variant IDs in cart links or Buy Buttons (Shopify advertorial / presell pages)
    v_ids = re.findall(r"/cart/(\d{10,})", page_html)
    v_ids += re.findall(r'(?:data-variant-id|name=["\']id["\']\s+value)=["\']?(\d{10,})', page_html)
    if v_ids:
        host = urllib.parse.urlparse(final_url).netloc
        try:
            _, pjson_raw = fetch_page(
                f"https://{host}/products.json?limit=250",
                country,
                accept="application/json",
            )
            pdata = json.loads(pjson_raw)
            target_ids = {int(vid) for vid in v_ids}
            for prod in pdata.get("products", []):
                for v in prod.get("variants", []):
                    if v.get("id") in target_ids:
                        return prod.get("handle"), "variant_cart"
        except Exception:
            pass

    return None, None


def extract_og_or_jsonld_price(page_html: str) -> Optional[float]:
    """Fallback price extraction from OpenGraph, JSON-LD, Microdata, or clean HTML regex."""
    # 1. OpenGraph amount
    m = re.search(
        r'property=["\'](?:og|product):price:amount["\']\s+content=["\']([\d.,]+)',
        page_html,
    )
    if m:
        v = m.group(1).replace(",", "")
        try:
            return float(v)
        except ValueError:
            pass

    # 2. Schema microdata
    m = re.search(r'itemprop=["\']price["\']\s+content=["\']([\d.,]+)["\']', page_html, re.I)
    if not m:
        m = re.search(r'content=["\']([\d.,]+)["\']\s+itemprop=["\']price["\']', page_html, re.I)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass

    # 3. JSON-LD price
    m = re.search(
        r'"price"\s*:\s*"?([\d]+[.,]?\d*)"?\s*,\s*"priceCurrency"', page_html
    )
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass

    # 4. Generic schema price
    m = re.search(r'"price"\s*:\s*([0-9]+(?:\.[0-9]+)?)', page_html)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass

    # 5. Clean HTML Regex fallback (explicitly avoiding strikethrough/compare/old prices)
    patterns = [
        r'data-(?![a-z0-9_-]*(?:compare|old|was|regular|strike|del))[a-z0-9_-]*price=["\']([0-9]+[,\.][0-9]{2})["\']',
        r'<(?:span|strong|b|p|div)[^>]*class=["\'](?![^"\']*(?:old|compare|was|regular|strike|del|original|crossed|msrp|before))[^"\']*(?:current[-_]?price|sale[-_]?price|special[-_]?price|product[-_]?price|price-new|new[-_]?price|offer[-_]?price)[^"\']*["\'][^>]*>\s*(?:[€$£]?\s*)?([0-9]+[,\.][0-9]{2})',
    ]
    for pat in patterns:
        for match in re.finditer(pat, page_html, re.I):
            val_str = match.group(1).replace(",", ".")
            try:
                v = float(val_str)
                if 5.0 <= v <= 2000.0:
                    return v
            except ValueError:
                pass

    return None


def extract_price_from_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Extract price and metadata for a single candidate."""
    out = dict(candidate)
    raw_url = candidate.get("landing_url") or candidate.get("product_url") or candidate.get("url")
    url = clean_url(raw_url)

    cb = candidate.get("country_breakdown") or {}
    country = (cb.get("main") or candidate.get("country") or "FR").upper()
    if country not in ("FR", "DE", "NL", "BE", "AT", "ES", "IT", "UK", "US"):
        country = "FR"

    out.update({
        "price_eur": None,
        "price_currency": None,
        "price_source": None,
        "product_title": candidate.get("product_title") or candidate.get("title"),
        "product_handle": None,
        "product_url": candidate.get("product_url") or url,
        "product_image": candidate.get("product_image") or candidate.get("image_url"),
        "is_shopify": None,
        "variants": [],
        "price_error": None,
    })

    if not url:
        out["price_error"] = "no landing url"
        return out

    try:
        final_url, page_html = fetch_page(url, country)
    except Exception as e:
        out["price_error"] = f"landing fetch error: {str(e)[:120]}"
        return out

    host = urllib.parse.urlparse(final_url).netloc
    is_shopify = (
        ("cdn.shopify.com" in page_html)
        or ("Shopify.shop" in page_html)
        or ("myshopify.com" in page_html)
    )
    out["is_shopify"] = is_shopify
    out["final_url"] = final_url

    detected_curr, _ = find_currency(page_html)
    handle, hsrc = find_handle(final_url, page_html, country)

    # 1. Native Shopify JSON endpoint
    if is_shopify and handle:
        purl = f"https://{host}/products/{handle}"
        try:
            _, js_content = fetch_page(purl + ".js", country, accept="application/json")
            pj = json.loads(js_content)
            variants = pj.get("variants") or []
            selected_var = None
            for v in variants:
                if v.get("available"):
                    selected_var = v
                    break
            if selected_var is None and variants:
                selected_var = variants[0]

            price_cents = (selected_var or {}).get("price", pj.get("price"))
            price = round(price_cents / 100.0, 2) if price_cents is not None else None

            if not detected_curr:
                try:
                    _, ppage = fetch_page(purl, country)
                    detected_curr, _ = find_currency(ppage)
                except Exception:
                    pass

            curr = detected_curr or "EUR"
            rate = CONVERSION_RATES.get(curr, 1.0)
            feat_img = pj.get("featured_image") or ""
            if feat_img.startswith("//"):
                feat_img = "https:" + feat_img

            out.update({
                "product_title": pj.get("title") or out["product_title"],
                "product_handle": handle,
                "product_url": purl,
                "product_image": feat_img or out["product_image"],
                "price_currency": curr,
                "price_raw": price,
                "price_eur": round(price * rate, 2) if price is not None else None,
                "price_source": f"shopify_js({hsrc})",
                "variants": [
                    (v.get("title"), round(v.get("price", 0) / 100.0, 2), v.get("available"))
                    for v in variants[:8]
                ],
                "product_type": pj.get("type"),
                "vendor": pj.get("vendor"),
                "price_error": None,
            })
            return out
        except Exception as e:
            out["price_error"] = f"shopify js error: {str(e)[:100]}"

    # 2. Fallback: OpenGraph / JSON-LD / HTML Regex
    p = extract_og_or_jsonld_price(page_html)
    if p is not None:
        curr = detected_curr or "EUR"
        rate = CONVERSION_RATES.get(curr, 1.0)
        t_match = re.search(
            r'property=["\']og:title["\']\s+content=["\']([^"\']+)', page_html
        )
        img_match = re.search(
            r'property=["\']og:image["\']\s+content=["\']([^"\']+)', page_html
        )

        out.update({
            "price_raw": p,
            "price_currency": curr,
            "price_eur": round(p * rate, 2),
            "price_source": "og/jsonld",
            "product_url": final_url,
            "product_title": html.unescape(t_match.group(1))[:120] if t_match else out["product_title"],
            "product_image": img_match.group(1) if img_match else out["product_image"],
            "price_error": None,
        })
        return out

    if not out.get("price_error"):
        out["price_error"] = (
            "no price found"
            + ("" if is_shopify else " (non-shopify)")
            + ("" if handle else " (no handle)")
        )
    return out


def process_candidates_list(
    candidates: List[Dict[str, Any]],
    workers: int = 12,
) -> List[Dict[str, Any]]:
    """Process a list of candidates in parallel."""
    t0 = time.time()
    results = [None] * len(candidates)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(extract_price_from_candidate, c): i
            for i, c in enumerate(candidates)
        }
        done = 0
        for f in as_completed(futs):
            i = futs[f]
            try:
                results[i] = f.result()
            except Exception as e:
                r = dict(candidates[i])
                r["price_error"] = f"crash: {e}"
                r["price_eur"] = None
                results[i] = r
            done += 1
            if done % 25 == 0 or done == len(candidates):
                print(
                    f"[extract_prices] {done}/{len(candidates)} processed ({time.time()-t0:.1f}s)",
                    file=sys.stderr,
                )

    priced_count = sum(1 for r in results if r and r.get("price_eur") is not None)
    print(
        f"[extract_prices] Done: {priced_count}/{len(results)} priced successfully in {time.time()-t0:.1f}s",
        file=sys.stderr,
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fast selling price extraction for TrendTrack ecommerce products."
    )
    parser.add_argument("--input", "-i", help="Input JSON file of candidates.")
    parser.add_argument("--output", "-o", help="Output JSON file for priced candidates.")
    parser.add_argument("--url", "-u", help="Test a single product URL.")
    parser.add_argument("--workers", "-w", type=int, default=12, help="Number of worker threads (default 12).")
    parser.add_argument("--from-sheet", help="Google Sheet ID to scan for URLs.")
    parser.add_argument("--range", default="Products!B2:C500", help="Range to scan in Sheet (default Products!B2:C500).")
    args = parser.parse_args()

    # Mode 1: Single URL test
    if args.url:
        cand = {"landing_url": args.url, "title": "Test Product"}
        res = extract_price_from_candidate(cand)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return

    # Mode 2: Google Sheet scan
    if args.from_sheet:
        if not GAPI_SCRIPT.exists():
            raise SystemExit(f"Google API script not found at {GAPI_SCRIPT}")
        cmd = [
            str(GAPI_PYTHON),
            str(GAPI_SCRIPT),
            "sheets",
            "get",
            args.from_sheet,
            args.range,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise SystemExit(f"Failed to read sheet: {r.stderr}")
        data = json.loads(r.stdout)
        values = data.get("values", []) if isinstance(data, dict) else data
        candidates = []
        for row in values:
            if len(row) >= 2 and row[1]:
                candidates.append({"title": row[0], "landing_url": row[1]})
        results = process_candidates_list(candidates, workers=args.workers)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
        else:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        return

    # Mode 3: JSON File pipeline
    if not args.input:
        parser.error("Either --input, --url, or --from-sheet must be specified.")

    with open(args.input, "r", encoding="utf-8") as f:
        candidates = json.load(f)

    results = process_candidates_list(candidates, workers=args.workers)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"[extract_prices] Wrote {len(results)} priced candidates to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
