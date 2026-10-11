#!/usr/bin/env python3
"""send_recap.py — Build and send the TrendTrack product research email report.

Aggregates funnel numbers, credits consumed, near-misses, and the Top 3 products,
and sends the recap email via the Google Workspace CLI.

Usage:
    python3 send_recap.py --send
    python3 send_recap.py --dry-run
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_TO = "autoecom77@gmail.com"
SHEET_LINK = "https://docs.google.com/spreadsheets/d/1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM/edit?gid=815136391"
GAPI_PYTHON = Path(os.environ.get("GAPI_PYTHON", Path.home() / ".hermes/venvs/gws/bin/python"))
GAPI_SCRIPT = Path(os.environ.get("GAPI_SCRIPT", Path.home() / ".hermes/skills/productivity/google-workspace/scripts/google_api.py"))


def load_json_safe(path: str, default: Any = None) -> Any:
    p = Path(path)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def build_email_content(
    products_path: str = "/tmp/tt_run/final_products.json",
    near_misses_path: str = "/tmp/tt_near_misses.json",
    sheet_result_path: str = "/tmp/tt_run/sheet_write_result.json",
    usage_start_path: str = "/tmp/tt_usage_start.json",
    usage_end_path: str = "/tmp/tt_usage_end.json",
    date_str: str = "",
) -> Dict[str, str]:
    """Compile email subject and body from run artifacts."""
    today = date_str or datetime.now().strftime("%d/%m/%Y")
    products: List[Dict[str, Any]] = load_json_safe(products_path, [])
    near_misses: List[str] = load_json_safe(near_misses_path, [])
    sheet_res: Dict[str, Any] = load_json_safe(sheet_result_path, {})
    u0: Dict[str, Any] = load_json_safe(usage_start_path, {})
    u1: Dict[str, Any] = load_json_safe(usage_end_path, {})

    # Calculate credits used
    credits_used = 0
    credits_rem = -1
    try:
        rem0 = u0.get("credits", {}).get("totalRemaining") or u0.get("includedQuota", {}).get("remaining", 0)
        rem1 = u1.get("credits", {}).get("totalRemaining") or u1.get("includedQuota", {}).get("remaining", 0)
        credits_used = max(0, int(rem0) - int(rem1))
        credits_rem = int(rem1)
    except Exception:
        pass

    subject = f"Recherche produit TrendTrack — {today}"

    lines = []
    lines.append(f"Recherche produit TrendTrack — {today}")
    lines.append("")

    start_r = sheet_res.get("start", "?")
    end_r = sheet_res.get("end", "?")
    lines.append(f"► {len(products)} produits ajoutés au Sheet (onglet Products, lignes {start_r}–{end_r}).")
    lines.append(f"► Triés directement par coefficient X décroissant (meilleure rentabilité en tête).")
    lines.append("")

    lines.append("TOP 3 DES PRODUITS RETENUS (par coefficient X)")
    top_products = sorted(products, key=lambda c: float(c.get("x_coefficient", 0)), reverse=True)[:3]
    for i, p in enumerate(top_products, start=1):
        cogs_note = f" (COGS {p.get('cogs_source', '1688')})" if p.get("cogs_source") else ""
        lines.append(
            f"{i}. {p.get('title', p.get('product_title', 'Produit'))} — "
            f"Prix {float(p.get('price_eur', 0)):.2f} € — "
            f"COGS {float(p.get('cogs_eur', 0)):.2f} € — "
            f"X {float(p.get('x_coefficient', 0)):.2f}{cogs_note} — "
            f"{p.get('product_url', p.get('landing_url', ''))}"
        )
    lines.append("")

    lines.append("TOUS LES PRODUITS AJOUTÉS (Prix / COGS / X)")
    for p in products:
        lines.append(
            f"- {p.get('title', p.get('product_title', 'Produit'))} ({p.get('shop_domain', '')}) : "
            f"{float(p.get('price_eur', 0)):.2f} € / {float(p.get('cogs_eur', 0)):.2f} € / "
            f"X {float(p.get('x_coefficient', 0)):.2f}"
        )
    lines.append("")

    if near_misses:
        lines.append(f"QUASI-MISS EXCLUS (29,90–29,99 €) : {len(near_misses)}")
        for nm in near_misses[:8]:
            lines.append(f"• {nm}")
        lines.append("")

    lines.append("CRÉDITS TRENDTRACK")
    if credits_used > 0:
        lines.append(f"• Consommation : {credits_used} crédits")
    if credits_rem >= 0:
        lines.append(f"• Solde restant : {credits_rem} crédits")
    lines.append("")
    lines.append(f"Lien direct vers le Google Sheet : {SHEET_LINK}")

    body = "\n".join(lines)
    return {"subject": subject, "body": body}


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
