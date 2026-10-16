#!/usr/bin/env python3
"""Regression suite: python3 scripts/test_infer_niche_priority.py [--live].

Offline tests never access credentials/network. --live additionally fetches real
Sheets dropdowns, makes three billed Jev 1.14 Decisions calls, assembles 75-column
rows locally, and saves actual responses to --report (default /tmp/tt_jev114_live.json).
No Sheet writes, no TrendTrack search/enrichment calls, no Ads modifications.
"""
import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch, Mock, MagicMock
from email.message import Message

sys.path.insert(0, str(Path(__file__).resolve().parent))
import infer_niche_priority as m
from sheet_vocab import extract_vocab

NICHES = ["Animaux", "Cuisine", "Sport", "Autre"]
PRIOS = ["1 Urgent", "2 Important", "3 Normal", "4 Faible", "5 Lancé"]
# Explicit test fixtures, NOT claimed to be scraped product data.
FIXTURES = [
    {"product_title": "Harnais de promenade réglable pour chien", "shop_domain": "fixture-pets.example",
     "description": "Harnais confortable pour chien, sangle réglable et attache de laisse.",
     "price_eur": 49.9, "cogs_eur": 8, "x_coefficient": 6.24, "cogs_method": "1688_image",
     "active_ads": 80, "reach_total": 800000, "reach_delta_7d": 180000, "days_running": 50,
     "ad_id": "facebook_123456789", "ad_url": "https://app.trendtrack.io/en/ju-2/explorer?tab=ads&ad=facebook_123456789&adSource=meta"},
    {"product_title": "Lot de 3 poêles de cuisine en acier inoxydable", "shop_domain": "fixture-kitchen.example",
     "description": "Poêles inox pour cuisson sur induction, gaz et four.", "price_eur": 89.9,
     "cogs_eur": 18, "x_coefficient": 4.99, "cogs_method": "heuristic", "active_ads": 5,
     "reach_total": 100000, "reach_delta_7d": 0, "days_running": 180},
    {"product_title": "Tapis de yoga antidérapant pour fitness", "shop_domain": "fixture-sport.example",
     "description": "Tapis pour pratique du yoga, pilates et exercices de fitness à domicile.",
     "price_eur": 39.9, "cogs_eur": 7, "x_coefficient": 5.7, "cogs_method": "1688_image",
     "active_ads": 20, "reach_total": 300000, "reach_delta_7d": 50000, "days_running": 80},
]


def typed(niche="Animaux", priority="2 Important"):
    return {"id": "fixture-decision", "model": m.DEFAULT_MODEL, "usage": {"cost": 0},
            "answers": {"niche": {"type": "choice", "choice": niche, "confidence": 0.9},
                        "priority": {"type": "choice", "choice": priority, "confidence": 0.8}}}


def metadata(condition, column=4, formatted=None):
    cell = {"dataValidation": {"condition": condition}}
    if formatted:
        cell["formattedValue"] = formatted
    return {"sheets": [{"properties": {"title": "Products"}, "data": [
        {"startRow": 1, "startColumn": column, "rowData": [{"values": [cell]}]},
        {"startRow": 1, "startColumn": 6, "rowData": [{"values": [{"dataValidation": {
            "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in PRIOS]}}}]}]},
    ]}]}


class Offline(unittest.TestCase):
    def test_exact_model_and_endpoint(self):
        self.assertEqual(m.DEFAULT_MODEL, "typesafe/jev-1.14")
        self.assertEqual(m.DECISIONS_URL, "https://openrouter.ai/api/alpha/decisions")

    def test_typed_questions_and_no_chat_fields(self):
        req = m.build_request(FIXTURES[0], NICHES, PRIOS)
        self.assertEqual(set(req), {"model", "state", "questions"})
        for key, options in (("niche", NICHES), ("priority", PRIOS)):
            self.assertEqual(req["questions"][key]["type"], "choice")
            self.assertEqual(list(req["questions"][key]["criteria"]), options)

    def test_unseen_dropdown_option_is_included(self):
        req = m.build_request({}, ["Nouvelle niche"], ["6 À revoir"])
        self.assertIn("Nouvelle niche", req["questions"]["niche"]["criteria"])
        self.assertIn("6 À revoir", req["questions"]["priority"]["criteria"])

    def test_state_preserves_zero_and_cogs_reliability(self):
        state = m.build_state(FIXTURES[1])
        self.assertEqual(state["reach_delta_7d"], 0)
        self.assertEqual(state["cogs_method"], "heuristic")
        self.assertEqual(state["price_eur"], 89.9)

    def test_missing_metrics_not_invented_and_secrets_excluded(self):
        state = m.build_state({"title": "Produit", "api_key": "SECRET"})
        self.assertNotIn("reach_total", state)
        self.assertNotIn("SECRET", json.dumps(state))

    def test_state_description_bounded(self):
        self.assertEqual(len(m.build_state({"description": "a" * 3000})["description"]), 1500)

    def test_launched_requires_our_launch_evidence(self):
        criteria = m.build_request({}, NICHES, PRIOS)["questions"]["priority"]["criteria"]
        self.assertIn("NOUS", criteria["5 Lancé"])
        self.assertIn("heuristique", m.build_request({}, NICHES, PRIOS)["questions"]["priority"]["instructions"])

    def test_match_label(self):
        self.assertEqual(m.match_label(" 3 NORMAL ", PRIOS), "3 Normal")
        self.assertIsNone(m.match_label("inconnu", PRIOS))
        self.assertIsNone(m.match_label(3, PRIOS))

    def test_full_dropdown_not_only_selected_value(self):
        meta = metadata({"type": "ONE_OF_LIST", "values": [
            {"userEnteredValue": "Animaux"}, {"userEnteredValue": "Unused category"}]}, formatted="Animaux")
        result = extract_vocab(meta, lambda _: self.fail("unexpected range read"))
        self.assertEqual(result["niches"], ["Animaux", "Unused category"])
        self.assertEqual(result["priorities"], PRIOS)

    def test_range_dropdown_and_named_range(self):
        for ref, expected in (("='Options'!A2:A", "'Options'!A2:A"), ("=Niches", "Niches"), ("=A2:A", "'Products'!A2:A")):
            with self.subTest(ref=ref):
                read = Mock(return_value=[["Animaux"], ["Sport"], [""]])
                result = extract_vocab(metadata({"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": ref}]}), read)
                self.assertEqual(result["niches"], ["Animaux", "Sport"])
                read.assert_called_once_with(expected)

    def test_no_dropdown_fails_instead_of_observed_fallback(self):
        with self.assertRaises(RuntimeError):
            extract_vocab(metadata({"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": "Animaux"}]}), lambda _: [])

    def test_fixture_vocab_no_network(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, "fetch_sheet_vocab", side_effect=AssertionError("network")):
            p = Path(tmp) / "v.json"
            p.write_text(json.dumps({"niches": NICHES, "priorities": PRIOS}))
            self.assertEqual(m.load_or_fetch_vocab(p)["priorities"], PRIOS)

    def test_normal_run_always_fetches_fresh(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, "fetch_sheet_vocab", return_value={"niches": NICHES, "priorities": PRIOS}) as fetch:
            p = Path(tmp) / "stale.json"
            p.write_text('{"niches":["obsolete"]}')
            self.assertEqual(m.load_or_fetch_vocab(None, p)["niches"], NICHES)
            fetch.assert_called_once()

    def test_empty_vocab_rejected(self):
        with patch.object(m, "fetch_sheet_vocab", return_value={"niches": [], "priorities": PRIOS}):
            with self.assertRaises(ValueError):
                m.load_or_fetch_vocab()

    def test_empty_work_needs_no_auth(self):
        with patch.object(m, "get_openrouter_api_key", side_effect=AssertionError("auth")):
            _, stats = m.apply_inference([{}], [], [])
            self.assertEqual(stats["skipped_no_vocab"], 1)

    def test_typed_answer_and_partial_failure(self):
        with patch.object(m, "decisions_call", return_value=typed()):
            self.assertTrue(m.infer_one({}, NICHES, PRIOS, api_key="test")["ok"])
        with patch.object(m, "decisions_call", return_value=typed(priority="invented")):
            result = m.infer_one({}, NICHES, PRIOS, api_key="test")
            self.assertFalse(result["ok"])
            self.assertIsNone(result["priority"])

    def test_wrong_answer_type_rejected(self):
        data = typed()
        data["answers"]["niche"]["type"] = "score"
        with patch.object(m, "decisions_call", return_value=data):
            self.assertIsNone(m.infer_one({}, NICHES, PRIOS, api_key="test")["niche"])

    def test_concurrent_apply_and_audit(self):
        with patch.object(m, "decisions_call", return_value=typed()), patch.object(m, "get_openrouter_api_key", return_value="test"):
            out, stats = m.apply_inference(copy.deepcopy(FIXTURES), NICHES, PRIOS)
        self.assertEqual(stats["priority_ok"], 3)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(out[0]["classification"]["id"], "fixture-decision")
        self.assertEqual(out[0]["ad_url"], FIXTURES[0]["ad_url"])

    def test_failure_removes_obsolete_labels(self):
        with patch.object(m, "decisions_call", side_effect=RuntimeError("provider down")), patch.object(m, "get_openrouter_api_key", return_value="test"):
            out, stats = m.apply_inference([{"niche": "obsolete", "priority": "obsolete"}], NICHES, PRIOS)
        self.assertEqual((out[0]["niche"], out[0]["priority"], stats["failed"]), ("", "", 1))

    def test_http_400_not_retried(self):
        error = urllib.error.HTTPError(m.DECISIONS_URL, 400, "Bad Request", Message(), io.BytesIO(b"{}"))
        with patch.object(m.urllib.request, "urlopen", side_effect=error) as request:
            with self.assertRaises(RuntimeError):
                m.decisions_call({}, "test")
            self.assertEqual(request.call_count, 1)

    def test_http_429_retried(self):
        error = urllib.error.HTTPError(m.DECISIONS_URL, 429, "Rate limited", Message(), io.BytesIO(b"{}"))
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(typed()).encode()
        with patch.object(m.urllib.request, "urlopen", side_effect=[error, response]) as request, patch.object(m.time, "sleep"):
            self.assertEqual(m.decisions_call({}, "test")["id"], "fixture-decision")
            self.assertEqual(request.call_count, 2)

    def test_assembly_only_changes_niche_and_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = assemble(copy.deepcopy(FIXTURES), Path(tmp) / "before")
            after_candidates = copy.deepcopy(FIXTURES)
            for c in after_candidates:
                c.update(niche="Animaux", priority="2 Important")
            after = assemble(after_candidates, Path(tmp) / "after")
            self.assertEqual(len(after), 3)
            for a, b in zip(before, after):
                self.assertEqual(len(b), 75)
                self.assertEqual(b[4], "Animaux")
                self.assertEqual(b[6], "2 Important")
                self.assertEqual([v for i, v in enumerate(a) if i not in (4, 6)], [v for i, v in enumerate(b) if i not in (4, 6)])


def assemble(candidates, directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "c.json").write_text(json.dumps(candidates))
    (directory / "e.json").write_text("[]")
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name("assemble_rows.py")),
        "--candidates", str(directory / "c.json"), "--enriched", str(directory / "e.json"),
        "--output", str(directory / "rows.json"), "--products-out", str(directory / "p.json")],
        capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise RuntimeError(proc.stderr)
    return json.loads((directory / "rows.json").read_text())


def run_live(report):
    vocab = m.load_or_fetch_vocab()
    out, stats = m.apply_inference(copy.deepcopy(FIXTURES), vocab["niches"], vocab["priorities"])
    assert stats["failed"] == 0, stats
    assert stats["niche_ok"] == stats["priority_ok"] == len(FIXTURES), stats
    expected = ["Animaux", "Cuisine", "Sport"]
    for c, niche in zip(out, expected):
        assert c["niche"] == niche, (c["product_title"], c["niche"], niche)
        assert c["priority"] in vocab["priorities"] and c["priority"] != "5 Lancé", c
        assert c["classification"]["model"].startswith("typesafe/jev-1.14"), c
        assert c["classification"]["id"], c
    with tempfile.TemporaryDirectory() as tmp:
        rows = assemble(out, Path(tmp))
        assert len(rows) == len(out) and all(len(r) == 75 for r in rows)
        assert all(r[4] in vocab["niches"] and r[6] in vocab["priorities"] for r in rows)
    result = {"test_data": "Explicit fixtures, not scraped products", "model": m.DEFAULT_MODEL,
              "vocab": vocab, "stats": stats, "candidates": out, "rows_verified": len(rows)}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    for c in out:
        print(f"LIVE {c['product_title']}: {c['niche']} / {c['priority']} ({c['classification']['model']})")
    print(f"LIVE PASSED: report {report}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("/tmp/tt_jev114_live.json"))
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Offline))
    if not result.wasSuccessful():
        sys.exit(1)
    if args.live:
        run_live(args.report)
