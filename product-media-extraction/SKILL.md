---
name: product-media-extraction
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
description: "Use when product images must be pulled from a product page."
metadata:
  hermes:
    tags: [ecommerce, product, images, media, shopify, scraping]
    related_skills: [aliexpress-image-search, ad-creative-repurposing]
---

# Product Media Extraction

Harvest the pertinent image assets from a product page and emit a manifest a
consumer (Shopify product import, creative work, COGS image search) can act
on. Deterministic: same page in, same manifest out. The pipeline is
`scripts/extract_product_images.py` (Python 3 stdlib only).

## Procedure

1. **Fetch with a complete browser header set** — `User-Agent`, `Accept`,
   `Accept-Language`, `Sec-Fetch-*`, `Upgrade-Insecure-Requests`. Storefront
   WAFs (Shopify's included) return 429 to header-less `curl` purely on
   request shape; one headers-complete retry unlocks the live page.
2. **Collect candidates from three sources, in priority order:**
   1. `GET <product-url>.js` — Shopify's product JSON: `images[]` +
      `media[]` where `media_type == "image"`. This is the merchant's own
      gallery and the authoritative source.
   2. `og:image` meta + JSON-LD `image` fields.
   3. `<img src|data-src>` in the page — page-builder content lives here.
   Canonical key per candidate: scheme+host+path with Shopify resize
   variants stripped (`_{w}x{h}` suffix, `width=` query).
3. **Apply the filter heuristic** — every rejection carries its reason in the
   manifest, nothing is silently dropped:
   - **format**: `.jpg .jpeg .png .webp` only (svg/gif/video out); magic
     bytes decide after download, not the extension.
   - **filename denylist** (basename, case-insensitive): logos/icons,
     payment marks, review widgets, badges, site chrome. The authoritative
     word list is `DENY_NAME` in the script.
   - **host**: first-party only — the product-page host (incl. subdomains)
     plus `cdn.shopify.com`. Third-party hosts (payment logos, review
     widgets, upload CDNs) are UI chrome, and this is also the branding
     guard: competitor badges die here.
   - **geometry**: width >= 500px, min side >= 400px, aspect ratio 0.5-2.5.
4. **Download each survivor and verify from bytes**: real dimensions and
   format via magic-byte sniffing, then dedupe on **content hash AND
   canonical basename** (see pitfalls — one alone misses duplicates).
5. **Cap at 20**, write `manifest.json` with `kept` (url, w, h, format,
   sources, in gallery order) and `rejected` (url, why). If fewer than 3
   images survive, report that to the caller — never relax the rules to
   inflate a thin page.

## Pitfalls

- **Dedupe must key on basename as well as content hash.** CDNs serve
  host-specific re-encodes of one asset (`cdn.shopify.com/s/files/...` vs
  `<shop>/cdn/shop/files/...`): identical filename, different bytes, so
  hash-only dedupe keeps both. Basename-only misses renamed copies. Use both.
- **Never trust the extension over the bytes** — with an `Accept: image/webp`
  request header the CDN answers WebP for `.png` URLs; record format and
  dimensions from the magic bytes.
- **Process the gallery pool before the page pool** so dedupe tie-breaks
  keep the merchant's gallery copy — deterministic order, stable manifest.
- **Keep the geometry thresholds.** `too small` rejections are mostly UI
  chrome (icons, 1020x301 strips, trust badges); the odd real image below
  the bar is acceptable loss — the caller reviews the manifest anyway.
- **Strip Shopify resize params before comparing URLs** or one asset
  multiplies into `_600x`, `_100x`, `width=1000` variants that all survive
  naive dedupe.

## Scripts

- `scripts/extract_product_images.py <product_url> [--out manifest.json]` —
  full pipeline (steps 1-5). Prints kept/rejected with reasons; `--out`
  writes the manifest consumers read.
