#!/usr/bin/env python3
"""run_pipeline.py — Master orchestrator for TrendTrack Product Research.

Runs the entire deterministic ecommerce research pipeline end-to-end:
  Étape 0  : Vérification crédits TrendTrack & extraction URL Sheet existantes
  Étape 1  : Recherche batch TrendTrack (query_ads_api.py, budget crédits)
  Étape 1.5: Dédoublonnage exact par domaine (pre_filter.py)
  Étape 2  : Extraction multithreadée des prix de vente web (extract_prices.py)
  Étape 2b : Pré-filtrage prix < 30€ & near-misses (price_filter.py --mode price-only)
  Étape 3  : Recherche COGS en cascade (cogs_image_search.py), parallélisée
  Étape 3b : Validation stricte ratio X (price_filter.py --mode full)
  Étape 4  : Enrichissement des top ads TrendTrack format ju-2 (enrich_api.py, budget crédits)
  Étape 5  : Formatage des 75 colonnes sans score (assemble_rows.py)
  Étape 6  : Écriture atomique dans Google Sheets (write_sheet.py)
  Étape 7  : Envoi de l'email récapitulatif (send_recap.py)

Credit policy (2026-10-16, user decision): NO per-run credit cap anymore. If items pass
the filters, enrich them ALL until the end (the 2026-10-16 run left 64 shops un-enriched
because of the old 1 200-credit cap). The search still takes --search-budget credits per
run (default 850) as a search-depth knob, and the enrichment gets the REST OF THE BALANCE
(until the account runs dry). The only remaining hard stop: abort if the starting balance
is < 300. Billing is ~1.5 credit per returned ad (~150 per 100-ad page).

Usage:
    python3 run_pipeline.py [--pages 5] [--dry-run] [--date YYYY-MM-DD]
                            [--search-budget 850] [--cogs-workers 4]
                            [--resume-from search|prices|cogs]

--resume-from restarts a stopped/crashed run at that stage, reusing the earlier /tmp
artifacts (the paid TrendTrack search is not re-run; the credit baseline is kept).

stdout: final JSON summary (funnel, credits, top 3, near-misses) — also saved to
        /tmp/tt_run/run_summary.json. All progress logs go to stderr.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from send_recap import compute_funnel, product_name  # noqa: E402

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
REST_KEY_PATH = HERMES_HOME / "mcp-tokens" / "trendtrack_rest_key.txt"
USAGE_URL = "https://api.trendtrack.io/v1/usage"
SHEET_ID = "1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM"

RUN_DIR = Path("/tmp/tt_run")
RUN_DIR.mkdir(parents=True, exist_ok=True)

# 2026-10-16: the per-run credit CAP is REMOVED (user decision) — enrich every kept
# shop until the end instead of stopping at an arbitrary budget. The search budget
# remains a search-DEPTH knob. The only hard stop left is the low-balance guard.
DEFAULT_SEARCH_BUDGET = 850
MIN_START_CREDITS = 300

# cogs_image_search.py cascade worst case ≈ Apify image (~2.3 min) + AliExpress (~2.3 min)
# + 1688 keyword (~1.8 min); its own internal timeouts should fire before this outer guard.
COGS_TIMEOUT_S = 420
DEFAULT_COGS_WORKERS = 4  # Apify free plan allows max 5 concurrent actor runs

NICHE_DIVISORS = {
    "cosmetique": 6.5,
    "maison": 5.0,
    "cuisine": 5.0,
    "apparel": 6.0,
    "sport": 5.0,
    "sante": 6.5,
    "puericulture": 6.0,
}

P: Dict[str, Path] = {
    "usage_start": Path("/tmp/tt_usage_start.json"),
    "usage_mid": Path("/tmp/tt_usage_mid.json"),
    "usage_end": Path("/tmp/tt_usage_end.json"),
    "sheet_existing": Path("/tmp/tt_sheet_existing.json"),
    "raw": Path("/tmp/tt_candidates_raw.json"),
    "query_stats": Path("/tmp/tt_query_stats.json"),
    "filtered": Path("/tmp/tt_candidates_filtered.json"),
    "with_prices": Path("/tmp/tt_candidates_with_prices.json"),
    "priced": Path("/tmp/tt_candidates_priced.json"),
    "near_misses": Path("/tmp/tt_near_misses.json"),
    "near_misses_full": Path("/tmp/tt_near_misses_full.json"),
    "with_cogs": Path("/tmp/tt_candidates_with_cogs.json"),
    "cogs_stats": Path("/tmp/tt_cogs_stats.json"),
    "kept": Path("/tmp/tt_candidates_kept.json"),
    "kept_meta": Path("/tmp/tt_candidates_kept_meta.json"),
    "vocab": Path("/tmp/tt_vocab.json"),
    "infer_stats": Path("/tmp/tt_infer_stats.json"),
    "enriched": Path("/tmp/tt_enriched.json"),
    "enrich_stats": Path("/tmp/tt_enrich_stats.json"),
    "rows": RUN_DIR / "rows.json",
    "final_products": RUN_DIR / "final_products.json",
    "sheet_result": RUN_DIR / "sheet_write_result.json",
    "run_summary": RUN_DIR / "run_summary.json",
}

# Artifacts produced by each resumable stage (see --resume-from).
STAGE_OUTPUTS = {
    "search": ["usage_start", "sheet_existing", "raw", "query_stats", "filtered"],
    "prices": ["with_prices", "priced", "near_misses"],
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def archive_stale_artifacts(keep: Optional[List[str]] = None) -> None:
    """Move the previous run's artifacts away so they can never leak into this run's recap."""
    keep_set = set(keep or [])
    existing = [p for k, p in P.items() if k not in keep_set and p.exists()]
    if not existing:
        return
    dest = RUN_DIR / f"archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    dest.mkdir(parents=True, exist_ok=True)
    for p in existing:
        try:
            shutil.move(str(p), str(dest / p.name))
        except Exception as e:
            log(f"[pipeline] Warning: could not archive {p} ({e})")
    log(f"[pipeline] Archived {len(existing)} previous-run artifact(s) to {dest}")


def fetch_usage() -> Dict[str, Any]:
    if not REST_KEY_PATH.exists():
        log(f"[pipeline] Warning: No REST key at {REST_KEY_PATH}")
        return {}
    key = REST_KEY_PATH.read_text().strip()
    req = urllib.request.Request(USAGE_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log(f"[pipeline] Warning: Could not check usage ({e})")
        return {}


def check_and_save_usage(dest_path: Path) -> Dict[str, Any]:
    """Fetch current TrendTrack usage (free call) and save to JSON."""
    data = fetch_usage()
    if data:
        dest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def remaining_credits(u: Dict[str, Any]) -> int:
    try:
        v = (u.get("credits") or {}).get("totalRemaining")
        if v is None:
            v = (u.get("includedQuota") or {}).get("remaining")
        return int(v) if v is not None else -1
    except Exception:
        return -1


def load_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def run_cmd(cmd: List[str], desc: str, stdout_path: Optional[Path] = None, fatal: bool = True) -> bool:
    """Run a sub-command with logging. stdout goes to stdout_path, else to our stderr."""
    t0 = time.time()
    log(f"\n[pipeline] === {desc} ===")
    if stdout_path is not None:
        with open(stdout_path, "w", encoding="utf-8") as f_out:
            res = subprocess.run(cmd, stdout=f_out, text=True)
    else:
        res = subprocess.run(cmd, stdout=sys.stderr, text=True)
    if res.returncode != 0:
        if fatal:
            raise SystemExit(f"[pipeline] ERROR: Step '{desc}' failed with exit code {res.returncode}")
        log(f"[pipeline] Warning: step '{desc}' failed with exit code {res.returncode}")
        return False
    log(f"[pipeline] ✓ {desc} completed in {time.time() - t0:.1f}s")
    return True


# ---------------------------------------------------------------------------
# COGS (Étape 3)
# ---------------------------------------------------------------------------
def _run_isolated(cmd: List[str], timeout: int) -> Optional[subprocess.CompletedProcess]:
    """Run cmd in its own session; on timeout kill the whole process group (incl. headless browsers)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()
        try:
            proc.communicate(timeout=15)
        except Exception:
            pass
        return None


def _strip_accents(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


def search_cogs(c: Dict[str, Any]) -> Dict[str, Any]:
    """COGS cascade for one candidate (mutates and returns it). Never raises.

    cogs_image_search.py takes the MEDIAN of each method's matches and rejects estimates
    with Prix/COGS > 30 (spare part / sample listings); if no method gives a plausible
    COGS, fall back to the niche heuristic (Prix / diviseur).
    """
    t0 = time.time()
    p_name = c.get("product_title") or c.get("title") or c.get("shop_domain", "")
    img_url = c.get("product_image") or c.get("image_url") or ""
    if img_url.startswith("//"):
        img_url = "https:" + img_url
    c.update({"cogs_eur": None, "cogs_source": None, "cogs_method": None, "cogs_note": "",
              "cogs_url": None, "cogs_confidence": None})
    price = c.get("price_eur")
    if price is None:
        # No selling price → price_filter (full) drops it anyway; don't spend Apify/browser time.
        c["cogs_method"] = "skipped_no_price"
        c["cogs_note"] = "pas de prix de vente — COGS non recherché"
        return c
    price_eur = float(price)

    if img_url:
        cmd = [sys.executable, str(SCRIPT_DIR / "cogs_image_search.py"),
               "--image-url", img_url, "--product-name", str(p_name)[:120], "--price-eur", f"{price_eur:.2f}"]
        res: Optional[subprocess.CompletedProcess] = None
        try:
            res = _run_isolated(cmd, COGS_TIMEOUT_S)
            if res is None:
                c["cogs_error"] = f"timeout > {COGS_TIMEOUT_S}s"
        except Exception as e:
            c["cogs_error"] = f"spawn error: {e}"
        if res is not None:
            cdata: Any = None
            try:
                cdata = json.loads(res.stdout) if res.stdout.strip() else None
            except Exception as e:
                c["cogs_error"] = f"bad JSON: {e}"
            if isinstance(cdata, dict):
                c["cogs_attempts"] = cdata.get("attempts") or []
                if res.returncode == 0 and cdata.get("cogs_eur"):
                    eur = float(cdata["cogs_eur"])
                    method = cdata.get("method") or ""
                    orig, cur = cdata.get("cogs_original"), cdata.get("cogs_currency")
                    label = {"apify_1688": "1688 (image)", "apify_1688_keyword": "1688 (mot-clé)",
                             "aliexpress_image": "AliExpress (image)"}.get(method, cdata.get("source") or method)
                    orig_txt = f"¥{orig:g} ≈ " if cur == "CNY" and isinstance(orig, (int, float)) else ""
                    c.update({"cogs_eur": eur, "cogs_source": cdata.get("source") or method,
                              "cogs_method": method, "cogs_note": f"COGS {label} médiane {orig_txt}{eur:.2f}€",
                              "cogs_url": cdata.get("product_url"), "cogs_confidence": cdata.get("confidence"),
                              "cogs_min_eur": cdata.get("cogs_min_eur")})
                else:
                    why = "; ".join(f"{a.get('method')}: {a.get('outcome')}" for a in c["cogs_attempts"])
                    c["cogs_error"] = "aucun COGS plausible" + (f" ({why})" if why else "")
            elif "cogs_error" not in c:
                c["cogs_error"] = f"no output (exit {res.returncode})"
    else:
        c["cogs_error"] = "no product image"

    if c["cogs_eur"] is None:
        div = 5.5
        niche_norm = _strip_accents(str(c.get("niche") or "")).lower()
        for k, d in NICHE_DIVISORS.items():
            if k in niche_norm:
                div = d
                break
        implausible = any("implausible" in str(a.get("outcome")) for a in c.get("cogs_attempts") or [])
        reason = "matches sourcing implausibles" if implausible else "produit introuvable au sourcing"
        c.update({"cogs_eur": round(price_eur / div, 2), "cogs_source": "heuristique",
                  "cogs_method": "heuristique",
                  "cogs_note": f"COGS estimé (heuristique diviseur {div}, {reason})"})
    c["cogs_elapsed_s"] = round(time.time() - t0, 1)
    return c


def main() -> None:
    parser = argparse.ArgumentParser(description="Master TrendTrack product research pipeline.")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Run date (YYYY-MM-DD)")
    parser.add_argument("--pages", type=int, default=5, help="Max pages per search method (default 5)")
    parser.add_argument("--dry-run", action="store_true", help="Run without modifying Sheet or sending real email")
    parser.add_argument("--search-budget", type=int, default=DEFAULT_SEARCH_BUDGET,
                        help=f"Credits for the ad search (search-depth knob, default {DEFAULT_SEARCH_BUDGET}). "
                             "The enrichment is no longer capped: every kept shop is enriched until the account balance runs out.")
    parser.add_argument("--cogs-workers", type=int, default=DEFAULT_COGS_WORKERS,
                        help=f"Parallel COGS searches (default {DEFAULT_COGS_WORKERS})")
    parser.add_argument("--resume-from", choices=["search", "prices", "cogs"], default="search",
                        help="Restart a stopped/crashed run at this stage, reusing the earlier /tmp artifacts "
                             "(the paid TrendTrack search is not re-run).")
    args = parser.parse_args()

    t_start = time.time()
    log(f"[pipeline] Starting TrendTrack Product Research Pipeline for {args.date} (from stage: {args.resume_from})...")
    keep: List[str] = []
    if args.resume_from in ("prices", "cogs"):
        keep += STAGE_OUTPUTS["search"]
    if args.resume_from == "cogs":
        keep += STAGE_OUTPUTS["prices"]
    missing = [str(P[k]) for k in keep if not P[k].exists()]
    if missing:
        raise SystemExit(f"[pipeline] Cannot resume from '{args.resume_from}': missing {missing}")
    archive_stale_artifacts(keep)

    # ---------------------------------------------------------
    # Étape 0 : Pré-checks
    # ---------------------------------------------------------
    if args.resume_from == "search":
        u0 = check_and_save_usage(P["usage_start"])
    else:
        u0 = load_json(P["usage_start"], {})  # keep the original baseline: the recap must count the search
    rem_start = remaining_credits(u0)
    log(f"[pipeline] TrendTrack balance at start: {rem_start} credits (search budget {args.search_budget})")
    if args.resume_from == "search" and 0 <= rem_start < MIN_START_CREDITS:
        raise SystemExit(
            f"[pipeline] ABORT: Low TrendTrack credits ({rem_start} < {MIN_START_CREDITS}). "
            f"Stopping run to protect balance."
        )
    search_budget = max(0, args.search_budget)

    if args.resume_from == "search":
        gapi_py = HERMES_HOME / "venvs/gws/bin/python"
        gapi_script = HERMES_HOME / "skills/productivity/google-workspace/scripts/google_api.py"
        fetch_sheet_cmd = [str(gapi_py), str(gapi_script), "sheets", "get", SHEET_ID, "Products!B2:C2000"]
        with open(P["sheet_existing"], "w", encoding="utf-8") as f_out:
            res = subprocess.run(fetch_sheet_cmd, stdout=f_out, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                raise SystemExit(f"[pipeline] Failed to fetch existing Sheet rows: {res.stderr}")

        # Étape 1 : Recherche batch TrendTrack API (budget crédits)
        run_cmd([sys.executable, str(SCRIPT_DIR / "query_ads_api.py"),
                 "--date", args.date, "--pages", str(args.pages),
                 "--max-credits", str(search_budget), "--stats-out", str(P["query_stats"])],
                f"Recherche batch TrendTrack (query_ads_api.py, budget {search_budget} crédits)",
                stdout_path=P["raw"])

        # Étape 1.5 : Dédoublonnage par rapport au Sheet
        run_cmd([sys.executable, str(SCRIPT_DIR / "pre_filter.py"),
                 "--candidates", str(P["raw"]), "--sheet-data", str(P["sheet_existing"])],
                "Dédoublonnage avec le Sheet (pre_filter.py)", stdout_path=P["filtered"])

    if args.resume_from in ("search", "prices"):
        # Étape 2 : Extraction des prix de vente web
        run_cmd([sys.executable, str(SCRIPT_DIR / "extract_prices.py"),
                 "--input", str(P["filtered"]), "--output", str(P["with_prices"])],
                "Extraction des prix de vente web (extract_prices.py)")

        # Étape 2bis : Pré-filtrage prix < 30 € & near-misses
        run_cmd([sys.executable, str(SCRIPT_DIR / "price_filter.py"), "--mode", "price-only",
                 "--input", str(P["with_prices"]), "--near-misses-file", str(P["near_misses"])],
                "Pré-filtrage prix (price_filter.py --mode price-only)", stdout_path=P["priced"])

    # ---------------------------------------------------------
    # Étape 3 : Recherche COGS en cascade (parallélisée)
    # ---------------------------------------------------------
    candidates_to_cogs: List[Dict[str, Any]] = load_json(P["priced"], [])
    to_search = [c for c in candidates_to_cogs if c.get("price_eur") is not None]
    n_no_price = len(candidates_to_cogs) - len(to_search)
    workers = max(1, args.cogs_workers)
    log(f"\n[pipeline] === Recherche COGS : {len(to_search)} candidats avec prix "
        f"({n_no_price} sans prix ignorés), {workers} en parallèle ===")
    t_cogs = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(search_cogs, c): c for c in to_search}
        for c in candidates_to_cogs:
            if c.get("price_eur") is None:
                search_cogs(c)  # instant: marks skipped_no_price
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                fut.result()
            except Exception as e:  # search_cogs never raises — belt and braces
                c["cogs_error"] = f"crash: {e}"
            done += 1
            cogs_val = c.get("cogs_eur")
            cogs_txt = f"{cogs_val:.2f}€" if isinstance(cogs_val, (int, float)) else "?"
            log(f"[cogs] ({done}/{len(to_search)}) {str(c.get('product_title') or c.get('shop_domain'))[:40]} "
                f"— {float(c['price_eur']):.2f}€ → COGS {cogs_txt} via {c.get('cogs_method')} "
                f"({c.get('cogs_elapsed_s', '?')}s)" + (f" [{c['cogs_error']}]" if c.get("cogs_error") else ""))

    with open(P["with_cogs"], "w", encoding="utf-8") as f:
        json.dump(candidates_to_cogs, f, indent=2, ensure_ascii=False)
    cogs_stats = {
        "searched": len(to_search),
        "skipped_no_price": n_no_price,
        "by_method": dict(Counter(str(c.get("cogs_method")) for c in to_search)),
        "implausible_rejections": sum(1 for c in to_search for a in (c.get("cogs_attempts") or [])
                                      if "implausible" in str(a.get("outcome"))),
        "timeouts": sum(1 for c in to_search if "timeout" in str(c.get("cogs_error", ""))),
        "elapsed_s": round(time.time() - t_cogs, 1),
    }
    P["cogs_stats"].write_text(json.dumps(cogs_stats, indent=2), encoding="utf-8")
    log(f"[pipeline] ✓ COGS terminé en {cogs_stats['elapsed_s']}s : {cogs_stats['by_method']}")

    # ---------------------------------------------------------
    # Étape 3bis : Validation finale ratio X
    # ---------------------------------------------------------
    run_cmd([sys.executable, str(SCRIPT_DIR / "price_filter.py"), "--mode", "full",
             "--input", str(P["with_cogs"]), "--near-misses-file", str(P["near_misses_full"])],
            "Validation finale ratio X (price_filter.py --mode full)", stdout_path=P["kept"])
    # Full mode must not wipe the near-misses caught in price-only mode: merge both lists.
    merged_nm = list(dict.fromkeys([*load_json(P["near_misses"], []), *load_json(P["near_misses_full"], [])]))
    P["near_misses"].write_text(json.dumps(merged_nm, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---------------------------------------------------------
    # Étape 4 : Enrichissement TrendTrack top ads — SANS plafond
    # (2026-10-16 : tous les shops retenus sont enrichis jusqu'au bout ;
    #  enrich_api.py --max-credits 0 = illimité, seule la limite du compte arrête)
    # ---------------------------------------------------------
    kept: List[Dict[str, Any]] = load_json(P["kept"], [])
    if not kept:
        log("[pipeline] No candidate kept — enrichment skipped")
        P["enriched"].write_text("[]", encoding="utf-8")
    else:
        ok = run_cmd([sys.executable, str(SCRIPT_DIR / "enrich_api.py"),
                      "--candidates", str(P["kept"]), "--max-credits", "0",
                      "--stats-out", str(P["enrich_stats"])],
                     "Enrichissement top ads (enrich_api.py, illimité)",
                     stdout_path=P["enriched"], fatal=False)
        if not ok or not isinstance(load_json(P["enriched"], None), list):
            log("[pipeline] Warning: enrichment failed, continuing with the search ads only")
            P["enriched"].write_text("[]", encoding="utf-8")

    # ---------------------------------------------------------
    # Étape 4bis : Inférence Niche + Priorité (OpenRouter, vocabulaire du Sheet)
    # ---------------------------------------------------------
    if kept:
        infer_cmd = [sys.executable, str(SCRIPT_DIR / "infer_niche_priority.py"),
                     "--candidates", str(P["kept"]), "--output", str(P["kept_meta"]),
                     "--vocab-out", str(P["vocab"]), "--stats-out", str(P["infer_stats"])]
        infer_ok = run_cmd(infer_cmd, "Inférence Niche & Priorité (infer_niche_priority.py, OpenRouter)",
                           fatal=False)
        if infer_ok and P["kept_meta"].exists():
            kept = load_json(P["kept_meta"], kept) or kept
            P["kept"].write_text(json.dumps(kept, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            log("[pipeline] Warning: niche/priority inference failed — continuing with empty columns")
    else:
        P["infer_stats"].write_text("{}", encoding="utf-8")

    # ---------------------------------------------------------
    # Étape 5 : Formatage des 75 colonnes (assemble_rows.py)
    # ---------------------------------------------------------
    run_cmd([sys.executable, str(SCRIPT_DIR / "assemble_rows.py"),
             "--candidates", str(P["kept"]), "--enriched", str(P["enriched"]),
             "--output", str(P["rows"]), "--products-out", str(P["final_products"])],
            "Assemblage des 75 colonnes Sheet (assemble_rows.py)")

    # ---------------------------------------------------------
    # Étape 6 : Écriture dans Google Sheets
    # ---------------------------------------------------------
    write_cmd = [sys.executable, str(SCRIPT_DIR / "write_sheet.py"),
                 "--rows", str(P["rows"]), "--result-out", str(P["sheet_result"])]
    if args.dry_run:
        write_cmd.append("--dry-run")
    run_cmd(write_cmd, "Écriture dans Google Sheets (write_sheet.py)")

    # ---------------------------------------------------------
    # Étape 7 : Envoi email récapitulatif
    # ---------------------------------------------------------
    check_and_save_usage(P["usage_end"])
    recap_cmd = [sys.executable, str(SCRIPT_DIR / "send_recap.py"), "--date", datetime.now().strftime("%d/%m/%Y")]
    recap_cmd.append("--dry-run" if args.dry_run else "--send")
    email_ok = run_cmd(recap_cmd, "Génération & Envoi du récapitulatif email (send_recap.py)", fatal=False)

    # ---------------------------------------------------------
    # Bilan Final
    # ---------------------------------------------------------
    final_products: List[Dict[str, Any]] = load_json(P["final_products"], [])
    total_time = time.time() - t_start
    funnel = compute_funnel()
    summary = {
        "status": "success" if email_ok else "success_email_failed",
        "date": args.date,
        "resumed_from": args.resume_from,
        "duration_seconds": round(total_time, 1),
        "dry_run": args.dry_run,
        "products_count": len(final_products),
        "sheet": load_json(P["sheet_result"], {}),
        "funnel": {k: v for k, v in funnel.items() if k not in ("near_misses", "enrich")},
        "cogs_stats": cogs_stats,
        "enrich_stats": load_json(P["enrich_stats"], {}),
        "infer_stats": load_json(P["infer_stats"], {}),
        "near_misses": funnel.get("near_misses") or [],
        "top_products": [
            {
                "title": product_name(p),
                "domain": p.get("shop_domain"),
                "price_eur": p.get("price_eur"),
                "cogs_eur": p.get("cogs_eur"),
                "cogs_method": p.get("cogs_method"),
                "cogs_url": p.get("cogs_url"),
                "x_coefficient": p.get("x_coefficient"),
                "url": p.get("product_url"),
            }
            for p in final_products[:3]
        ],
    }
    P["run_summary"].write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    log("\n[pipeline] ====================================================")
    log(f"[pipeline] Pipeline completed in {total_time:.1f}s — {len(final_products)} products added")
    log("[pipeline] ====================================================")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
