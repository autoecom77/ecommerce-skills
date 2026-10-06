---
name: aliexpress-image-search
description: "Reverse image search on AliExpress using Scrapling to find products, prices, and direct links."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [ecommerce, aliexpress, image-search, cogs, scrapling, reverse-image-search]
    related_skills: [recherche-produit-trendtrack]
---

# AliExpress Image Search

Performs visual product searches directly against AliExpress using **Scrapling** with stealth browser automation and fast CSS parsing. This allows discovering identical or similar products, supplier links, unit prices (COGS), ratings, and sales volume starting from a product image file or remote image URL.

## Environment & Dependencies

- **Python Virtualenv**: `/home/ubuntu/.hermes/venvs/ecommerce/bin/python`
- **Scrapling Version**: 0.4.15+ (with `patchright`, `browserforge`, `curl-cffi`)
- **Browser**: Patchright Chromium (`~/.cache/ms-playwright/chromium-1243`)

## CLI Usage

The script is available at:
`~/.hermes/skills/ecommerce/aliexpress-image-search/scripts/aliexpress_image_search.py`
(and symlinked at `~/.hermes/skills/ecommerce/aliexpress_image_search.py`).

### 1. Basic Search with a Local Image (Top Commande / Best-selling Item)
```bash
/home/ubuntu/.hermes/venvs/ecommerce/bin/python \
  /home/ubuntu/.hermes/skills/ecommerce/aliexpress_image_search.py \
  --image /path/to/product_image.jpg \
  --currency EUR \
  --country FR
```

### 2. Search with a Web Image URL (Auto-downloaded)
```bash
/home/ubuntu/.hermes/venvs/ecommerce/bin/python \
  /home/ubuntu/.hermes/skills/ecommerce/aliexpress_image_search.py \
  --image "https://example.com/ad_creatives/product.jpg" \
  --currency EUR \
  --country FR
```

### 3. Programmatic JSON Output (For automation & COGS workflows)
By default, returns the single most-ordered matching item directly as a JSON object (no averaging needed):
```bash
/home/ubuntu/.hermes/venvs/ecommerce/bin/python \
  /home/ubuntu/.hermes/skills/ecommerce/aliexpress_image_search.py \
  --image /tmp/sample.png \
  --json
```

Output format (single top product):
```json
{
  "item_id": "1005011940027587",
  "title": "Nouveaux écouteurs Bluetooth sans fil, casque supra-auriculaire...",
  "price": 17.69,
  "currency": "EUR",
  "price_formatted": "17,69 €",
  "rating": 4.9,
  "sales_count": 1152,
  "sales_formatted": "1152 vendus",
  "shipping": "Livraison gratuite dès 10,00€ d'achat",
  "url": "https://fr.aliexpress.com/item/1005011940027587.html",
  "image_url": "https://ae-pic-a1.aliexpress-media.com/kf/S...jpg"
}
```

If multiple ranked products are needed, add `--all` (optional `--limit 10`).

## Python SDK Integration

You can import `search_by_image` directly in any Python script:

```python
import sys
sys.path.append("/home/ubuntu/.hermes/skills/ecommerce/aliexpress-image-search/scripts")
from aliexpress_image_search import search_by_image

# Search by local file or URL
items = search_by_image(
    image_input="https://example.com/creative.jpg",
    currency="EUR",
    country="FR",
    limit=10,
    headless=True
)

for item in items:
    print(f"{item['title']} - {item['price']} {item['currency']} ({item['url']})")
```

## Workflow Integration (TrendTrack COGS)

In `recherche-produit-trendtrack` (Étape 3 — COGS):
Instead of relying on fragile keyword text search when keywords fail or yield imprecise matches:
1. Extract the product image from the ad landing page.
2. Call `aliexpress_image_search.py --image <image_url_or_path> --json`.
3. Read the lowest and median supplier prices and the best-matching item URL.
