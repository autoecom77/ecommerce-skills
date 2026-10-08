#!/usr/bin/env python3
"""Duplicate the store's MAIN Shopify theme, named after the product.

Usage:
  python3 theme_dup.py --name "<Nom FR du produit>" [--source-id GID]
      [--source-name NAME] [--store aadunp-f2.myshopify.com] [--wait 180]

Idempotent: a theme with exactly this name is reused (reused=true), never
duplicated twice. The copy stays UNPUBLISHED. Polls `processing` to false
before printing so the theme is immediately writable/deletable.
"""
import argparse, json, os, subprocess, sys, tempfile, time

STORE_DEFAULT = "aadunp-f2.myshopify.com"
SHOPIFY = os.path.expanduser("~/.local/bin/shopify")


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


def wait_ready(store, theme_id, wait_s):
    """Poll theme.processing until false. Returns seconds waited."""
    if wait_s <= 0:
        return 0
    t0 = time.time()
    while time.time() - t0 < wait_s:
        data = gql(store,
                   "query($id: ID!) { theme(id: $id) { processing processingFailed } }",
                   {"id": theme_id})
        t = data.get("theme") or {}
        if not t.get("processing"):
            if t.get("processingFailed"):
                print(json.dumps({"warning": "theme processingFailed=true"}), file=sys.stderr)
            return int(time.time() - t0)
        time.sleep(5)
    print(json.dumps({"warning": "theme still processing after --wait"}), file=sys.stderr)
    return int(time.time() - t0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="theme name = titre FR du produit")
    ap.add_argument("--source-id", help="theme GID to duplicate (default: role MAIN)")
    ap.add_argument("--source-name", help="theme name to duplicate (default: role MAIN)")
    ap.add_argument("--store", default=STORE_DEFAULT)
    ap.add_argument("--wait", type=int, default=180, help="max seconds to wait for processing")
    args = ap.parse_args()

    data = gql(args.store,
               "query { themes(first: 50) { nodes { id name role processing } } }")
    themes = (data.get("themes") or {}).get("nodes") or []

    existing = next((t for t in themes if t["name"] == args.name), None)
    if existing:
        waited = wait_ready(args.store, existing["id"], args.wait)
        print(json.dumps({"reused": True, "id": existing["id"], "name": existing["name"],
                          "role": existing["role"], "processing_waited_s": waited},
                         ensure_ascii=False, indent=1))
        return

    if args.source_id:
        source = next((t for t in themes if t["id"] == args.source_id), None)
        if source is None:
            source = {"id": args.source_id, "name": "(by id)", "role": "?"}
    elif args.source_name:
        source = next((t for t in themes if t["name"] == args.source_name), None)
    else:
        source = next((t for t in themes if t.get("role") == "MAIN"), None)
    if not source:
        print(json.dumps({"error": "no source theme found",
                          "available": [{"name": t["name"], "role": t["role"]} for t in themes]},
                         ensure_ascii=False))
        sys.exit(4)

    q = ("mutation($id: ID!, $name: String) { themeDuplicate(id: $id, name: $name) "
         "{ newTheme { id name role } userErrors { field message } } }")
    data = gql(args.store, q, {"id": source["id"], "name": args.name}, mutate=True)
    td = data.get("themeDuplicate") or {}
    errs = td.get("userErrors") or []
    theme = td.get("newTheme")
    if errs or not theme:
        print(json.dumps({"error": "themeDuplicate failed", "userErrors": errs},
                         ensure_ascii=False))
        sys.exit(5)

    waited = wait_ready(args.store, theme["id"], args.wait)
    print(json.dumps({"reused": False, "id": theme["id"], "name": theme["name"],
                      "role": theme["role"],
                      "source": {"id": source["id"], "name": source["name"],
                                 "role": source["role"]},
                      "processing_waited_s": waited}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
