#!/usr/bin/env python3
"""send_recap.py — Build and send the TrendTrack product research email report.

Aggregates the conversion funnel (search → dedup → price → COGS → X → Sheet),
the TrendTrack credits consumed, the near-misses and the Top 3 products from the
run artifacts, and sends the recap email via the Google Workspace CLI.

Usage:
    python3 send_recap.py --send [--date DD/MM/YYYY]
    python3 send_recap.py --dry-run
"""

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_TO = "autoecom77@gmail.com"
SHEET_LINK = "https://docs.google.com/spreadsheets/d/1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM/edit?gid=815136391"
GAPI_PYTHON = Path(os.environ.get("GAPI_PYTHON", Path.home() / ".hermes/venvs/gws/bin/python"))
GAPI_SCRIPT = Path(os.environ.get("GAPI_SCRIPT", Path.home() / ".hermes/skills/productivity/google-workspace/scripts/google_api.py"))
RUN_CREDIT_CAP = 1200

PATHS: Dict[str, str] = {
    "query_stats": "/tmp/tt_query_stats.json",
    "raw": "/tmp/tt_candidates_raw.json",
    "filtered": "/tmp/tt_candidates_filtered.json",
    "with_prices": "/tmp/tt_candidates_with_prices.json",
    "with_cogs": "/tmp/tt_candidates_with_cogs.json",
    "kept": "/tmp/tt_candidates_kept.json",
    "enrich_stats": "/tmp/tt_enrich_stats.json",
    "products": "/tmp/tt_run/final_products.json",
    "near_misses": "/tmp/tt_near_misses.json",
    "sheet_result": "/tmp/tt_run/sheet_write_result.json",
    "usage_start": "/tmp/tt_usage_start.json",
    "usage_end": "/tmp/tt_usage_end.json",
}

METHOD_LABELS = {"shopify_reach_growth": "M1 reach growth", "volume_live": "M2 volume live"}
COGS_LABELS = {
    "apify_1688": "1688 image",
    "aliexpress_image": "AliExpress image",
    "apify_1688_keyword": "1688 mot-clé",
    "heuristique": "heuristique (estimé)",
}


def load_json_safe(path: str, default: Any = None) -> Any:
    p = Path(path)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _remaining(u: Dict[str, Any]) -> int:
    try:
        v = (u.get("credits") or {}).get("totalRemaining")
        if v is None:
            v = (u.get("includedQuota") or {}).get("remaining")
        return int(v) if v is not None else -1
    except Exception:
        return -1


def product_name(p: Dict[str, Any]) -> str:
    """Same precedence as Sheet column B (assemble_rows.py)."""
    return str(p.get("title_fr") or p.get("product_title") or p.get("title") or p.get("shop_domain") or "Produit")


def compute_funnel(paths: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Funnel + credits numbers from the run artifacts (missing artifacts → None)."""
    paths = {**PATHS, **(paths or {})}
    q = load_json_safe(paths["query_stats"], {}) or {}
    raw = load_json_safe(paths["raw"], None)
    filtered = load_json_safe(paths["filtered"], None)
    with_prices = load_json_safe(paths["with_prices"], None)
    with_cogs = load_json_safe(paths["with_cogs"], None)
    kept = load_json_safe(paths["kept"], None)
    products = load_json_safe(paths["products"], []) or []
    near = load_json_safe(paths["near_misses"], []) or []
    sheet = load_json_safe(paths["sheet_result"], {}) or {}
    enr = load_json_safe(paths["enrich_stats"], {}) or {}
    u0 = load_json_safe(paths["usage_start"], {}) or {}
    u1 = load_json_safe(paths["usage_end"], {}) or {}

    f: Dict[str, Any] = {
        "methods": q.get("methods") or [],
        "search_budget": q.get("budget"),
        "search_budget_hit": bool(q.get("budget_hit")),
        "raw_ads": q.get("raw_ads"),
        "unique_shops": len(raw) if isinstance(raw, list) else q.get("unique_shops"),
        "after_sheet_dedup": len(filtered) if isinstance(filtered, list) else None,
    }
    if isinstance(with_prices, list):
        prices = [c.get("price_eur") for c in with_prices]
        valid = [float(p) for p in prices if p is not None]
        f["priced"] = len(valid)
        f["no_price"] = len(prices) - len(valid)
        f["near_miss"] = sum(1 for p in valid if 29.90 <= p <= 29.99)
        f["below_30"] = sum(1 for p in valid if p < 30.0 and not (29.90 <= p <= 29.99))
        f["price_ok"] = sum(1 for p in valid if p >= 30.0)
    if isinstance(with_cogs, list):
        searched = [c for c in with_cogs if c.get("price_eur") is not None]
        f["cogs_by_method"] = dict(Counter(str(c.get("cogs_method") or c.get("cogs_source") or "?") for c in searched))
    f["x_ok"] = len(kept) if isinstance(kept, list) else None
    if isinstance(f.get("price_ok"), int) and isinstance(f.get("x_ok"), int):
        f["x_rejected"] = f["price_ok"] - f["x_ok"]
    f["added"] = int(sheet.get("written") or 0) if sheet else len(products)
    f["sheet_start"], f["sheet_end"] = sheet.get("start"), sheet.get("end")
    f["dry_run"] = bool(sheet.get("dry_run"))
    f["near_misses"] = near
    rem0, rem1 = _remaining(u0), _remaining(u1)
    f["credits_start"], f["credits_end"] = rem0, rem1
    f["credits_used"] = (rem0 - rem1) if rem0 >= 0 and rem1 >= 0 else None
    f["credits_search"] = q.get("credits_spent")
    f["credits_enrich"] = enr.get("credits_spent")
    f["enrich"] = enr
    return f


def _fmt(v: Any) -> str:
    return "?" if v is None else str(v)


def build_email_content(date_str: str = "", paths: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Compile email subject and body from run artifacts."""
    paths = {**PATHS, **(paths or {})}
    today = date_str or datetime.now().strftime("%d/%m/%Y")
    f = compute_funnel(paths)
    products: List[Dict[str, Any]] = load_json_safe(paths["products"], []) or []

    subject = f"Recherche produit TrendTrack — {today}" + (" [DRY RUN]" if f["dry_run"] else "")
    lines: List[str] = [f"Recherche produit TrendTrack — {today}", ""]

    if f["added"]:
        lines.append(
            f"► {f['added']} produits ajoutés au Sheet (onglet Products, lignes {f['sheet_start']}–{f['sheet_end']})"
            + (" — DRY RUN, rien n'a été écrit" if f["dry_run"] else "") + "."
        )
        lines.append("► Triés par coefficient X décroissant (meilleure rentabilité en tête).")
    else:
        lines.append("► Aucun produit n'a passé tous les filtres ce run — rien n'a été ajouté au Sheet.")
    lines.append("")

    lines.append("ENTONNOIR DE CONVERSION")
    if f["methods"]:
        parts = []
        for m in f["methods"]:
            lbl = METHOD_LABELS.get(m.get("tag"), m.get("tag"))
            txt = f"{lbl} {m.get('pages_fetched', 0)} page(s) / {m.get('ads_received', 0)} ads"
            if m.get("error"):
                txt += " (erreur API)"
            parts.append(txt)
        lines.append("• Recherche TrendTrack : " + " · ".join(parts)
                     + (f" → {f['raw_ads']} ads exploitables" if f.get("raw_ads") is not None else ""))
        if f["search_budget_hit"]:
            lines.append(f"  (pagination arrêtée par le budget crédits recherche : {f['search_budget']})")
    lines.append(f"• Boutiques uniques (dédup domaine) : {_fmt(f.get('unique_shops'))}")
    lines.append(f"• Après dédoublonnage avec le Sheet : {_fmt(f.get('after_sheet_dedup'))}")
    if "priced" in f:
        lines.append(f"• Prix de vente extraits : {f['priced']} ({f['no_price']} sans prix détectable)")
        lines.append(f"• Prix ≥ 30 € : {f['price_ok']} ({f['below_30']} sous 30 €, {f['near_miss']} quasi-miss 29,90–29,99 €)")
    if f.get("cogs_by_method"):
        lines.append("• COGS : " + ", ".join(
            f"{n} {COGS_LABELS.get(k, k)}" for k, n in sorted(f["cogs_by_method"].items(), key=lambda kv: -kv[1])))
    if f.get("x_ok") is not None:
        lines.append(f"• Coefficient X validé : {f['x_ok']}"
                     + (f" ({f['x_rejected']} rejetés)" if f.get("x_rejected") is not None else ""))
    lines.append(f"• Ajoutés au Sheet : {f['added']}")
    lines.append("")

    if products:
        lines.append("TOP 3 DES PRODUITS RETENUS (par coefficient X)")
        top_products = sorted(products, key=lambda c: float(c.get("x_coefficient") or 0), reverse=True)[:3]
        for i, p in enumerate(top_products, start=1):
            meth = COGS_LABELS.get(str(p.get("cogs_method") or ""), p.get("cogs_source") or "")
            lines.append(
                f"{i}. {product_name(p)} ({p.get('shop_domain', '')}) — "
                f"Prix {float(p.get('price_eur') or 0):.2f} € — "
                f"COGS {float(p.get('cogs_eur') or 0):.2f} €" + (f" ({meth})" if meth else "") + " — "
                f"X {float(p.get('x_coefficient') or 0):.2f} — "
                f"{p.get('product_url') or p.get('landing_url') or ''}"
            )
        lines.append("")

        lines.append("TOUS LES PRODUITS AJOUTÉS (Prix / COGS / X)")
        for p in products:
            est = " [COGS estimé]" if p.get("cogs_method") == "heuristique" or p.get("cogs_source") == "heuristique" else ""
            lines.append(
                f"- {product_name(p)} ({p.get('shop_domain', '')}) : "
                f"{float(p.get('price_eur') or 0):.2f} € / {float(p.get('cogs_eur') or 0):.2f} € / "
                f"X {float(p.get('x_coefficient') or 0):.2f}{est}"
            )
        lines.append("")

    near = f["near_misses"]
    if near:
        lines.append(f"QUASI-MISS EXCLUS (29,90–29,99 €) : {len(near)}")
        for nm in near[:10]:
            lines.append(f"• {nm}")
    else:
        lines.append("QUASI-MISS EXCLUS (29,90–29,99 €) : aucun")
    lines.append("")

    lines.append("CRÉDITS TRENDTRACK")
    if f["credits_used"] is not None:
        detail = []
        if f.get("credits_search") is not None:
            detail.append(f"recherche {f['credits_search']}")
        if f.get("credits_enrich") is not None:
            detail.append(f"enrichissement {f['credits_enrich']}")
        lines.append(f"• Consommation : {f['credits_used']} crédits"
                     + (f" ({' · '.join(detail)})" if detail else "") + f" — plafond {RUN_CREDIT_CAP}/run")
    if f["credits_end"] >= 0:
        lines.append(f"• Solde restant : {f['credits_end']} crédits")
    enr = f.get("enrich") or {}
    if enr.get("skipped_budget"):
        lines.append(f"• {enr['skipped_budget']} boutique(s) non enrichie(s) (budget atteint) — seule l'ad source est listée")
    lines.append("")
    lines.append(f"Lien direct vers le Google Sheet : {SHEET_LINK}")

    return {"subject": subject, "body": "\n".join(lines)}


def send_email_gapi(to: str, subject: str, body: str) -> None:
    """Send email using google_api.py."""
    if not GAPI_PYTHON.exists() or not GAPI_SCRIPT.exists():
        raise SystemExit(f"Google API script not found at {GAPI_SCRIPT}")

    cmd = [
        str(GAPI_PYTHON),
        str(GAPI_SCRIPT),
        "gmail",
        "send",
        "--to", to,
        "--subject", subject,
        "--body", body,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise SystemExit(f"Failed to send email: {res.stderr}")
    print(f"[send_recap] ✓ Email successfully sent to {to}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send TrendTrack recap email.")
    parser.add_argument("--to", default=DEFAULT_TO, help="Recipient email address")
    parser.add_argument("--send", action="store_true", help="Actually send the email via Gmail API")
    parser.add_argument("--dry-run", action="store_true", help="Print email body without sending")
    parser.add_argument("--date", default="", help="Custom date string (DD/MM/YYYY)")
    args = parser.parse_args()

    content = build_email_content(date_str=args.date)

    if args.send:
        send_email_gapi(args.to, content["subject"], content["body"])
    else:
        print("=== EMAIL SUBJECT ===")
        print(content["subject"])
        print("\n=== EMAIL BODY ===")
        print(content["body"])


if __name__ == "__main__":
    main()
