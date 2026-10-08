#!/usr/bin/env python3
"""Push a local build/ directory into a Shopify theme, then bind the product template.

Usage:
  python3 theme_push.py --theme-id GID --dir build/ [--bind-product HANDLE|GID]
      [--suffix <suffix>] [--store aadunp-f2.myshopify.com] [--dry-run]

File order is enforced: sections/* first (JSON templates are validated against
existing section files), then templates/*, then everything else (assets/...).
Text files upload as TEXT, binary as BASE64, in batches of 10. Every pushed file
is read back afterwards. --bind-product sets product.templateSuffix to <suffix>,
which must match templates/product.<suffix>.json in the pushed set.
"""
import argparse, base64, json, os, subprocess, sys, tempfile

STORE_DEFAULT = "aadunp-f2.myshopify.com"
SHOPIFY = os.path.expanduser("~/.local/bin/shopify")
TEXT_EXT = {".liquid", ".json", ".js", ".css", ".scss", ".svg", ".html", ".txt", ".md", ".yml", ".yaml"}
BATCH = 10


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
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    out = r.stdout
    start = out.find("{")
    if start < 0:
        print(json.dumps({"error": "shopify CLI failed", "stdout": out[:1500],
                          "stderr": r.stderr[:800]}))
        sys.exit(2)
    data = json.loads(out[start:])
    if "errors" in data:
        print(json.dumps({"error": "GraphQL errors", "raw": data["errors"][:5]}))
        sys.exit(3)
    return data


def collect_files(root):
    """[{filename, path, body_type, value, size}] in push order: sections, templates, rest."""
    files = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            ext = os.path.splitext(fn)[1].lower()
            raw = open(full, "rb").read()
            if ext in TEXT_EXT:
                body_type, value = "TEXT", raw.decode("utf-8")
            else:
                body_type, value = "BASE64", base64.b64encode(raw).decode("ascii")
            files.append({"filename": rel, "path": full, "body_type": body_type,
                          "value": value, "size": len(raw)})

    def rank(f):
        if f["filename"].startswith("sections/"):
            return 0
        if f["filename"].startswith("templates/"):
            return 1
        return 2

    return sorted(files, key=lambda f: (rank(f), f["filename"]))


def resolve_product(store, ref):
    if ref.startswith("gid://"):
        q = ("query($id: ID!) { product(id: $id) { id title handle templateSuffix } }")
        data = gql(store, q, {"id": ref})
        prod = data.get("product")
    else:
        q = "query($q: String!) { products(first: 5, query: $q) { nodes { id title handle templateSuffix } } }"
        data = gql(store, q, {"q": f"handle:'{ref}'"})
        nodes = (data.get("products") or {}).get("nodes") or []
        if not nodes:
            data = gql(store, q, {"q": f"title:'{ref}'"})
            nodes = (data.get("products") or {}).get("nodes") or []
        prod = nodes[0] if nodes else None
    if not prod:
        print(json.dumps({"error": "product not found", "ref": ref}))
        sys.exit(6)
    return prod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--theme-id", required=True)
    ap.add_argument("--dir", required=True, help="local build dir (sections/ templates/ assets/)")
    ap.add_argument("--bind-product", help="product handle or GID")
    ap.add_argument("--suffix", help="template suffix, e.g. pdp-<handle>")
    ap.add_argument("--store", default=STORE_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = collect_files(args.dir)
    if not files:
        print(json.dumps({"error": "no files found", "dir": args.dir}))
        sys.exit(2)

    # suffix sanity: templates/product.<suffix>.json must be in the pushed set
    if args.suffix:
        expect = f"templates/product.{args.suffix}.json"
        if expect not in [f["filename"] for f in files]:
            print(json.dumps({"error": f"--suffix {args.suffix} needs {expect} in the push set",
                              "pushed": [f["filename"] for f in files]}))
            sys.exit(7)

    summary = {"theme_id": args.theme_id, "planned": [f["filename"] for f in files],
               "order_note": "sections first, then templates, then rest"}
    if args.dry_run:
        summary["dry_run"] = True
        if args.bind_product:
            summary["bind_planned"] = {"product": args.bind_product, "suffix": args.suffix}
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return

    upserted, failed = [], []
    for i in range(0, len(files), BATCH):
        batch = files[i:i + BATCH]
        q = ("mutation($themeId: ID!, $files: [OnlineStoreThemeFilesUpsertFileInput!]!) { "
             "themeFilesUpsert(themeId: $themeId, files: $files) { "
             "upsertedThemeFiles { filename } userErrors { field message } } }")
        variables = {"themeId": args.theme_id,
                     "files": [{"filename": f["filename"],
                                "body": {"type": f["body_type"], "value": f["value"]}}
                               for f in batch]}
        data = gql(args.store, q, variables, mutate=True)
        pu = data.get("themeFilesUpsert") or {}
        upserted += [x["filename"] for x in (pu.get("upsertedThemeFiles") or [])]
        for e in (pu.get("userErrors") or []):
            failed.append({"field": e.get("field"), "message": e.get("message")})

    # read-back verification (always filter by filenames: the unfiltered files()
    # connection is capped/paginated and silently drops later pages)
    names = [f["filename"] for f in files]
    remote = {}
    for i in range(0, len(names), 20):
        chunk = names[i:i + 20]
        q = ("query($id: ID!) { theme(id: $id) { "
             "files(filenames: [%s], first: %d) { nodes { filename size } } } }"
             % (", ".join('"%s"' % n for n in chunk), len(chunk)))
        data = gql(args.store, q, {"id": args.theme_id})
        for n in ((data.get("theme") or {}).get("files") or {}).get("nodes") or []:
            remote[n["filename"]] = n
    missing = [n for n in names if n not in remote]
    local_size = {f["filename"]: f["size"] for f in files}
    size_mismatch = [n for n in names if n in remote and str(remote[n].get("size")) != str(local_size[n])]
    summary.update({
        "upserted": upserted,
        "user_errors": failed,
        "readback_missing": missing,
        "readback_size_mismatch": size_mismatch,
        "readback_sizes": {n: remote.get(n, {}).get("size") for n in names if n in remote},
    })

    if args.bind_product:
        if not args.suffix:
            print(json.dumps({"error": "--bind-product requires --suffix"}))
            sys.exit(7)
        prod = resolve_product(args.store, args.bind_product)
        q = ("mutation($product: ProductUpdateInput!) { "
             "productUpdate(product: $product) { "
             "product { id handle templateSuffix } userErrors { field message } } }")
        data = gql(args.store, q,
                   {"product": {"id": prod["id"], "templateSuffix": args.suffix}},
                   mutate=True)
        pu = data.get("productUpdate") or {}
        summary["bind"] = {"product": pu.get("product"), "user_errors": pu.get("userErrors")}

    summary["ok"] = not failed and not missing and not size_mismatch
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if failed or missing:
        sys.exit(8)


if __name__ == "__main__":
    main()
