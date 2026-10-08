#!/usr/bin/env python3
"""Harvest pertinent product image assets from a product page and filter them.

Usage: python3 fetch_images.py <product_url> [--out manifest.json]
       [--pools gallery,page] [--max 20] [--include REGEX]

Stdlib only. Shopify sites: gallery comes from the product.js/.json endpoint
(authoritative). Fallbacks: og:image + JSON-LD (gallery pool), <img> tags (page pool).
Heuristic tested 2026-10-06 on luveon.com: 66 candidates -> 18 kept.
"""
import sys, json, re, hashlib, struct, urllib.request, urllib.parse, os

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

KEEP_EXT = {".jpg", ".jpeg", ".png", ".webp"}
DENY_NAME = re.compile(
    r"(logo|favicon|icon|sprite|payment|afterpay|klarna|paypal|stripe|"
    r"apple[-_]?pay|google[-_]?pay|gpay|trustpilot|judge|yotpo|loox|stamped|"
    r"review|star|flag|avatar|placeholder|loading|spinner|cookie|newsletter|"
    r"header|footer|nav|menu|search|cart|wishlist|account|"
    r"badge|seal|secure|shipping|delivery|returns?|garantie)", re.I)
FIRST_PARTY_EXTRA = {"cdn.shopify.com"}
MIN_SIDE = 400
MIN_WIDTH = 500
AR_MIN, AR_MAX = 0.5, 2.5


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")


def sniff_dims(data):
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:2] == b"\xff\xd8":
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1; continue
                m = data[i + 1]
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                         0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            c = data[12:16]
            if c == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1,
                        int.from_bytes(data[27:30], "little") + 1)
            if c == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if c == b"VP8L":
                b = data[21:25]
                return (1 + (((b[1] & 0x3F) << 8) | b[0]),
                        1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6)))
    except Exception:
        pass
    return None


def canonical(u):
    p = urllib.parse.urlsplit(u)
    q = urllib.parse.parse_qs(p.query)
    q.pop("width", None)
    base = re.sub(r"_\d+x\d+(?=\.\w+$)", "", p.path)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, base,
                                   urllib.parse.urlencode(q, doseq=True), ""))


def canon_basename(u):
    """Filename identity across hosts/resize variants (dedupe key #2)."""
    name = urllib.parse.unquote(urllib.parse.urlsplit(u).path.split("/")[-1])
    name = re.sub(r"_\d+x\d+(?=\.\w+$)", "", name)
    return name.lower()


def main():
    url = sys.argv[1]
    out_path = None
    pools = {"gallery", "page"}
    max_images = 20
    include_re = None
    args = sys.argv[2:]
    if "--out" in args: out_path = args[args.index("--out") + 1]
    if "--pools" in args: pools = set(args[args.index("--pools") + 1].split(","))
    if "--max" in args: max_images = int(args[args.index("--max") + 1])
    if "--include" in args: include_re = re.compile(args[args.index("--include") + 1], re.I)

    shop_host = urllib.parse.urlsplit(url).netloc

    # --- language check (before downloads) ---
    html_s, site_lang, fr_alternate = "", "", ""
    js_imgs, js_media = [], []
    try:
        body, _ = http_get(url.rstrip("/") + ".js")
        d = json.loads(body)
        js_imgs = d.get("images", [])
        js_media = [m.get("src") for m in d.get("media", []) if m.get("media_type") == "image"]
    except Exception as e:
        print(f"[warn] product.js unavailable: {e}", file=sys.stderr)
    try:
        html, _ = http_get(url)
        html_s = html.decode("utf-8", "replace")
    except Exception as e:
        print(f"[warn] html fetch failed: {e}", file=sys.stderr)
    m = re.search(r'<html[^>]+lang="([^"]+)"', html_s)
    site_lang = (m.group(1) if m else "").lower()
    for hreflang, href in re.findall(r'<link[^>]+hreflang="([^"]+)"[^>]+href="([^"]+)"', html_s):
        if hreflang.lower().startswith("fr") and not fr_alternate:
            fr_alternate = href

    def absurl(u):
        u = u.strip()
        return ("https:" + u) if u.startswith("//") else u

    pool_gallery, pool_page = {}, {}

    def add(pool, u, source):
        u = absurl(u)
        if not re.match(r"https?://", u):
            return
        pool.setdefault(canonical(u), {"url": u, "sources": []})["sources"].append(source)

    if "gallery" in pools:
        for u in js_imgs: add(pool_gallery, u, "product.js images")
        for u in js_media: add(pool_gallery, u, "product.js media")
        for mm in re.findall(r'<meta[^>]+property="og:image(?::\w+)?"[^>]+content="([^"]+)"', html_s):
            add(pool_gallery, mm, "og:image")
    if "page" in pools:
        for mm in re.findall(r'<img[^>]+(?:src|data-src)="([^"]+)"', html_s):
            add(pool_page, mm, "img tag")

    def first_party(u):
        host = urllib.parse.urlsplit(u).netloc.lower()
        return host in FIRST_PARTY_EXTRA or host == shop_host or host.endswith("." + shop_host)

    def evaluate(pool):
        kept, rejected = [], []
        for item in pool.values():
            u = item["url"]
            path = urllib.parse.urlsplit(u).path
            ext = os.path.splitext(path)[1].lower()
            name = urllib.parse.unquote(os.path.basename(path))
            if include_re and not include_re.search(u):
                rejected.append({**item, "why": "not matched by --include"}); continue
            if ext not in KEEP_EXT:
                rejected.append({**item, "why": f"format {ext or 'none'}"}); continue
            if DENY_NAME.search(name):
                rejected.append({**item, "why": "filename denylist"}); continue
            if not first_party(u):
                rejected.append({**item, "why": "third-party host"}); continue
            kept.append(item)
        return kept, rejected

    k_gal, r_gal = evaluate(pool_gallery)
    k_page, r_page = evaluate(pool_page)

    result, seen_hash, seen_base = [], {}, set()
    for item in k_gal + k_page:
        rec = dict(item)
        try:
            data, ctype = http_get(item["url"], timeout=30)
        except Exception as e:
            rec.update(kept=False, why=f"download failed: {e}")
            result.append(rec); continue
        dims = sniff_dims(data)
        w, h = dims or (0, 0)
        rec.update(bytes=len(data), content_type=ctype, w=w, h=h,
                   sha1=hashlib.sha1(data).hexdigest()[:12])
        base = canon_basename(item["url"])
        if not dims:
            rec.update(kept=False, why="unreadable dims / not an image")
        elif w < MIN_WIDTH or min(w, h) < MIN_SIDE:
            rec.update(kept=False, why=f"too small {w}x{h}")
        elif not (AR_MIN <= w / h <= AR_MAX):
            rec.update(kept=False, why=f"aspect ratio {w/h:.2f}")
        elif rec["sha1"] in seen_hash:
            rec.update(kept=False, why=f"duplicate of {seen_hash[rec['sha1']]}")
        elif base in seen_base:
            rec.update(kept=False, why=f"duplicate filename {base}")
        else:
            seen_hash[rec["sha1"]] = item["url"]
            seen_base.add(base)
            rec["kept"] = True
        result.append(rec)

    for rec in r_gal + r_page:
        result.append({**rec, "kept": False})

    kept = [r for r in result if r.get("kept")][:max_images]
    summary = {
        "product_url": url,
        "site_lang": site_lang,
        "fr_alternate": fr_alternate,
        "needs_image_translation": not (site_lang.startswith("fr") or fr_alternate),
        "candidates": {"gallery": len(pool_gallery), "page": len(pool_page)},
        "kept": kept,
        "rejected": [r for r in result if not r.get("kept")],
    }
    print(f"site_lang={site_lang or '?'} fr_alternate={fr_alternate or '-'} "
          f"needs_image_translation={summary['needs_image_translation']}")
    print(f"candidates: gallery={len(pool_gallery)} page={len(pool_page)}  KEPT {len(kept)} (cap {max_images})")
    for r in kept:
        print(f"  + {r['w']}x{r['h']} {r['url']}  <- {','.join(r['sources'])}")
    print("REJECTED:")
    for r in summary["rejected"]:
        print(f"  - [{r['why']}] {r['url'][:110]}")
    if out_path:
        json.dump(summary, open(out_path, "w"), ensure_ascii=False, indent=1)
        print(f"manifest -> {out_path}")


if __name__ == "__main__":
    main()
