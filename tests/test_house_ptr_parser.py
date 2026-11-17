"""
Tests af ingestion/house_ptr_parser.py og ingestion/parse_house_ptr.py.

Kør (fra projektmappen):
    python3 tests/test_house_ptr_parser.py -v          (eller:  pytest tests/test_house_ptr_parser.py)

Fixtures (tests/fixtures/ptr/):
  {doc}.txt / {doc}.layout.txt / {doc}.expected.json   19 rigtige 2016-PDF'er (alle med tekstlag). Teksterne er
        udtrukket med pypdf (almindelig + layout). De forventede handler er skrevet ned ved at se hver side af PDF'en som billede;
        de er IKKE kopieret fra parserens output. Hvis PDF og parser er uenige, er det PDF'en, der gælder.
  legacy/{år}_{doc}.txt / .expected.json             9 tekster fra data/legacy_text med specialtilfælde (NUL-tegn,
        sideskift, små bogstaver i ticker, short/option, DC-ejer, exchange). Forventede handler er skrevet ud fra at læse
        teksten (PDF'erne findes ikke for 2021-2026).
  checked/{år}_{doc}.checked.txt (+ .txt / .layout.txt)   26 indberetninger fra 2016-2025 (t2016, old_2021, new_2022), hvor
        hver handel er aflæst fra PDF-siden (billede) i runde 2 og skrevet ned i et kompakt format (se checked/README.txt),
        uden at kopiere parserens output. CheckedAgainstPdfTests sammenligner ALLE felter; KNOWN_PARSER_DEVIATIONS i testen
        lister de få steder, hvor parseren (bevidst uændret) afviger fra PDF'en.
  fixtures/regression_changes_2021_2026.json         de felter, der ændres ift. den oprindelige parser, og hvorfor.
  pdf/                                               3 rigtige PDF'er til test af parse_house_ptr.py (inkl. en scannet).
  house_ptr_parser_v0_original.py                    parseren FØR t2016 blev tilføjet (til regressionstest).
"""

from __future__ import annotations

import contextlib
import csv
import importlib.util
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
PTR = FIX / "ptr"
LEGACY_TEXT = ROOT / "data" / "legacy_text" / "house_cache"
sys.path.insert(0, str(ROOT / "ingestion"))

import house_ptr_parser as hp  # noqa: E402
import parse_house_ptr as ph  # noqa: E402

MULTI_PAGE_REQUIRED = {"20004487", "20004720", "20004906", "20005220"}


def norm(s: str) -> str:
    """Sammenligning uden hensyn til store/små bogstaver, mellemrum og tegnsætning (tekstlaget har små-versal-støj)."""
    return re.sub(r"[\W_]+", "", s or "").casefold()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def t2016_docs() -> list[str]:
    return sorted(p.name.split(".")[0] for p in PTR.glob("*.expected.json"))


def parse_t2016(doc: str) -> dict:
    plain = (PTR / f"{doc}.txt").read_text(encoding="utf-8")
    layout = (PTR / f"{doc}.layout.txt").read_text(encoding="utf-8")
    return hp.parse_ptr_text(plain, layout)


def as_row(t: dict) -> dict:
    """Parserens handel -> de felter, der sammenlignes (samme logik som parse_house_ptr bruger til CSV)."""
    return {
        "owner": t["owner"], "ticker": t["ticker"], "type": ph.trade_type(t),
        "transaction_date": t["transaction_date"], "notification_date": t["notification_date"],
        "amount_low": None if t["amount_low"] is None else int(t["amount_low"]),
        "amount_high": None if t["amount_high"] is None else int(t["amount_high"]),
    }


class T2016FixtureTests(unittest.TestCase):
    def test_fixture_coverage(self):
        docs = set(t2016_docs())
        self.assertGreaterEqual(len(docs), 10)
        self.assertTrue(MULTI_PAGE_REQUIRED <= docs, "de flersidede 2016-PDF'er skal være med")
        total = sum(load_json(PTR / f"{d}.expected.json")["n_trades"] for d in docs)
        self.assertGreaterEqual(total, 100)

    def test_all_manually_checked_t2016_documents(self):
        for doc in t2016_docs():
            with self.subTest(doc=doc):
                exp = load_json(PTR / f"{doc}.expected.json")
                self.assertEqual(exp["n_trades"], len(exp["trades"]))
                res = parse_t2016(doc)
                self.assertEqual(res["template"], "t2016")
                self.assertEqual(res["warnings"], [], "ingen advarsler forventes")
                self.assertEqual(res["header"]["member"], exp["member"])
                self.assertEqual(res["header"]["state_dst"], exp["state_dst"])
                self.assertEqual(res["header"]["signed_date"], exp["signed_date"])
                self.assertEqual(len(res["trades"]), len(exp["trades"]), "antal handler")
                for i, (got, want) in enumerate(zip(res["trades"], exp["trades"]), 1):
                    where = f"{doc} handel {i} ({want['asset']})"
                    row = as_row(got)
                    for key in ("owner", "ticker", "type", "transaction_date", "notification_date",
                                "amount_low", "amount_high"):
                        self.assertEqual(row[key], want[key], f"{where}: {key}")
                    self.assertEqual(norm(got["asset"]), norm(want["asset"]), f"{where}: asset")
                    self.assertEqual(norm(got["sub_holding"]), norm(want["sub_holding"]), f"{where}: sub_holding")
                    self.assertEqual(norm(got["description"]), norm(want["description"]), f"{where}: description")
                    inc, why = ph.include_decision(got)
                    self.assertEqual(inc, want["include_v1"], f"{where}: include_v1 ({why})")

    def test_page_break_in_the_middle_of_a_trade(self):
        # 20004487: handel 7, 17 og 27 er delt mellem to sider (kernen på den ene side, subholding på den næste)
        res = parse_t2016("20004487")
        for idx in (7, 17, 27):
            t = res["trades"][idx - 1]
            self.assertTrue(t["sub_holding"], f"handel {idx} mangler subholding efter sideskift")
            self.assertEqual(t["filing_status"], "New")

    def test_lowercase_type_code_and_ticker(self):
        # 20005154 har små bogstaver i tekstlaget ('s' som typekode, 'Oa' som ticker)
        res = parse_t2016("20005154")
        self.assertEqual([t["ticker"] for t in res["trades"]], ["OA", "OA"])
        self.assertEqual([t["side"] for t in res["trades"]], ["buy", "sell"])

    def test_plain_text_alone_is_not_silently_empty(self):
        plain = (PTR / "20004419.txt").read_text(encoding="utf-8")
        res = hp.parse_ptr_text(plain)               # ingen layout-tekst
        self.assertEqual(res["trades"], [])
        self.assertTrue(any("layout" in w for w in res["warnings"]), res["warnings"])
        res2 = hp.parse_ptr_text(plain, "   \n")     # tom layout-tekst
        self.assertTrue(any("layout" in w for w in res2["warnings"]))

    def test_unknown_template_gives_clear_warning(self):
        text = "Periodic Transaction Report\nName: Hon. Test Person Status: Member State/District: XX01\n" \
               "Date Amount Something Else\nAAPL P 01/02/2017 01/03/2017 $1,001 - $15,000\n" * 3
        res = hp.parse_ptr_text(text)
        self.assertEqual(res["template"], "unknown")
        self.assertEqual(res["trades"], [])
        self.assertTrue(any("ukendt skabelon" in w for w in res["warnings"]), res["warnings"])

    def test_empty_text_is_flagged(self):
        res = hp.parse_ptr_text("\x00\x00 \n")
        self.assertEqual(res["template"], "empty")
        self.assertTrue(any("ingen tekst" in w for w in res["warnings"]))

    def test_warning_when_filing_status_line_missing(self):
        plain = (PTR / "20004485.txt").read_text(encoding="utf-8")
        layout = (PTR / "20004485.layout.txt").read_text(encoding="utf-8")
        broken = re.sub(r"^\s*FILINg STATUS: New\s*$", "", layout, count=1, flags=re.M)
        self.assertNotEqual(broken, layout)
        res = hp.parse_ptr_text(plain, broken)
        self.assertEqual(len(res["trades"]), 2)
        self.assertTrue(res["warnings"])

    def test_warning_when_a_trade_row_cannot_be_read(self):
        plain = (PTR / "20004485.txt").read_text(encoding="utf-8")
        layout = (PTR / "20004485.layout.txt").read_text(encoding="utf-8")
        broken = re.sub(r"(\(APA\)\s+)S(\s+01/15/2016)", r"\1X\2", layout, count=1)
        self.assertNotEqual(broken, layout)
        res = hp.parse_ptr_text(plain, broken)
        self.assertTrue(any("to datoer" in w for w in res["warnings"]), res["warnings"])

    def test_owner_check_against_asset_class_details(self):
        plain = (PTR / "20004485.txt").read_text(encoding="utf-8")
        layout = (PTR / "20004485.layout.txt").read_text(encoding="utf-8")
        broken = re.sub(r"^(\s*)SP(\s+Apache)", r"\1JT\2", layout, count=1, flags=re.M)
        self.assertNotEqual(broken, layout)
        res = hp.parse_ptr_text(plain, broken)
        self.assertEqual(res["trades"][0]["owner"], "joint")
        self.assertTrue(any("Asset Class Details" in w for w in res["warnings"]), res["warnings"])

    def test_layout_text_from_older_pypdf_versions(self):
        # pypdf 4.x-5.x klistrer typekoden til aktienavnet ("(LUK)S") og indsætter mellemrum i overskrifter
        # ("a SSet   c laSS   DetailS"). Samme handler skal findes.
        plain = (PTR / "20004642.txt").read_text(encoding="utf-8")
        layout = (PTR / "20004642.layout.txt").read_text(encoding="utf-8")
        glued = re.sub(r"(\([A-Za-z]+\))\s{2,}([PS])(\s+\d\d/)", r"\1\2\3", layout)
        glued = glued.replace("aSSeT clASS DeTAIlS", "a SSeT   c lASS   DeTAIlS").replace("INITIAl PublIc OfferINgS", "INITIAl   PublIc     O fferINgS")
        self.assertNotEqual(glued, layout)
        self.assertIn("(LUK)S", glued)
        a, b = hp.parse_ptr_text(plain, layout), hp.parse_ptr_text(plain, glued)
        self.assertEqual(b["warnings"], [])
        self.assertEqual([as_row(t) for t in a["trades"]], [as_row(t) for t in b["trades"]])
        self.assertEqual(len(b["trades"]), 13)

    def test_repeated_table_header_is_not_treated_as_data(self):
        res = parse_t2016("20004720")                 # tabelhoved gentages på hver side, også midt i en handel
        self.assertEqual(len(res["trades"]), 20)
        self.assertTrue(all(t["description"].lower().startswith("annuity") for t in res["trades"]))


class InferAssetTypeTests(unittest.TestCase):
    def check(self, asset, ticker, expected, desc=""):
        self.assertEqual(hp.infer_asset_type(asset, ticker, desc)[0], expected, asset)

    def test_rules(self):
        self.check("Apple Inc. (AAPL)", "AAPL", "ST")
        self.check("Vanguard Intermediate-Term Corporate Bond ETF (VCIT)", "VCIT", "ET")   # bond-ETF er en ETF
        self.check("First Trust NASDAQ Technology Dividend Index Fund (TDIV)", "TDIV", "ET")
        self.check("Vanguard 500 Index Fund Admiral Shares (VFIAX)", "VFIAX", "MF")
        self.check("Fidelity Magellan Fund (FMAGX)", "FMAGX", "MF")                         # 5 bogstaver med X
        self.check("Tesla Jan 2017 Call 250 (TSLA)", "TSLA", "OP")
        self.check("Global X NASDAQ-100 Covered Call ETF (QYLD)", "QYLD", "ET")             # ETF'er undtages
        self.check("Apple Inc. (AAPL)", "AAPL", "OP", desc="Exercised 25 call options")
        self.check("Grayscale Bitcoin Trust (GBTC)", "GBTC", "CR")
        self.check("Ford Motor Co 5.5% Notes due 2030 (F)", "F", "BD")
        self.check("Texas Pacific Land (TPL)", "TPL", "?")                                  # tvivl -> ikke med
        self.check("Some Private LLC", "", "")

    def test_only_stock_and_etf_are_included(self):
        for typ, expected in (("ST", True), ("ET", True), ("MF", False), ("OP", False), ("BD", False),
                              ("CR", False), ("?", False), ("", False)):
            t = {"asset_type": typ, "ticker": "XYZ", "option_like": False, "asset": "Name", "transaction_date": "2016-01-01",
                 "amount_low": 1001.0, "asset_type_source": "inferred"}
            self.assertEqual(ph.include_decision(t)[0], expected, typ)


class LegacyCaseTests(unittest.TestCase):
    """Specialtilfælde, den eksisterende parser allerede håndterer (tekster fra data/legacy_text)."""

    def run_case(self, key: str):
        exp = load_json(PTR / "legacy" / f"{key}.expected.json")
        text = (PTR / "legacy" / f"{key}.txt").read_text(encoding="utf-8")
        res = hp.parse_ptr_text(text)
        self.assertEqual(res["template"], exp["template"])
        self.assertEqual(res["warnings"], [])
        self.assertEqual(res["header"]["member"], exp["member"])
        self.assertEqual(res["header"]["state_dst"], exp["state_dst"])
        self.assertEqual(res["header"]["signed_date"], exp["signed_date"])
        self.assertEqual(len(res["trades"]), len(exp["trades"]))
        for i, (got, want) in enumerate(zip(res["trades"], exp["trades"]), 1):
            where = f"{key} handel {i}"
            self.assertEqual(got["owner"], want["owner"], where)
            if exp["template"] == "old_2021":
                # se nedenfor: i den ældre skabelon kan aktienavnet have forrige handels subholding/beskrivelse foran sig
                base = re.sub(r"\([^)]*\)\s*$", "", want["asset"])      # ticker kan stå efter et sideskift
                self.assertIn(norm(base), norm(got["asset"]), where)
            else:
                self.assertEqual(norm(got["asset"]), norm(want["asset"]), where)
            self.assertEqual(got["ticker"], want["ticker"], where)
            self.assertEqual(got["asset_type"], want["asset_type"], where)
            self.assertEqual(got["side"], want["side"], where)
            self.assertEqual(got["partial"], want["partial"], where)
            self.assertEqual(got["transaction_date"], want["transaction_date"], where)
            self.assertEqual(got["notification_date"], want["notification_date"], where)
            self.assertEqual(None if got["amount_low"] is None else float(got["amount_low"]), want["amount_low"], where)
            self.assertEqual(None if got["amount_high"] is None else float(got["amount_high"]), want["amount_high"], where)
            self.assertEqual(got["amount_flag"], want.get("amount_flag", ""), where + " amount_flag")
            if exp["template"] == "old_2021":
                # Kendt begrænsning i den ældre skabelon (uændret i denne runde): subholding/beskrivelse flyder sammen
                # med starten af NÆSTE handels aktienavn (og omvendt kan 'asset' få forrige handels subholding/beskrivelse
                # foran sig). Den rigtige tekst skal derfor være en begyndelse af parserens (asset: en slutning).
                self.assertTrue(norm(got["sub_holding"]).startswith(norm(want["sub_holding"])), where)
                self.assertTrue(norm(got["description"]).startswith(norm(want["description"])), where)
            else:
                self.assertEqual(norm(got["sub_holding"]), norm(want["sub_holding"]), where)
                self.assertEqual(norm(got["description"]), norm(want["description"]), where)
            self.assertEqual(got["option_like"], want["option_like"], where)
            self.assertEqual(got["short_related"], want["short_related"], where)
            self.assertEqual(ph.include_decision(got)[0], want["include_v1"], f"{where} include_v1")
            for v in got.values():
                if isinstance(v, str):
                    self.assertNotIn("\x00", v, where)
        return res

    def test_nul_characters_treasury_bill(self):
        text = (PTR / "legacy" / "2022_20021762.txt").read_text(encoding="utf-8")
        self.assertIn("\x00", text, "fixturen skal faktisk indeholde NUL-tegn")
        self.run_case("2022_20021762")

    def test_nul_characters_do_not_change_the_result(self):
        for key in ("2025_20030458", "2024_20024375", "2023_20023153"):
            with self.subTest(key=key):
                text = (PTR / "legacy" / f"{key}.txt").read_text(encoding="utf-8")
                self.assertIn("\x00", text)
                a = hp.parse_ptr_text(text)
                b = hp.parse_ptr_text(text.replace("\x00", ""))
                self.assertEqual(a["trades"], b["trades"])
                self.assertTrue(a["trades"])
                self.run_case(key)

    def test_option_marking(self):
        for key in ("2025_20030458", "2023_20023153"):
            res = self.run_case(key)
            self.assertTrue(res["trades"][0]["option_like"])
            self.assertFalse(ph.include_decision(res["trades"][0])[0])
            self.assertTrue(res["trades"][0]["is_stock"] is False)     # is_stock tager højde for option_like

    def test_short_marking_and_lowercase_tickers(self):
        res = self.run_case("2021_20018553")
        flagged = [t["ticker"] for t in res["trades"] if t["short_related"]]
        self.assertEqual(flagged, ["AI", "CDNA", "DASH", "DASH", "HOLX", "QDEL", "QDEL"])
        self.assertTrue(all(t["is_stock"] for t in res["trades"]))      # short-handler beholdes, men er markeret

    def test_lowercase_ticker_single(self):
        res = self.run_case("2022_20020733")
        self.assertEqual(res["trades"][0]["ticker"], "YUMC")

    def test_lowercase_ticker_warrant_is_not_in_v1(self):
        res = self.run_case("2021_20019877")
        self.assertEqual(res["trades"][0]["ticker"], "KPLTW")
        self.assertTrue(res["trades"][0]["is_stock"])                   # gammel regel: kode ST
        self.assertFalse(ph.include_decision(res["trades"][0])[0])      # ny regel: 'Warrant' i navnet

    def test_page_break_in_the_middle_of_a_trade(self):
        res = self.run_case("2021_20020002")
        last = res["trades"][-1]
        # Beløbet "$15,001 -" står på side 1 og "$50,000" på side 2 (kontrolleret mod PDF-siderne). Runde 1 gav
        # amount_high = None; fra runde 2 hentes den øvre grænse og kontrolleres mod Husets standardinterval.
        self.assertEqual((last["ticker"], last["amount_low"], last["amount_high"], last["amount_flag"]),
                         ("RBLX", 15001.0, 50000.0, "repaired_split"))
        self.assertEqual(ph.amount_warnings(res["trades"]), [])

    def test_page_break_amount_is_not_guessed(self):
        """Passer den fundne øvre grænse ikke til standardintervallet, bliver amount_high tom og beløbet markeret."""
        low, high, flag, _ = hp._parse_amount(hp.AMOUNT_RE.search("$15,001 -"), " Filing Id #1 (X) [ST] $99,000 ")
        self.assertEqual((low, high, flag), (15001.0, None, "incomplete_high"))
        low, high, flag, _ = hp._parse_amount(hp.AMOUNT_RE.search("$15,001 -"), "")
        self.assertEqual((low, high, flag), (15001.0, None, "incomplete_high"))
        trades = [{"amount_flag": "incomplete_high"}]
        self.assertTrue(any("øvre grænse" in w for w in ph.amount_warnings(trades)))

    def test_owner_dc_and_exchange(self):
        res = self.run_case("2026_20033804")
        self.assertEqual({t["owner"] for t in res["trades"]}, {"dependent child"})
        self.assertEqual({ph.trade_type(t) for t in res["trades"]}, {"exchange"})

    def test_exact_amount_is_kept_with_decimals_and_marked(self):
        res = self.run_case("2024_20024375")
        t = res["trades"][0]
        self.assertEqual((t["amount_low"], t["amount_high"], t["amount_flag"]), (360.0, 360.0, "exact"))
        m = hp.AMOUNT_RE.search("$268.95 g")
        self.assertEqual(hp._parse_amount(m)[:3], (268.95, 268.95, "exact"))
        self.assertEqual(hp._parse_amount(hp.AMOUNT_RE.search("$.01"))[:3], (0.01, 0.01, "exact"))

    def test_over_amount_is_open_interval(self):
        self.assertEqual(hp._parse_amount(hp.AMOUNT_RE.search("Spouse/DC Over $1,000,000"))[:3],
                         (1000000.0, None, "open_over"))
        self.assertEqual(hp._parse_amount(hp.AMOUNT_RE.search("Over $50,000,000"))[:3], (50000000.0, None, "open_over"))


@unittest.skipUnless(LEGACY_TEXT.is_dir(), "data/legacy_text mangler")
class RegressionAgainstOriginalParser(unittest.TestCase):
    """
    Den nye parser skal give præcis samme resultat som den ORIGINALE på alle tekster fra 2021-2026, bortset fra de
    forskelle, der er listet i fixtures/regression_changes_2021_2026.json (hver med en årsag, som testen kontrollerer):
      exact          præcist beløb ("$360.00"): low = high = beløbet (før: low uden decimaler, high tom)
      repaired_split beløb delt af sideskift: øvre grænse hentet, kun hvis den passer til Husets standardinterval
      extra_trade    handel, der før gik tabt, fordi typekoden var klistret til aktienavnet (9 filer, 1 handel hver)
    """
    KEYS = ["owner", "asset", "ticker", "asset_type", "side", "partial", "transaction_date", "notification_date",
            "amount_low", "amount_high", "sub_holding", "description", "option_like", "short_related", "is_stock"]

    def diffs(self):
        spec = importlib.util.spec_from_file_location("orig_parser", FIX / "house_ptr_parser_v0_original.py")
        orig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(orig)
        fields, extra, n, n_trades = [], [], 0, 0
        for path in sorted(LEGACY_TEXT.glob("*/*.txt")):
            text = path.read_text(encoding="utf-8", errors="surrogateescape")
            old, new = orig.parse_ptr_text(text), hp.parse_ptr_text(text)
            rel = f"{path.parent.name}/{path.stem}"
            n += 1
            n_trades += len(old["trades"])
            self.assertEqual(new["header"], old["header"], path)
            if len(new["trades"]) == len(old["trades"]):
                self.assertEqual(new["warnings"], old["warnings"], path)
                for i, (o, nw) in enumerate(zip(old["trades"], new["trades"])):
                    for k in self.KEYS:
                        if o[k] != nw[k]:
                            fields.append({"path": rel, "trade": i, "field": k, "old": o[k], "new": nw[k],
                                           "why": nw["amount_flag"]})
            else:
                proj = lambda t: tuple(repr(t[k]) for k in self.KEYS)       # noqa: E731
                import collections
                co, cn = collections.Counter(map(proj, old["trades"])), collections.Counter(map(proj, new["trades"]))
                added, removed = cn - co, co - cn
                extra.append({"path": rel, "old_n": len(old["trades"]), "new_n": len(new["trades"]),
                              "old_warnings": old["warnings"], "new_warnings": new["warnings"],
                              "neighbours_with_changed_asset_or_sub_or_description": sum(removed.values()),
                              "added_trades": [[t["ticker"], t["transaction_date"], t["side"], t["amount_low"],
                                                t["amount_high"]] for t in new["trades"] if proj(t) in added]})
        return fields, extra, n, n_trades

    def test_only_the_documented_differences(self):
        fields, extra, n, n_trades = self.diffs()
        self.assertGreater(n, 3000)
        self.assertGreater(n_trades, 27000)
        doc = load_json(FIX / "regression_changes_2021_2026.json")
        self.assertEqual(fields, doc["field_changes"])
        self.assertEqual(extra, doc["extra_trade_docs"])

    def test_every_difference_has_its_reason(self):
        doc = load_json(FIX / "regression_changes_2021_2026.json")
        by = {"exact": 0, "repaired_split": 0}
        for c in doc["field_changes"]:
            self.assertIn(c["why"], by, c)
            by[c["why"]] += 1
            if c["why"] == "exact":
                if c["field"] == "amount_high":
                    self.assertIsNotNone(c["new"])
                    self.assertTrue(c["old"] is None or c["old"] == int(c["new"]), c)
                else:
                    self.assertEqual(c["field"], "amount_low", c)
                    self.assertEqual(int(c["new"]), int(c["old"]), c)           # decimaler tilføjet, hele dollars uændret
            else:
                self.assertEqual(c["field"], "amount_high", c)
                self.assertIsNone(c["old"], c)
        for c in doc["field_changes"]:
            if c["why"] == "repaired_split":
                lows = [d for d in doc["field_changes"] if d["path"] == c["path"] and d["trade"] == c["trade"]]
                self.assertEqual(len(lows), 1, "kun amount_high ændres ved sideskift-reparation")
                self.assertIn(c["new"], hp.STANDARD_RANGES.values(), c)
        self.assertEqual(by, {"exact": 182, "repaired_split": 32})
        self.assertEqual(len(doc["extra_trade_docs"]), 9)
        for e in doc["extra_trade_docs"]:
            self.assertEqual(e["new_n"], e["old_n"] + 1, e)
            self.assertTrue(any("markører" in w for w in e["old_warnings"]), e)    # fejlen blev før meldt som advarsel
            self.assertEqual(e["new_warnings"], [], e)
            self.assertTrue(e["added_trades"], e)


CHECKED = PTR / "checked"


def read_checked(path: Path) -> tuple[dict, list[dict]]:
    """Læser en .checked.txt (se checked/README.txt): ({header-nøgler}, [handler])."""
    meta, trades = {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            for part in line[2:].split(" | "):
                k, _, v = part.partition("=")
                meta[k.strip()] = v.strip()
            continue
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("|")
        assert len(f) == 11, (path.name, line)
        owner, asset, ticker, code, typ, tdate, ndate, amount, sub, desc, status = f
        low, _, high = amount.partition("-")
        exact = "-" not in amount
        trades.append({"owner": owner, "asset": asset, "ticker": ticker, "code": code, "type": typ, "tdate": tdate,
                       "ndate": ndate, "low": float(low), "high": float(low) if exact else (float(high) if high else None),
                       "open": (not exact) and not high, "exact": exact, "sub": sub, "desc": desc, "status": status})
    return meta, trades


# Steder, hvor parseren er uenig med PDF'en (checked-filen er PDF'en). Dem rettes ikke i runde 2, fordi reglen er, at
# 2021-2026 skal være uændret ift. den oprindelige parser; de står her med PARSERENS nuværende værdi, så testen fejler,
# hvis de ændres (så er fixturen/afvigelsen til at tage stilling til). (doc, handelsnr, felt) -> parserens værdi.
KNOWN_PARSER_DEVIATIONS = {
    # new_2022, ren tekst: en beskrivelse over to linjer ("...dated" / "2/9/2022.") - anden linje havner i NÆSTE handels aktienavn,
    # og dermed mister næste handel sin ejerkode (JT står midt i aktienavnet).
    ("2024_20024581", 2, "description"): "Vested shares sold to separate trust not for the benefit of filer or spouse pursuant to forward contract dated",
    ("2024_20024581", 3, "owner"): "self",
    ("2024_20024581", 3, "asset"): "2/9/2022. JT Seattle WA MN LT PW 3.55% PWR Util Due 05/01/45",
    # PDF: "Trimer Capital Partners I LP (GLAS) - Core"; (GLAS) er et fondsfamilie-navn, ikke en børsticker. Parserens regel
    # (parentes med store bogstaver) tager det som ticker; typen er HN, så handlen er ikke med i v1 uanset.
    ("2025_20032187", 4, "ticker"): "GLAS",
    ("2025_20032187", 5, "ticker"): "GLAS",
}


def checked_docs() -> list[str]:
    return sorted(p.name[:-len(".checked.txt")] for p in CHECKED.glob("*.checked.txt"))


class CheckedAgainstPdfTests(unittest.TestCase):
    """
    Indberetninger fra 2016-2026, hvor hver handel er aflæst direkte fra PDF-siden (billede) i runde 2 og skrevet ned i
    checked/*.checked.txt uden at kopiere parserens output. Dækker t2016 (2016-2018), old_2021 (2018-2022, inkl. 5 af de
    filer, der før gav markør-mismatch) og new_2022 (2022-2025) samt åbne ("Over") og præcise beløb.
    """

    def test_coverage_across_years_and_templates(self):
        docs = checked_docs()
        self.assertGreaterEqual(len(docs), 20)
        by_year, templates = {}, set()
        for d in docs:
            meta, _ = read_checked(CHECKED / f"{d}.checked.txt")
            by_year.setdefault(d[:4], []).append(d)
            templates.add((d[:4], meta["template"]))
        for y in ("2017", "2018", "2019", "2020"):
            self.assertGreaterEqual(len(by_year[y]), 2, y)
        self.assertIn(("2018", "t2016"), templates)
        self.assertIn(("2018", "old_2021"), templates)
        mismatch = [d for d in docs if "markoer-mismatch" in read_checked(CHECKED / f"{d}.checked.txt")[0].get("note", "")]
        self.assertGreaterEqual(len(mismatch), 3)

    def test_every_checked_document(self):
        for d in checked_docs():
            with self.subTest(doc=d):
                meta, want = read_checked(CHECKED / f"{d}.checked.txt")
                plain = (CHECKED / f"{d}.txt").read_text(encoding="utf-8")
                lay_path = CHECKED / f"{d}.layout.txt"
                layout = lay_path.read_text(encoding="utf-8") if lay_path.is_file() else None
                res = hp.parse_ptr_text(plain, layout)
                self.assertEqual(res["template"], meta["template"])
                if meta.get("source") != "plain":
                    self.assertEqual(res["header"]["signed_date"], meta["signed"])
                    self.assertEqual(res["header"]["state_dst"], meta["state"])
                self.assertEqual(res["header"]["member"], meta["member"])
                self.assertEqual(len(res["trades"]), len(want), "antal handler")
                for i, (got, w) in enumerate(zip(res["trades"], want), 1):
                    where = f"{d} handel {i} ({w['asset']})"
                    pdf = {
                        "owner": w["owner"], "asset": w["asset"], "ticker": w["ticker"].upper(),
                        "asset_type": w["code"] or got["asset_type"], "type": w["type"], "transaction_date": w["tdate"],
                        "notification_date": w["ndate"], "amount_low": w["low"], "amount_high": w["high"],
                        "amount_flag": "open_over" if w["open"] else ("exact" if w["exact"] else ""),
                        "sub_holding": w["sub"], "description": w["desc"], "filing_status": w["status"],
                    }
                    parsed = dict(got, type=ph.trade_type(got))
                    for field, want_val in pdf.items():
                        have = parsed[field]
                        if field in ("asset", "sub_holding", "description"):
                            have, want_val = norm(have), norm(want_val)
                        key = (d, i, field)
                        if key in KNOWN_PARSER_DEVIATIONS:
                            known = KNOWN_PARSER_DEVIATIONS[key]
                            known = norm(known) if field in ("asset", "sub_holding", "description") else known
                            self.assertNotEqual(have, want_val, where + f" {field}: afvigelsen er rettet, fjern den fra listen")
                            self.assertEqual(have, known, where + f" {field}")
                        else:
                            self.assertEqual(have, want_val, where + f" {field}")

    def test_old_2021_layout_fixes_owner_and_text_that_plain_text_gets_wrong(self):
        """20015042 (Pelosi): ejerkoden SP er i almindelig tekst klistret til forrige handels beskrivelse ("...$1600.sP")."""
        plain = (CHECKED / "2020_20015042.txt").read_text(encoding="utf-8")
        layout = (CHECKED / "2020_20015042.layout.txt").read_text(encoding="utf-8")
        self.assertEqual({t["owner"] for t in hp.parse_ptr_text(plain)["trades"]}, {"self"})          # almindelig tekst: forkert
        res = hp.parse_ptr_text(plain, layout)
        self.assertEqual(res["parse_source"], "layout")
        self.assertEqual({t["owner"] for t in res["trades"]}, {"spouse"})                              # PDF: SP
        self.assertTrue(all(t["option_like"] for t in res["trades"]))                                 # 'call options' i beskrivelsen

    def test_t2016_without_layout_text_uses_plain_text_and_says_so(self):
        """20005968 (PDF fra anden producent, tomt layout-udtræk): 1 handel fra almindelig tekst + advarsel, aldrig stille 0 handler."""
        plain = (CHECKED / "2020_20005968.txt").read_text(encoding="utf-8")
        for layout in (None, "", "  \n"):
            res = hp.parse_ptr_text(plain, layout)
            self.assertEqual(len(res["trades"]), 1)
            self.assertEqual(res["parse_source"], "plain")
            self.assertTrue(any("layout-tekst mangler" in w for w in res["warnings"]))

    def test_unusable_layout_falls_back_to_plain_text(self):
        """Layout uden tabelhoved bruges ikke; så læses almindelig tekst (parse_source er ikke 'layout')."""
        plain = (CHECKED / "2018_20009624.txt").read_text(encoding="utf-8")
        res = hp.parse_ptr_text(plain, "intet der ligner en tabel\n")
        self.assertEqual(len(res["trades"]), 5)
        self.assertNotEqual(res.get("parse_source"), "layout")

    def test_legacy_old_2021_fixtures_with_layout_are_exact(self):
        """De fire old_2021-filer fra data/legacy_text er også tjekket mod PDF; med layout er asset/subholding/beskrivelse præcise."""
        for key in ("2021_20018553", "2021_20019877", "2021_20020002", "2022_20020733"):
            with self.subTest(key=key):
                exp = load_json(PTR / "legacy" / f"{key}.expected.json")
                plain = (PTR / "legacy" / f"{key}.txt").read_text(encoding="utf-8")
                layout = (PTR / "legacy" / f"{key}.layout.txt").read_text(encoding="utf-8")
                res = hp.parse_ptr_text(plain, layout)
                self.assertEqual(res["parse_source"], "layout")
                self.assertEqual(len(res["trades"]), len(exp["trades"]))
                for got, w in zip(res["trades"], exp["trades"]):
                    self.assertEqual(norm(got["asset"]), norm(w["asset"]))
                    self.assertEqual(norm(got["sub_holding"]), norm(w["sub_holding"]))
                    self.assertEqual(norm(got["description"]), norm(w["description"]))
                    self.assertEqual(got["owner"], w["owner"])
                    self.assertEqual((got["amount_low"], got["amount_high"]), (float(w["amount_low"]), float(w["amount_high"])))
                    self.assertEqual((got["option_like"], got["short_related"]), (w["option_like"], w["short_related"]))
                    self.assertEqual(ph.include_decision(got)[0], w["include_v1"])


class Round2RuleTests(unittest.TestCase):
    """Regler fra runde 2: LOCATION/COMMENTS, datomistanke, dubletter/sletninger, navne uden accenter."""

    def test_location_and_comments_are_read_and_lose_no_trade(self):
        d = PTR / "20004420"
        plain = d.with_suffix(".txt").read_text(encoding="utf-8")
        lay = (PTR / "20004420.layout.txt").read_text(encoding="utf-8")
        marker = "                    SUBHOLDINg OF: Brad and Ann Ashford Joint Account\n"
        self.assertIn(marker, lay)
        res = hp.parse_ptr_text(plain, lay.replace(marker, marker + "                    LOCATION: Omaha, NE, US\n"
                                                   "                    COMMENTS: Shares were\n                    sold in two lots\n"))
        self.assertEqual(res["warnings"], [])
        self.assertEqual(len(res["trades"]), 1)
        t = res["trades"][0]
        self.assertEqual((t["location"], t["comments"]), ("Omaha, NE, US", "Shares were sold in two lots"))
        self.assertEqual(t["sub_holding"], "Brad and Ann Ashford Joint Account")      # underskriften skylles ikke ind i subholding

    def test_date_suspect_reasons(self):
        f = hp.date_suspect_reason
        self.assertEqual(f("2018-03-01", "2018-04-01"), "")
        self.assertEqual(f("2018-04-01", "2018-04-01"), "")                           # samme dag er ok
        self.assertEqual(f("2018-04-02", "2018-04-01"), "efter_filing_date")
        self.assertEqual(f("2014-04-01", "2018-04-01"), "over_3_aar_foer_filing_date")
        self.assertEqual(f("2015-04-01", "2018-04-01"), "")                           # præcis 3 år er ikke mistænkt
        self.assertEqual(f("2081-01-01", "2018-04-01"), "efter_filing_date")
        self.assertEqual(f("1918-01-01", "2018-04-01"), "umuligt_aar")
        self.assertEqual(f("", "2018-04-01"), "ugyldig_dato")
        self.assertEqual(f("2018-03-01", ""), "filing_date_mangler")

    @staticmethod
    def row(doc, year="2020", filing="2020-05-01", ticker="AAA", ttype="buy", tdate="2020-04-01", low="1001.0", high="15000.0",
            status="New", include=True, reason="kode ST", member="José Sánchez", owner="self"):
        return {"year": year, "doc_id": doc, "member": member, "ticker": ticker, "asset": ticker + " Inc", "type": ttype,
                "transaction_date": tdate, "amount_low": low, "amount_high": high, "filing_status": status,
                "filing_date": filing, "include_v1": include, "include_reason": reason, "owner": owner}

    def test_duplicates_are_marked_in_the_later_filing_only(self):
        r = self.row
        rows = [r("200", filing="2020-05-01"), r("201", filing="2020-06-01", status="Amended", member="Jose Sanchez"),
                r("202", filing="2020-06-02", ticker="BBB"),
                r("203", filing="2020-06-03", tdate="2020-04-02")]        # anden dato: ingen dublet
        n = ph.finalize_trades(rows)
        self.assertEqual([x["duplicate_of"] for x in rows], ["", "200", "", ""])      # accenter ændrer ikke medlemmet
        self.assertEqual([x["include_v1"] for x in rows], [True, False, True, True])
        self.assertTrue(rows[1]["include_reason"].startswith("dublet af 200"))
        self.assertIn("kode ST", rows[1]["include_reason"])                              # grundreglen bevares
        self.assertEqual(n["duplicate"], 1)

    def test_identical_rows_in_the_same_filing_are_not_duplicates(self):
        rows = [self.row("300"), self.row("300")]
        ph.finalize_trades(rows)
        self.assertEqual([x["duplicate_of"] for x in rows], ["", ""])
        self.assertEqual([x["include_v1"] for x in rows], [True, True])

    def test_later_filing_with_more_identical_rows_marks_only_the_surplus_as_duplicates(self):
        rows = [self.row("400")] + [self.row("401", filing="2020-06-01") for _ in range(2)]
        ph.finalize_trades(rows)
        self.assertEqual([x["duplicate_of"] for x in rows], ["", "400", ""])

    def test_different_amount_is_not_a_duplicate(self):
        rows = [self.row("500"), self.row("501", filing="2020-06-01", low="15001.0", high="50000.0", status="Amended")]
        n = ph.finalize_trades(rows)
        self.assertEqual([x["duplicate_of"] for x in rows], ["", ""])
        self.assertEqual(n["amended_not_dup"], 1)

    def test_deleted_rows_and_the_trade_they_delete_are_excluded(self):
        rows = [self.row("600"), self.row("601", filing="2020-06-01", status="Deleted")]
        n = ph.finalize_trades(rows)
        self.assertEqual(rows[0]["deleted_by"], "601")
        self.assertEqual([x["include_v1"] for x in rows], [False, False])
        self.assertEqual((n["deleted_by"], n["deleted_rows"]), (1, 1))

    def test_date_suspect_rows_are_excluded_but_counted_and_the_date_is_kept(self):
        rows = [self.row("700", tdate="2021-01-01"), self.row("701", tdate="2020-04-01", ticker="CCC")]
        n = ph.finalize_trades(rows)
        self.assertEqual(rows[0]["date_suspect"], "efter_filing_date")
        self.assertEqual(rows[0]["transaction_date"], "2021-01-01")                    # datoen rettes ikke
        self.assertFalse(rows[0]["include_v1"])
        self.assertTrue(rows[1]["include_v1"])
        self.assertEqual(n["date_suspect"], 1)

    def test_rows_that_were_never_included_stay_excluded_with_their_own_reason(self):
        rows = [self.row("800", include=False, reason="aktivtype ?", tdate="2021-01-01")]
        ph.finalize_trades(rows)
        self.assertFalse(rows[0]["include_v1"])
        self.assertEqual(rows[0]["include_reason"], "aktivtype ?")

    def test_finalize_is_idempotent(self):
        rows = [self.row("900"), self.row("901", filing="2020-06-01"), self.row("902", tdate="2021-01-01", ticker="DDD")]
        ph.finalize_trades(rows)
        once = [dict(x) for x in rows]
        ph.finalize_trades(rows)
        self.assertEqual(rows, once)

    def test_fold_removes_accents_and_case(self):
        self.assertEqual(ph.fold("José SÁNCHEZ"), ph.fold("jose sanchez"))
        self.assertEqual(ph.fold("Nydia M. Velázquez"), "nydia m. velazquez")


class ParseHousePtrScriptTests(unittest.TestCase):
    """Ende-til-ende test af ingestion/parse_house_ptr.py i en midlertidig projektmappe (rigtige PDF'er fra fixtures)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ptrtest_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        raw = self.tmp / "data" / "raw"
        (raw / "ptr" / "2016").mkdir(parents=True)
        for name in ("20004419", "20006116", "9109787"):
            shutil.copy(PTR / "pdf" / f"{name}.pdf", raw / "ptr" / "2016" / f"{name}.pdf")
        rows = [
            ("2016", "20004419", "Brad", "Ashford", "NE02", "2016-01-15"),
            ("2016", "20006116", "Lou", "Barletta", "PA11", "2016-10-28"),
            ("2016", "9109787", "Brian", "Babin", "TX36", "2016-08-05"),
            ("2016", "20009999", "No", "Pdf", "XX01", "2016-09-01"),          # PDF mangler
            ("2017", "20010000", "Other", "Year", "XX02", "2017-01-01"),
        ]
        with (raw / "ptr_index.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["year", "doc_id", "first", "last", "prefix", "suffix", "state_dst", "filing_date", "pdf_path", "status"])
            for y, d, f, l, s, fd in rows:
                w.writerow([y, d, f, l, "Hon.", "", s, fd, f"data/raw/ptr/{y}/{d}.pdf", "ok"])
        self.patches = {
            "ROOT": self.tmp, "INDEX_PATH": raw / "ptr_index.csv", "RAW_DIR": raw / "ptr",
            "TEXT_DIR": self.tmp / "data" / "interim" / "text", "LAYOUT_DIR": self.tmp / "data" / "interim" / "text_layout",
            "OUT_DIR": self.tmp / "data" / "processed"}
        self.saved = {k: getattr(ph, k) for k in self.patches}
        for k, v in self.patches.items():
            setattr(ph, k, v)
        self.addCleanup(lambda: [setattr(ph, k, v) for k, v in self.saved.items()])

    def run_main(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ph.main(list(args))
        return rc, buf.getvalue()

    def read_csv(self, name):
        with (self.tmp / "data" / "processed" / name).open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_time_limit_resume_cache_scanned_and_merge(self):
        # 1) tiden er brugt med det samme: intet udtrukket, intet går tabt, alt er 'not_processed'/'pdf_missing'
        rc, out = self.run_main("2016", "--max-seconds", "0.000001")
        self.assertEqual(rc, 0)
        self.assertIn("ikke nået", out)
        status = {r["doc_id"]: r["status"] for r in self.read_csv("parse_report.csv")}
        self.assertEqual(status, {"20004419": "not_processed", "20006116": "not_processed",
                                  "9109787": "not_processed", "20009999": "pdf_missing"})
        self.assertFalse((self.tmp / "data" / "interim" / "text" / "2016" / "20004419.txt").exists())
        # 2) normal kørsel
        rc, out = self.run_main("2016")
        self.assertEqual(rc, 0)
        trades = self.read_csv("trades.csv")
        self.assertEqual(len(trades), 3)                                  # 1 + 2 handler, den scannede giver ingen
        self.assertEqual({t["doc_id"] for t in trades}, {"20004419", "20006116"})
        t0 = trades[0]
        self.assertEqual((t0["member"], t0["state_dst"], t0["owner"], t0["ticker"], t0["type"], t0["transaction_date"],
                          t0["notification_date"], t0["filing_date"], t0["amount_low"], t0["amount_high"],
                          t0["include_v1"], t0["template"]),
                         ("Brad Ashford", "NE02", "self", "DIS", "sell", "2016-01-04", "2016-01-04", "2016-01-15",
                          "15001", "50000", "True", "t2016"))
        scanned = self.read_csv("scanned.csv")
        self.assertEqual([(s["doc_id"], s["sider"]) for s in scanned], [("9109787", "2")])
        status = {r["doc_id"]: r["status"] for r in self.read_csv("parse_report.csv")}
        self.assertEqual(status["9109787"], "scanned")
        self.assertEqual(status["20004419"], "parsed")
        self.assertTrue((self.tmp / "data" / "interim" / "text" / "2016" / "20004419.txt").is_file())
        self.assertTrue((self.tmp / "data" / "interim" / "text_layout" / "2016" / "20004419.txt").is_file())
        # 3) cachen genbruges (ingen nyt udtræk)
        cache = self.tmp / "data" / "interim" / "text" / "2016" / "20004419.txt"
        before = cache.stat().st_mtime_ns
        rc, out = self.run_main("2016")
        self.assertIn("0 nye, 0 med layout efterhentet, 3 fra cache", out)
        self.assertEqual(cache.stat().st_mtime_ns, before)
        # 4) et andet år bevarer rækkerne for 2016
        rc, out = self.run_main("2017")
        self.assertEqual(len(self.read_csv("trades.csv")), 3)
        years = {r["year"] for r in self.read_csv("parse_report.csv")}
        self.assertEqual(years, {"2016", "2017"})

    def test_bad_arguments(self):
        for args in (["abc"], ["2020-2016"], ["2016", "--max-seconds"], ["2016", "--max-seconds", "0"], ["--nope"]):
            with self.subTest(args=args):
                rc, out = self.run_main(*args)
                self.assertEqual(rc, 2)
                self.assertIn("Brug:", out)


if __name__ == "__main__":
    unittest.main()
