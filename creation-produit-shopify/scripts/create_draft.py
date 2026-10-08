#!/usr/bin/env python3
"""Create a Shopify DRAFT product from an image manifest + source options.

Usage:
  python3 create_draft.py --manifest manifest.json --title "Titre FR" \
      --options-from "https://shop/products/x" --price 39.90 [--stock 500] \
      [--store aadunp-f2.myshopify.com] [--dry-run]

One `productSet(synchronous: true)` mutation: status DRAFT, productOptions
(colors/sizes/materials from the source product.js), every option combination
as a variant with price and inventory quantity 500 at the active location, and
the kept images as product files. Local files (translated images) go through a staged
upload first. Refuses to run if a product with the same title already exists.
"""
import argparse, json, os, subprocess, sys, tempfile, urllib.request, uuid
from itertools import product as cartesian

STORE_DEFAULT = "aadunp-f2.myshopify.com"
SHOPIFY = os.path.expanduser("~/.local/bin/shopify")


def sh(args, timeout=300):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def gql(store, query, variables=None, mutate=False, timeout=300):
    with tempfile.NamedTemporaryFile("w", suffix=".graphql", delete=False) as q:
        q.write(query); qpath = q.name
    args = [SHOPIFY, "store", "execute", "-s", store, "-j", "--query-file", qpath]
    if mutate:
        args.append("--allow-mutations")
    if variables is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as v:
            json.dump(variables, v); vpath = v.name
        args += ["--variable-file", vpath]
    code, out, err = sh(args, timeout=timeout)
    try:
        data = json.loads(out)
    except Exception:
        print(f"[shopify CLI] exit={code}\nstdout: {out[:2000]}\nstderr: {err[:2000]}", file=sys.stderr)
        sys.exit(2)
    return data


def fetch_source_options(product_url):
    """Options + variants from the source shop's product.js endpoint."""
    UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    req = urllib.request.Request(product_url.rstrip("/") + ".js", headers={
        "User-Agent": UA, "Accept": "application/json,text/*;q=0.9",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    return [{"name": o["name"], "values": o["values"]}
            for o in d.get("options", []) if o.get("values")]


def upload_local_file(store, path, filename, mime="image/png"):  # staged upload
    q = ("mutation($input: [StagedUploadInput!]!) { stagedUploadsCreate(input: $input) "
         "{ stagedTargets { url resourceUrl parameters { name value } } userErrors { field message } } }")
    data = gql(store, q, {"input": [{"resource": "IMAGE", "filename": filename,
                                     "mimeType": mime, "httpMethod": "POST"}]}, mutate=True)
    targets = (data.get("stagedUploadsCreate") or {}).get("stagedTargets") or []
    if not targets:
        raise RuntimeError(f"stagedUploadsCreate failed: {json.dumps(data)[:500]}")
    t = targets[0]
    boundary = uuid.uuid4().hex
    body = b""
    for p in t["parameters"]:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{p['name']}\"\r\n\r\n{p['value']}\r\n").encode()
    with open(path, "rb") as f:
        raw = f.read()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
             f"Content-Type: {mime}\r\n\r\n").encode() + raw + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(t["url"], data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        if r.status not in (200, 201):
            raise RuntimeError(f"staged upload failed: HTTP {r.status}")
    return t["resourceUrl"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--options-from", help="source product URL (reads its .js options)")
    ap.add_argument("--options-json", help="inline options JSON: [{name, values: [...]}]")
    ap.add_argument("--stock", type=int, default=500)
    ap.add_argument("--price", required=True,
                    help="variant price, e.g. 39.90 (Sheet col J = Prix)")
    ap.add_argument("--store", default=STORE_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # normalize price: accept "39.90", "39,90", "39.90€"
    raw = str(args.price).strip().replace("€", "").replace("EUR", "").replace(" ", "").replace(",", ".")
    try:
        price_val = float(raw)
        if price_val <= 0:
            raise ValueError
    except ValueError:
        print(f"error: invalid --price {args.price!r} (expected e.g. 39.90)", file=sys.stderr)
        sys.exit(2)
    price = f"{price_val:.2f}"

    manifest = json.load(open(args.manifest))
    kept = manifest["kept"]
    if args.options_from:
        options = fetch_source_options(args.options_from)
    else:
        options = json.loads(args.options_json or "[]")
    if not options:
        print("error: no options (colors/sizes/materials) resolved", file=sys.stderr)
        sys.exit(2)

    # idempotence: abort if title exists
    q = "query($q: String!) { products(first: 5, query: $q) { nodes { id title status } } }"
    data = gql(args.store, q, {"q": f"title:'{args.title}'"})
    existing = (data.get("products") or {}).get("nodes") or []
    if existing:
        print(json.dumps({"error": "product with this title already exists",
                          "existing": existing}, ensure_ascii=False))
        sys.exit(3)

    # location for inventory
    data = gql(args.store, "query { locations(first: 10) { nodes { id name isActive } } }")
    locs = (data.get("locations") or {}).get("nodes") or []
    loc = next((l for l in locs if l.get("isActive")), None) or (locs[0] if locs else None)
    if not loc:
        print("error: no inventory location found", file=sys.stderr)
        sys.exit(2)

    # files
    files = []
    for i, img in enumerate(kept):
        if img.get("local_path"):
            src = upload_local_file(args.store, img["local_path"],
                                    os.path.basename(img["local_path"]))
        else:
            src = img["url"]
        files.append({"originalSource": src, "contentType": "IMAGE",
                      "alt": img.get("alt") or f"{args.title} {i + 1}"})

    # variants: every option combination, stock each
    names = [o["name"] for o in options]
    values = [o["values"] for o in options]
    variants = []
    for combo in cartesian(*values):
        v = {
            "optionValues": [{"name": val, "optionName": n}
                             for n, val in zip(names, combo)],
            "inventoryQuantities": [{"locationId": loc["id"], "quantity": args.stock,
                                     "name": "available"}],
            "price": price,
        }
        variants.append(v)

    product_input = {
        "title": args.title,
        "status": "DRAFT",
        "productOptions": [{"name": n, "position": i + 1,
                            "values": [{"name": v} for v in vals]}
                           for i, (n, vals) in enumerate(zip(names, values))],
        "variants": variants,
        "files": files,
    }

    if args.dry_run:
        print(json.dumps({"dry_run": True, "location": loc,
                          "options": options, "variant_count": len(variants),
                          "file_count": len(files), "stock": args.stock,
                          "price": price},
                         ensure_ascii=False, indent=1))
        return

    q = ("mutation($input: ProductSetInput!) { "
         "productSet(synchronous: true, input: $input) { "
         "product { id title handle status "
         "mediaCount { count } "
         "options { name values } "
         "variants(first: 250) { nodes { id displayName inventoryQuantity price } } } "
         "userErrors { field message } } }")
    data = gql(args.store, q, {"input": product_input}, mutate=True, timeout=600)
    ps = data.get("productSet") or {}
    errs = ps.get("userErrors") or []
    prod = ps.get("product")
    if errs or not prod:
        print(json.dumps({"error": "productSet failed", "userErrors": errs,
                          "raw": json.dumps(data)[:1500]}, ensure_ascii=False))
        sys.exit(4)

    vs = (prod.get("variants") or {}).get("nodes") or []
    bad_stock = [v for v in vs if v.get("inventoryQuantity") not in (None, args.stock)]
    bad_price = []
    for v in vs:
        # price is a scalar Money string in API >= 2025-10; dict {amount} before
        p = v.get("price")
        amt = p.get("amount") if isinstance(p, dict) else p
        try:
            if amt is None or abs(float(amt) - price_val) > 0.005:
                bad_price.append({"id": v.get("id"), "price": amt})
        except (TypeError, ValueError):
            bad_price.append({"id": v.get("id"), "price": amt})
    result = {
        "created": True,
        "id": prod["id"], "title": prod["title"], "status": prod["status"],
        "handle": prod.get("handle"),
        "media_count": (prod.get("mediaCount") or {}).get("count"),
        "variants_expected": len(variants), "variants_returned": len(vs),
        "stock_ok": not bad_stock, "stock_mismatches": bad_stock[:5],
        "price_expected": price, "price_ok": not bad_price,
        "price_mismatches": bad_price[:5],
        "admin_url": f"https://admin.shopify.com/store/{args.store.replace('.myshopify.com','')}/products/{prod['id'].split('/')[-1]}",
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
