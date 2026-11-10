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
            self.assertEqual(None if got["amount_low"] is None else int(got["amount_low"]), want["amount_low"], where)
            if not want.get("known_parser_defect_amount_high"):
                self.assertEqual(None if got["amount_high"] is None else int(got["amount_high"]), want["amount_high"], where)
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
        # Ticker og beløbets nedre grænse findes trods sideskiftet. KENDT FEJL I DEN ÆLDRE SKABELON (ikke rettet i runde 1,
        # fordi 2021-2026 skal være uændret): beløbets øvre grænse ($50,000 efter sideskiftet) findes ikke. PDF/tekst siger
        # 15001-50000. Når fejlen rettes, skal denne test opdateres til 50000.0.
        self.assertEqual((last["ticker"], last["amount_low"]), ("RBLX", 15001.0))
        self.assertIsNone(last["amount_high"])
        # parse_house_ptr.py markerer den slags med en advarsel i stedet for at gætte
        self.assertTrue(any("øvre grænse" in w for w in ph.amount_warnings(res["trades"])))

    def test_owner_dc_and_exchange(self):
        res = self.run_case("2026_20033804")
        self.assertEqual({t["owner"] for t in res["trades"]}, {"dependent child"})
        self.assertEqual({ph.trade_type(t) for t in res["trades"]}, {"exchange"})

    def test_exact_amount_has_no_upper_bound(self):
        res = self.run_case("2024_20024375")
        self.assertEqual((res["trades"][0]["amount_low"], res["trades"][0]["amount_high"]), (360.0, None))


@unittest.skipUnless(LEGACY_TEXT.is_dir(), "data/legacy_text mangler")
class RegressionAgainstOriginalParser(unittest.TestCase):
    """Den nye parser skal give præcis samme resultat som den oprindelige på alle tekster fra 2021-2026."""

    def test_identical_on_all_legacy_texts(self):
        spec = importlib.util.spec_from_file_location("orig_parser", FIX / "house_ptr_parser_v0_original.py")
        orig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(orig)
        n = n_trades = 0
        for path in sorted(LEGACY_TEXT.glob("*/*.txt")):
            text = path.read_text(encoding="utf-8")
            old, new = orig.parse_ptr_text(text), hp.parse_ptr_text(text)
            n += 1
            n_trades += len(old["trades"])
            self.assertEqual(new["header"], old["header"], path)
            self.assertEqual(new["warnings"], old["warnings"], path)
            self.assertEqual(len(new["trades"]), len(old["trades"]), path)
            for o, nw in zip(old["trades"], new["trades"]):
                self.assertEqual({k: nw[k] for k in o}, o, path)      # alle gamle felter uændrede (nye felter er tilføjet)
        self.assertGreater(n, 3000)
        self.assertGreater(n_trades, 27000)


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
        self.assertIn("0 nye, 3 fra cache", out)
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
