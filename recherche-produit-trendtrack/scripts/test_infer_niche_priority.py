#!/usr/bin/env python3
"""
test_infer_niche_priority.py — Offline + live tests for infer_niche_priority.py.

Run:  python3 scripts/test_infer_niche_priority.py

Sections:
  1. Pure offline (no network): prompt building, label matching, loose JSON
     parsing, empty-vocabulary path, vocab cache handling.
  2. LIVE tests (network): guarded by RUN_LIVE=1 env or --live flag —
     one real OpenRouter call on a small fixture, and (optionally) the
     dynamic Sheet vocabulary fetch.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from infer_niche_priority import (  # noqa: E402
    build_prompt, match_label, parse_json_loose, load_or_fetch_vocab,
    infer_one, apply_inference, candidate_label,
)

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


CAND = {
    "shop_domain": "luveon.com",
    "product_title": "Legging 3D anti-cellulite",
    "title_fr": "Legging sculptant 3D",
    "product_url": "https://luveon.com/products/leggings-3d-luveon",
    "price_eur": 59.90,
    "cogs_eur": 8.20,
    "x_coefficient": 7.3,
    "active_ads": 3768,
    "reach_total": 5213475,
    "reach_delta_7d": 340000,
    "days_running": 286,
    "country_breakdown": {"main": "FR"},
    "description": "Legging compression 3D, draine et affine la peau. +50 000 clientes.",
}
NICHES = ["Cosmétique", "Apparel/Femme", "Maison/Literie", "Sport/Outdoor", "Gadget/Accessoires"]
PRIOS = ["1 Urgent", "2 Important"]


def test_offline():
    print("\n--- 1. OFFLINE ---")
    p = build_prompt(CAND, NICHES, PRIOS)
    check("prompt contient le titre", "Legging" in p)
    check("prompt liste les niches", all(n in p for n in NICHES))
    check("prompt liste les priorités", all(x in p for x in PRIOS))
    check("prompt exige le JSON", '"niche"' in p and '"priority"' in p)

    check("match exact", match_label("Cosmétique", NICHES) == "Cosmétique")
    check("match casse", match_label("cosmétique", NICHES) == "Cosmétique")
    check("match espace", match_label(" 1 urgent ", PRIOS) == "1 Urgent")
    check("hors vocabulaire → None", match_label("Beauté", NICHES) is None)
    check("valeur vide → None", match_label("", NICHES) is None)
    check("vocab vide → None", match_label("Cosmétique", []) is None)

    d = parse_json_loose('{"niche": "Cosmétique", "priority": "1 Urgent"}')
    check("JSON nu", d == {"niche": "Cosmétique", "priority": "1 Urgent"})
    d = parse_json_loose('```json\n{"niche": "Sport/Outdoor"}\n```')
    check("JSON encerclé ```", d == {"niche": "Sport/Outdoor"})
    d = parse_json_loose('Voici la réponse:\n{"niche": "Bijoux"} merci.')
    check("JSON dans du texte", d == {"niche": "Bijoux"})
    check("JSON invalide → None", parse_json_loose("niche: Bijoux") is None)
    check("liste (non dict) → None", parse_json_loose('["Bijoux"]') is None)

    # vocab cache: file present → no Sheet fetch (monkeypatch fetch to explode)
    import infer_niche_priority as m
    tmp = Path(tempfile.mkdtemp())
    cache = tmp / "vocab.json"
    cache.write_text(json.dumps({"niches": NICHES, "priorities": PRIOS}), encoding="utf-8")
    orig = m.fetch_column_values
    m.fetch_column_values = lambda col: (_ for _ in ()).throw(RuntimeError("network!"))
    try:
        v = load_or_fetch_vocab(cache, None)
        check("cache vocab sans réseau", v == {"niches": NICHES, "priorities": PRIOS})
    finally:
        m.fetch_column_values = orig

    # apply_inference with empty vocab: nothing to do, no crash, no LLM call
    cands = [dict(CAND)]
    out, stats = apply_inference(cands, [], [], "x", workers=1)
    check("vocab vide → skip sans crash", stats["skipped_no_vocab"] == 1 and out[0].get("niche") is None)

    # apply_inference with a stubbed LLM (monkeypatch chat_json → fixed answer)
    m2 = sys.modules["infer_niche_priority"]
    orig_chat = m2.chat_json
    m2.chat_json = lambda prompt, model, key, **kw: {"niche": "Apparel/Femme", "priority": "2 Important"}
    try:
        cands = [dict(CAND), dict(CAND, shop_domain="other.com", product_title="Set de 3 poêles inox")]
        out, stats = m2.apply_inference(cands, NICHES, PRIOS, "stub", workers=2)
        check("LLM stub: niche appliquée", out[0]["niche"] == "Apparel/Femme")
        check("LLM stub: priorité appliquée", out[1]["priority"] == "2 Important")
        check("LLM stub: stats cohérentes", stats["niche_ok"] == 2 and stats["priority_ok"] == 2)
    finally:
        m2.chat_json = orig_chat

    # LLM returns an out-of-vocab answer → must NOT be applied
    m2.chat_json = lambda prompt, model, key, **kw: {"niche": "Tractors", "priority": "9 Max"}
    try:
        cands = [dict(CAND)]
        out, stats = m2.apply_inference(cands, NICHES, PRIOS, "stub", workers=1)
        check("réponse hors vocabulaire ignorée", out[0].get("niche") is None and stats["failed"] == 1)
    finally:
        m2.chat_json = orig_chat


def test_live():
    print("\n--- 2. LIVE (réseau) ---")
    live = "--live" in sys.argv or os.environ.get("RUN_LIVE") == "1"
    if not live:
        print("⏭️  ignoré (lancer avec --live ou RUN_LIVE=1)")
        return
    res = infer_one(CAND, NICHES, PRIOS, "typesafe/jev-router",
                    __import__("infer_niche_priority").get_openrouter_api_key())
    print("   raw:", json.dumps(res.get("raw"), ensure_ascii=False)[:160])
    check("live: niche dans le vocabulaire", res.get("niche") in NICHES, f"got {res.get('niche')!r}")
    check("live: priorité dans le vocabulaire", res.get("priority") in PRIOS or res.get("priority") is None,
          f"got {res.get('priority')!r}")
    check("live: au moins une valeur", bool(res.get("ok")))


if __name__ == "__main__":
    test_offline()
    test_live()
    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} échec(s): {', '.join(FAILURES)}")
        sys.exit(1)
    print("✅ Tous les tests passent.")
