#!/usr/bin/env python3
"""Extract pertinent product image assets from a product page.

Usage:
    python3 extract_product_images.py <product_url> [--out manifest.json]

Pipeline: fetch (browser headers) -> candidates (Shopify product.js gallery,
og:image/JSON-LD, page <img> tags) -> filter heuristic -> download +
magic-byte verify -> dedupe (content hash AND canonical basename) ->
manifest {kept, rejected-with-reason}. Python 3 stdlib only.
Heuristic rules documented in the skill's SKILL.md.
"""
import hashlib
import json
import os
import re
import struct
import sys
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# ---- filter heuristic (authoritative word list) -------------------------
KEEP_EXT = {".jpg", ".jpeg", ".png", ".webp"}
DENY_NAME = re.compile(
    r"(logo|favicon|icon|sprite|payment|afterpay|klarna|paypal|stripe|"
    r"apple[-_]?pay|google[-_]?pay|gpay|trustpilot|judge|yotpo|loox|stamped|"
    r"review|star|flag|avatar|placeholder|loading|spinner|cookie|newsletter|"
    r"header|footer|nav|menu|search|cart|wishlist|account|badge|seal|secure|"
    r"shipping|delivery|returns?|garantie)", re.I)
FIRST_PARTY_EXTRA = {"cdn.shopify.com"}   # plus any subdomain of the shop host
MIN_WIDTH = 500
MIN_SIDE = 400
AR_MIN, AR_MAX = 0.5, 2.5
MAX_IMAGES = 20


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")


def sniff(data):
    """Return (format, w, h) from magic bytes; (None, 0, 0) if unknown."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "png", *struct.unpack(">II", data[16:24])
        if data[:2] == b"\xff\xd8":
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return "jpeg", w, h
                seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + max(seglen, 2)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                return ("webp",
                        int.from_bytes(data[24:27], "little") + 1,
                        int.from_bytes(data[27:30], "little") + 1)
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return "webp", w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                b = data[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) |
                         ((b[1] & 0xC0) >> 6))
                return "webp", w, h
    except Exception:
        pass
    return None, 0, 0


def _strip_variant(path):
    return re.sub(r"_\d{2,4}x\d{2,4}(?=\.\w+$)", "", path)


def canonical(u):
    """Strip Shopify resize variants so URL duplicates collapse."""
    p = urllib.parse.urlsplit(u)
    q = urllib.parse.parse_qs(p.query)
    q.pop("width", None)
    return urllib.parse.urlunsplit(
        (p.scheme, p.netloc, _strip_variant(p.path),
         urllib.parse.urlencode(q, doseq=True), ""))


def name_key(u):
    """Canonical basename — one asset on two CDN hosts keeps its filename."""
    base = os.path.basename(urllib.parse.urlsplit(u).path)
    return _strip_variant(urllib.parse.unquote(base).lower())


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    url = sys.argv[1]
    out_path = (sys.argv[sys.argv.index("--out") + 1]
                if "--out" in sys.argv else None)
    shop_host = urllib.parse.urlsplit(url).netloc.lower()

    js_imgs, js_media = [], []
    try:
        body, _ = http_get(url.rstrip("/") + ".js")
        d = json.loads(body)
        js_imgs = d.get("images", [])
        js_media = [m.get("src") for m in d.get("media", [])
                    if m.get("media_type") == "image"]
    except Exception as e:
        print(f"[warn] product.js unavailable: {e}", file=sys.stderr)
    html_s = ""
    try:
        html, _ = http_get(url)
        html_s = html.decode("utf-8", "replace")
    except Exception as e:
        print(f"[warn] html fetch failed: {e}", file=sys.stderr)

    def absurl(u):
        u = u.strip()
        return "https:" + u if u.startswith("//") else u

    pool_gallery, pool_page = {}, {}

    def add(pool, u, source):
        u = absurl(u)
        if not re.match(r"https?://", u):
            return
        key = canonical(u)
        pool.setdefault(key, {"url": u, "sources": []})["sources"].append(source)

    for u in js_imgs:
        add(pool_gallery, u, "product.js images")
    for u in js_media:
        add(pool_gallery, u, "product.js media")
    for m in re.findall(
            r'<meta[^>]+property="og:image(?::\w+)?"[^>]+content="([^"]+)"',
            html_s):
        add(pool_gallery, m, "og:image")
    for m in re.findall(r'<img[^>]+(?:src|data-src)="([^"]+)"', html_s):
        add(pool_page, m, "img tag")

    def first_party(u):
        host = urllib.parse.urlsplit(u).netloc.lower()
        return (host in FIRST_PARTY_EXTRA or host == shop_host
                or host.endswith("." + shop_host))

    candidates = []
    rejected = []
    for pool in (pool_gallery, pool_page):   # gallery first: it wins ties
        for item in pool.values():
            u = item["url"]
            path = urllib.parse.urlsplit(u).path
            ext = os.path.splitext(path)[1].lower()
            name = urllib.parse.unquote(os.path.basename(path))
            if ext not in KEEP_EXT:
                rejected.append({**item, "why": f"format {ext or 'none'}"})
            elif DENY_NAME.search(name):
                rejected.append({**item, "why": "filename denylist"})
            elif not first_party(u):
                rejected.append({**item, "why": "third-party host"})
            else:
                candidates.append(item)

    kept = []
    seen_sha, seen_name = {}, {}
    for item in candidates:
        rec = dict(item)
        try:
            data, ctype = http_get(item["url"], timeout=30)
        except Exception as e:
            rec.update(kept=False, why=f"download failed: {e}")
            rejected.append(rec)
            continue
        fmt, w, h = sniff(data)
        sha = hashlib.sha1(data).hexdigest()[:12]
        nkey = name_key(item["url"])
        rec.update(content_type=ctype, format=fmt, w=w, h=h, sha1=sha)
        if not fmt:
            rec.update(kept=False, why="unreadable dims / not an image")
        elif w < MIN_WIDTH or min(w, h) < MIN_SIDE:
            rec.update(kept=False, why=f"too small {w}x{h}")
        elif not (AR_MIN <= w / h <= AR_MAX):
            rec.update(kept=False, why=f"aspect ratio {w / h:.2f}")
        elif sha in seen_sha:
            rec.update(kept=False, why=f"duplicate of {seen_sha[sha]}")
        elif nkey in seen_name:
            rec.update(kept=False, why=f"duplicate of {seen_name[nkey]}")
        else:
            seen_sha[sha] = seen_name[nkey] = item["url"]
            rec["kept"] = True
            kept.append(rec)
            continue
        rejected.append(rec)

    kept = kept[:MAX_IMAGES]
    print(f"candidates: gallery={len(pool_gallery)} page={len(pool_page)}")
    print(f"KEPT: {len(kept)} (cap {MAX_IMAGES})")
    for r in kept:
        print(f"  + {r['w']}x{r['h']} {r['format']:4} "
              f"{r['url'][:110]}  <- {','.join(r['sources'])}")
    print(f"REJECTED: {len(rejected)}")
    for r in rejected:
        print(f"  - [{r['why']}] {r['url'][:110]}")
    if len(kept) < 3:
        print("[warn] fewer than 3 images survived — caller should review")

    if out_path:
        json.dump({"product_url": url, "kept": kept, "rejected": rejected},
                  open(out_path, "w"), indent=1)
        print(f"manifest -> {out_path}")


if __name__ == "__main__":
    main()
