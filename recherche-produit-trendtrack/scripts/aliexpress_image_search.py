#!/usr/bin/env /home/ubuntu/.hermes/venvs/ecommerce/bin/python
"""
AliExpress Reverse Image Search Tool
Uses Scrapling with stealth browser automation and fast parsing to perform
reverse image searches on AliExpress and extract product titles, prices, ratings,
sales counts, and URLs.
"""

import sys
import os
import re
import json
import time
import urllib.request
import tempfile
import argparse
from typing import List, Dict, Any, Optional, Union

# Auto-reexec in ecommerce venv if invoked with system python
VENV_PYTHON = "/home/ubuntu/.hermes/venvs/ecommerce/bin/python"
if os.path.exists(VENV_PYTHON) and sys.executable != VENV_PYTHON:
    try:
        import scrapling
    except ImportError:
        os.execv(VENV_PYTHON, [VENV_PYTHON] + sys.argv)

from scrapling import Selector
from scrapling.fetchers import StealthyFetcher


def download_temp_image(url: str) -> str:
    """Download a remote image URL to a temporary local file."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    req = urllib.request.Request(url, headers=headers)
    suffix = ".jpg"
    clean_url = url.split("?")[0].lower()
    for ext in [".png", ".webp", ".jpeg", ".jpg", ".bmp"]:
        if clean_url.endswith(ext):
            suffix = ext
            break

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            tmp.write(resp.read())
        tmp.flush()
        return tmp.name
    finally:
        tmp.close()


def parse_price(raw_text: str) -> Dict[str, Any]:
    """Parse raw price string into numeric value and currency."""
    if not raw_text:
        return {"raw": "", "value": None, "currency": ""}

    cleaned = re.sub(r"\s+", "", raw_text)

    # Detect currency
    currency = ""
    if "€" in cleaned:
        currency = "EUR"
    elif "C$" in raw_text or "CA$" in raw_text:
        currency = "CAD"
    elif "$" in cleaned:
        currency = "USD"
    elif "£" in cleaned:
        currency = "GBP"
    elif "¥" in cleaned or "CNY" in raw_text:
        currency = "CNY"

    # Match primary price pattern like 19,49 or 19.49
    m = re.search(r"(\d+(?:[.,]\d+)?)", cleaned)
    val = None
    if m:
        num_str = m.group(1).replace(",", ".")
        try:
            val = float(num_str)
        except ValueError:
            pass

    return {
        "raw": raw_text.strip(),
        "value": val,
        "currency": currency,
    }


def parse_sales(trade_text: str) -> Dict[str, Any]:
    """Extract numeric sales count from trade text (e.g. '1 152 vendus' -> 1152, '55 sold' -> 55)."""
    if not trade_text:
        return {"raw": "", "count": 0}

    lines = [l.strip() for l in trade_text.splitlines() if l.strip()]
    sales_line = ""
    for l in lines:
        if any(w in l.lower() for w in ["vendu", "sold", "commande", "order"]):
            sales_line = l
            break
    if not sales_line:
        sales_line = trade_text

    # Match patterns like: 1 152 vendus, 5584 vendus, 10k+ vendus, 120 commandes
    m = re.search(
        r"((?:\d{1,3}(?:[\s,]\d{3})+|\d+)(?:[.,]\d+)?)\s*(k\+?|\+)?\s*(?:vendus?|sold|commandes?|orders?)",
        sales_line,
        re.IGNORECASE,
    )
    count = 0
    if m:
        num_str = re.sub(r"[\s,]", "", m.group(1))
        try:
            val = float(num_str)
            if m.group(2) and "k" in m.group(2).lower():
                val *= 1000
            count = int(val)
        except ValueError:
            pass
    return {"raw": sales_line.strip(), "count": count}


def parse_rating(rating_text: str) -> Optional[float]:
    """Extract numeric rating (e.g. '4.9')."""
    if not rating_text:
        return None
    m = re.search(r"([1-5]\.?\d?)", rating_text.strip())
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def search_by_image(
    image_input: str,
    currency: str = "EUR",
    country: str = "FR",
    limit: int = 15,
    first_only: bool = True,
    headless: bool = True,
    timeout_ms: int = 60000,
) -> Union[Dict[str, Any], List[Dict[str, Any]], None]:
    """
    Search AliExpress for products matching an image.
    Extracts all matches and sorts them by number of commands (orders/sales) descending.

    :param image_input: Local file path or HTTP(S) URL of image.
    :param currency: Currency code (e.g. 'EUR', 'USD', 'CAD').
    :param country: Destination country code (e.g. 'FR', 'US', 'DE').
    :param limit: Maximum number of products to return if first_only=False.
    :param first_only: If True (default), return only the first (most ordered) product dict.
    :param headless: Whether to run the browser in headless mode.
    :param timeout_ms: Max execution timeout in milliseconds.
    :return: Top product dict if first_only=True, or list of product dicts sorted by orders if False.
    """
    is_temp = False
    if image_input.startswith("http://") or image_input.startswith("https://"):
        local_image_path = download_temp_image(image_input)
        is_temp = True
    else:
        local_image_path = os.path.abspath(image_input)

    if not os.path.exists(local_image_path):
        raise FileNotFoundError(f"Image not found at path: {local_image_path}")

    captured_html: List[str] = []
    navigated_url: List[str] = []

    def page_action(page):
        # 1. Dismiss any overlay / promo poplayer
        time.sleep(2)
        try:
            page.keyboard.press("Escape")
            page.evaluate("""() => {
                document.querySelectorAll('.image-poplayer-modal, [class*="poplayer"], [class*="modal-mask"]').forEach(el => el.remove());
            }""")
        except Exception:
            pass
        time.sleep(1)

        # 2. Click the image search button
        btn = page.query_selector('[class*="picture-search-btn"]')
        if not btn:
            raise RuntimeError("Could not locate the image search button in AliExpress search bar.")
        btn.click(force=True)
        time.sleep(1)

        # 3. Locate the hidden file input and upload
        file_input = page.wait_for_selector('input[type="file"]', state="attached", timeout=8000)
        file_input.set_input_files(local_image_path)

        # 4. Wait for redirection to image search wholesale results
        page.wait_for_url(lambda u: "wholesale" in u or "isNewImageSearch=y" in u, timeout=30000)
        navigated_url.append(page.url)

        # 5. Wait for product grid to populate
        try:
            page.wait_for_selector('a.search-card-item, div.card-out-wrapper', timeout=20000)
        except Exception:
            pass

        # Scroll to load additional results
        try:
            page.evaluate("window.scrollBy(0, 1000)")
            time.sleep(2)
        except Exception:
            pass

        captured_html.append(page.content())

    # Configure user locale and regional cookie
    locale = "fr_FR" if country.upper() == "FR" else "en_US"
    cookies = [
        {
            "name": "aep_usuc_f",
            "value": f"site=glo&c_tp={currency.upper()}&region={country.upper()}&b_locale={locale}",
            "domain": ".aliexpress.com",
            "path": "/",
        }
    ]

    try:
        StealthyFetcher.fetch(
            "https://www.aliexpress.com",
            cookies=cookies,
            headless=headless,
            page_action=page_action,
            timeout=timeout_ms,
        )
    finally:
        if is_temp and os.path.exists(local_image_path):
            try:
                os.remove(local_image_path)
            except OSError:
                pass

    if not captured_html:
        raise RuntimeError("Failed to capture AliExpress image search results page.")

    # Parse with Scrapling Selector
    sel = Selector(captured_html[0])
    cards = sel.css("a.search-card-item")
    if not cards:
        cards = sel.css(".card-out-wrapper")

    results: List[Dict[str, Any]] = []
    seen_ids = set()

    for card in cards:
        href = card.attrib.get("href") or card.css("a::attr(href)").extract_first() or ""
        if "/item/" not in href:
            continue
        if href.startswith("//"):
            href = "https:" + href
        clean_url = href.split("?")[0]

        # Extract item ID
        m = re.search(r"/item/(\d+)\.html", clean_url)
        item_id = m.group(1) if m else clean_url
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        # Title
        title_el = card.css('[class*="titleText"], [class*="title--"]')
        title = title_el.first.get_all_text().strip() if title_el else ""

        # Price
        price_el = card.css('[class*="price-sale"], [class*="price--"]')
        price_text = price_el.first.get_all_text().strip() if price_el else ""
        price_data = parse_price(price_text)

        # Rating
        rating_el = card.css('[class*="starRating"]')
        rating_text = rating_el.first.get_all_text().strip() if rating_el else ""
        rating_val = parse_rating(rating_text)

        # Trade / Sales
        trade_el = card.css('[class*="trade--"]')
        trade_text = trade_el.first.get_all_text().strip() if trade_el else ""
        sales_data = parse_sales(trade_text)

        # Shipping
        service_el = card.css('[class*="serviceItem"]')
        shipping_text = service_el.first.get_all_text().strip() if service_el else ""

        # Image
        img_el = card.css('img[class*="product-img"], img')
        img_url = ""
        if img_el:
            img_url = img_el.first.attrib.get("src") or img_el.first.attrib.get("data-src", "")
            if img_url.startswith("//"):
                img_url = "https:" + img_url

        # Clean display price format
        display_price = re.sub(r"\s+", " ", price_text.replace("\n", " ")).strip()
        display_price = re.sub(r"(\d+)\s*,\s*(\d+)", r"\1,\2", display_price)

        results.append({
            "item_id": item_id,
            "title": title,
            "price": price_data["value"],
            "currency": price_data["currency"] or currency.upper(),
            "price_formatted": display_price or price_data["raw"],
            "rating": rating_val,
            "sales_count": sales_data["count"],
            "sales_formatted": sales_data["raw"],
            "shipping": shipping_text,
            "url": clean_url,
            "image_url": img_url,
        })

    # Sort all matching products by number of commands (orders/sales count) descending.
    # Tie-break by rating descending, then lowest price.
    results.sort(
        key=lambda x: (
            x["sales_count"],
            x["rating"] if x["rating"] is not None else 0.0,
            -(x["price"] if x["price"] is not None else 999999.0),
        ),
        reverse=True,
    )

    if first_only:
        return results[0] if results else None

    return results[:limit]


def main():
    parser = argparse.ArgumentParser(
        description="Search AliExpress for product prices using an image (sorted by number of commands)."
    )
    parser.add_argument(
        "--image", "-i", required=True, help="Path or URL to the product image."
    )
    parser.add_argument(
        "--currency", "-c", default="EUR", help="Target currency code (default: EUR)."
    )
    parser.add_argument(
        "--country", default="FR", help="Destination country code (default: FR)."
    )
    parser.add_argument(
        "--all", action="store_true", help="Return all matching products sorted by orders instead of only the first (best-selling) item."
    )
    parser.add_argument(
        "--limit", "-l", type=int, default=10, help="Max results to return when --all is set (default: 10)."
    )
    parser.add_argument(
        "--json", action="store_true", help="Output results in JSON format."
    )
    parser.add_argument(
        "--no-headless", action="store_true", help="Run browser in headful mode (debugging)."
    )
    parser.add_argument(
        "--timeout", type=int, default=60, help="Timeout in seconds (default: 60)."
    )

    args = parser.parse_args()

    first_only = not args.all

    try:
        result = search_by_image(
            image_input=args.image,
            currency=args.currency,
            country=args.country,
            limit=args.limit,
            first_only=first_only,
            headless=not args.no_headless,
            timeout_ms=args.timeout * 1000,
        )
    except Exception as e:
        if args.json:
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # Formatted CLI output
    if not result:
        print("No matching products found on AliExpress.")
        return

    # Single top item mode (default)
    if first_only:
        item = result
        p_str = f"{item['price']} {item['currency']}" if item["price"] is not None else item["price_formatted"]
        rating_str = f"{item['rating']} ★" if item["rating"] else "N/A"
        sales_str = item["sales_formatted"] if item["sales_formatted"] else f"{item['sales_count']} commandes"
        ship_str = f" | {item['shipping']}" if item["shipping"] else ""

        print(f"\n=================== AliExpress Image Search (Top Commande) ===================")
        print(f"Produit le plus commandé pour l'image : {args.image}")
        print(f"==============================================================================")
        print(f"Titre :       {item['title']}")
        print(f"Prix unitaire:{p_str}")
        print(f"Commandes :   {sales_str} (Note : {rating_str}{ship_str})")
        print(f"Lien direct : {item['url']}")
        print(f"Image :       {item['image_url']}")
        print(f"==============================================================================\n")
        return

    # Multiple items list mode (only when --all is explicitly passed)
    items = result
    print(f"\n=================== AliExpress Search By Image (Sorted by Orders) ===================")
    print(f"Found {len(items)} matching products for image: {args.image}")
    print(f"====================================================================================\n")

    for idx, item in enumerate(items, start=1):
        p_str = f"{item['price']} {item['currency']}" if item["price"] is not None else item["price_formatted"]
        rating_str = f"{item['rating']} ★" if item["rating"] else "N/A"
        sales_str = item["sales_formatted"] if item["sales_formatted"] else f"{item['sales_count']} commandes"
        ship_str = f" | {item['shipping']}" if item["shipping"] else ""

        print(f"[{idx}] {item['title'][:70]}")
        print(f"    Prix:     {p_str} (Commandes: {sales_str} | Note: {rating_str}{ship_str})")
        print(f"    Lien:     {item['url']}")
        print(f"    Image:    {item['image_url'][:75]}...")
        print()


if __name__ == "__main__":
    main()
