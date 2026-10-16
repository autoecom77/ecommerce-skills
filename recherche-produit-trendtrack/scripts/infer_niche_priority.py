#!/usr/bin/env python3
"""
infer_niche_priority.py — LLM inference for the Niche (col E) and Priority (col G)
Sheet columns, via OpenRouter (default model: typesafe/jev-router).

The allowed vocabularies are fetched DYNAMICALLY from the Google Sheet
(distinct values of columns E and G), so the model can only pick labels that
already exist in the Sheet.

Auth: same pattern as the ad-creative-repurposing skill — OPENROUTER_API_KEY
from the environment, ~/.hermes/.env, or a local .env file.

Usage:
    python3 infer_niche_priority.py \
        --candidates /tmp/tt_candidates_kept.json \
        --output /tmp/tt_candidates_kept_meta.json \
        [--vocab-out /tmp/tt_vocab.json] [--vocab-file fixture.json] \
        [--model typesafe/jev-router] [--workers 4] [--limit 3]

Outputs the candidates JSON with `niche` and `priority` set (exact Sheet
labels). On inference failure the candidate keeps its previous niche (or
None) and priority (or None); assemble_rows.py falls back to "Divers"/"".
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
GAPI_PY = HERMES_HOME / "venvs/gws/bin/python"
GAPI_SCRIPT = HERMES_HOME / "skills/productivity/google-workspace/scripts/google_api.py"
SHEET_ID = "1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM"

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "typesafe/jev-router"  # the only Jev model on OpenRouter (verified 2026-10-16)


# ---------------------------------------------------------------------------
# OpenRouter auth — same pattern as ad-creative-repurposing/detector.py
# ---------------------------------------------------------------------------
def get_openrouter_api_key() -> str:
    """Retrieve OPENROUTER_API_KEY from environment, ~/.hermes/.env, or local .env."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    for p in (HERMES_HOME / ".env", Path("/home/ubuntu/.hermes/.env"), Path(".env")):
        if p.is_file():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("OPENROUTER_API_KEY="):
                        val = line.split("=", 1)[1].strip(" \"'")
                        if val:
                            return val
            except Exception:
                pass
    raise RuntimeError("OPENROUTER_API_KEY not found in environment, ~/.hermes/.env, or .env")


def chat_json(prompt: str, model: str, api_key: str, retries: int = 2,
              timeout: int = 90) -> Optional[Dict[str, Any]]:
    """One OpenRouter chat call constrained to a JSON object answer. None on failure."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Tu es un analyste e-commerce dropshipping EU. "
                                          "Tu réponds STRICTEMENT en JSON valide, sans texte autour."},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        # Reasoning models (the Jev router routes to some) spend tokens on
        # "reasoning" BEFORE content: 300 gave finish_reason=length with
        # content=null (caught live 2026-10-16). 1024 covers reasoning + answer.
        "max_tokens": 1024,
    }
    last_err = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            OPENROUTER_CHAT_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "TrendTrack Niche/Priority inference",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices") or []
            content = ((choices[0].get("message") or {}).get("content")
                       if choices else None)
            if not content:
                # Reasoning models can exhaust a small token budget (content=null).
                last_err = f"empty content (finish_reason={choices[0].get('finish_reason') if choices else 'n/a'})"
                continue
            return parse_json_loose(content)
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode()[:150]}"
            if e.code < 500:
                break
        except Exception as e:
            last_err = str(e)[:150]
        time.sleep(1.5 * (attempt + 1))
    print(f"[infer] LLM call failed: {last_err}", file=sys.stderr)
    return None


def parse_json_loose(content: str) -> Optional[Dict[str, Any]]:
    """Parse a JSON object, tolerating markdown fences and prose wrappers."""
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    try:
        parsed = json.loads(content.strip())
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            try:
                parsed = json.loads(m.group(0))
                return parsed if isinstance(parsed, dict) else None
            except Exception:
                return None
    return None


# ---------------------------------------------------------------------------
# Dynamic vocabulary from the Google Sheet (columns E = Niche, G = Priorité)
# ---------------------------------------------------------------------------
def fetch_column_values(column: str) -> List[str]:
    """Distinct non-empty values of a Products column, via the Google API CLI."""
    if not GAPI_PY.exists() or not GAPI_SCRIPT.exists():
        raise RuntimeError(f"Google API CLI not found ({GAPI_PY})")
    cmd = [str(GAPI_PY), str(GAPI_SCRIPT), "sheets", "get", SHEET_ID,
           f"Products!{column}2:{column}2000"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"Sheet fetch failed for column {column}: {res.stderr[:200]}")
    rows = json.loads(res.stdout or "[]")
    seen: List[str] = []
    for r in rows:
        val = str(r[0]).strip() if r and r[0] is not None else ""
        if val and val not in seen:
            seen.append(val)
    return seen


def load_or_fetch_vocab(vocab_file: Optional[Path], vocab_out: Optional[Path],
                        refresh: bool = False) -> Dict[str, List[str]]:
    """Niche + Priority vocabularies: cache file → live Sheet fetch → save cache."""
    if vocab_file and vocab_file.exists() and not refresh:
        data = json.loads(vocab_file.read_text(encoding="utf-8"))
        return {"niches": data.get("niches") or [], "priorities": data.get("priorities") or []}
    niches = fetch_column_values("E")
    priorities = fetch_column_values("G")
    if vocab_out:
        vocab_out.parent.mkdir(parents=True, exist_ok=True)
        vocab_out.write_text(json.dumps({"niches": niches, "priorities": priorities},
                                        indent=2, ensure_ascii=False), encoding="utf-8")
    return {"niches": niches, "priorities": priorities}


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------
def candidate_label(c: Dict[str, Any]) -> str:
    return str(c.get("title_fr") or c.get("product_title") or c.get("title")
              or c.get("shop_domain") or "?")[:70]


def build_prompt(c: Dict[str, Any], niches: List[str], priorities: List[str]) -> str:
    """Compact classification prompt with the product signals and the allowed labels."""
    main_cc = str((c.get("country_breakdown") or {}).get("main") or "").upper() or "?"
    price = c.get("price_eur")
    cogs = c.get("cogs_eur")
    x = c.get("x_coefficient")
    desc = str(c.get("description") or "")[:300]
    lines = [
        "Classe ce produit e-commerce pour un tableau de veille dropshipping.",
        "",
        "PRODUIT:",
        f"- Titre: {candidate_label(c)}",
        f"- Boutique: {c.get('shop_domain') or '?'}",
        f"- URL landing: {str(c.get('product_url') or c.get('landing_url') or '')[:100]}",
        f"- Prix vente: {f'{price:.2f} EUR' if isinstance(price, (int, float)) else '?'}",
        f"- COGS: {f'{cogs:.2f} EUR' if isinstance(cogs, (int, float)) else '?'}",
        f"- Coefficient marge X (prix/COGS): {f'{x:.2f}' if isinstance(x, (int, float)) else '?'}",
        f"- Ads actives de la boutique: {c.get('active_ads') or '?'}",
        f"- Reach de la top ad: {c.get('reach_total') or '?'}"
        f" (évolution 7j: {c.get('reach_delta_7d') or '?'})",
        f"- Days running de la top ad: {c.get('days_running') or '?'}",
        f"- Pays principal: {main_cc}",
        f"- Texte de l'ad: {desc}",
        "",
        "NICHE — choisis EXACTEMENT une valeur de cette liste (copie la chaîne telle quelle):",
        json.dumps(niches, ensure_ascii=False),
    ]
    if priorities:
        lines += [
            "PRIORITÉ — choisis EXACTEMENT une valeur de cette liste "
            "(1 Urgent = fort potentiel/marge/dynamique, 2 Important = bon potentiel):",
            json.dumps(priorities, ensure_ascii=False),
        ]
    else:
        lines += ['PRIORITÉ — réponds "" (liste vide dans le Sheet).']
    lines += [
        "",
        'Réponds STRICTEMENT en JSON: {"niche": "<valeur de la liste>", "priority": "<valeur de la liste>"}',
    ]
    return "\n".join(lines)


def match_label(value: Optional[str], vocab: List[str]) -> Optional[str]:
    """Match a model answer to a canonical vocabulary label (exact, then casefold)."""
    if not value or not vocab:
        return None
    value = str(value).strip()
    for v in vocab:
        if value == v:
            return v
    for v in vocab:
        if value.casefold() == v.casefold():
            return v
    return None


def infer_one(c: Dict[str, Any], niches: List[str], priorities: List[str],
              model: str, api_key: str) -> Dict[str, Any]:
    """Infer niche + priority for one candidate. Never raises."""
    prompt = build_prompt(c, niches, priorities)
    parsed = chat_json(prompt, model, api_key)
    niche = match_label((parsed or {}).get("niche"), niches)
    priority = match_label((parsed or {}).get("priority"), priorities)
    return {"niche": niche, "priority": priority,
            "raw": parsed, "ok": bool(niche or priority)}


def apply_inference(candidates: List[Dict[str, Any]], niches: List[str],
                    priorities: List[str], model: str, workers: int = 4,
                    log_every: int = 10) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Fill niche/priority on every candidate (in place + returned). Concurrency-safe."""
    api_key = get_openrouter_api_key()
    stats = {"total": len(candidates), "niche_ok": 0, "priority_ok": 0,
             "failed": 0, "skipped_no_vocab": 0}
    todo = [c for c in candidates if niches or priorities]
    stats["skipped_no_vocab"] = len(candidates) - len(todo)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(infer_one, c, niches, priorities, model, api_key): c for c in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            c = futs[fut]
            try:
                res = fut.result()
            except Exception as e:  # infer_one never raises, belt and braces
                res = {"niche": None, "priority": None, "ok": False, "raw": {"error": str(e)}}
            if res.get("niche"):
                c["niche"] = res["niche"]
                stats["niche_ok"] += 1
            if res.get("priority"):
                c["priority"] = res["priority"]
                stats["priority_ok"] += 1
            if not res.get("ok"):
                stats["failed"] += 1
                print(f"[infer] no valid answer for {candidate_label(c)} "
                      f"(raw: {json.dumps(res.get('raw'), ensure_ascii=False)[:120]})", file=sys.stderr)
            if i % log_every == 0 or i == len(todo):
                print(f"[infer] {i}/{len(todo)} inferred "
                      f"(niche {stats['niche_ok']}, priority {stats['priority_ok']}, "
                      f"failed {stats['failed']})", file=sys.stderr)
    return candidates, stats


# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Infer Niche & Priority columns via OpenRouter.")
    parser.add_argument("--candidates", "-c", required=True, help="Candidates JSON (price_filter full output).")
    parser.add_argument("--output", "-o", required=True, help="Output candidates JSON (same list, niche+priority set).")
    parser.add_argument("--vocab-out", default="/tmp/tt_vocab.json",
                        help="Cache file for the Sheet vocabularies (written on live fetch).")
    parser.add_argument("--vocab-file", default=None,
                        help="Use a vocabulary fixture instead of fetching the Sheet (tests).")
    parser.add_argument("--refresh-vocab", action="store_true", help="Ignore the vocab cache and re-fetch the Sheet.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenRouter model (default {DEFAULT_MODEL}).")
    parser.add_argument("--workers", type=int, default=4, help="Parallel LLM calls (default 4).")
    parser.add_argument("--limit", type=int, default=0, help="Infer only the first N candidates (tests; 0 = all).")
    parser.add_argument("--stats-out", default="", help="Optional stats JSON path.")
    args = parser.parse_args()

    with open(args.candidates, "r", encoding="utf-8") as f:
        candidates: List[Dict[str, Any]] = json.load(f)
    if args.limit > 0:
        candidates = candidates[: args.limit]
    if not candidates:
        print("[infer] no candidates — nothing to do", file=sys.stderr)
        Path(args.output).write_text("[]", encoding="utf-8")
        return

    vocab = load_or_fetch_vocab(Path(args.vocab_file) if args.vocab_file else None,
                                Path(args.vocab_out) if args.vocab_out else None,
                                refresh=args.refresh_vocab)
    niches, priorities = vocab["niches"], vocab["priorities"]
    print(f"[infer] vocabulary: {len(niches)} niches, {len(priorities)} priorities "
          f"(model {args.model}, {len(candidates)} candidates)", file=sys.stderr)
    if not niches and not priorities:
        print("[infer] empty vocabulary — skipping inference entirely", file=sys.stderr)

    candidates, stats = apply_inference(candidates, niches, priorities,
                                        args.model, args.workers)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(candidates, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
    if args.stats_out:
        Path(args.stats_out).write_text(json.dumps({**stats, "model": args.model,
                                                    "niches": niches, "priorities": priorities},
                                                   indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[infer] done: {stats['niche_ok']} niches, {stats['priority_ok']} priorities, "
          f"{stats['failed']} failed → {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
