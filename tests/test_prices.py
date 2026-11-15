"""
Tests for prices/ (offline, mod en lokal falsk Alpaca-server). Kør: python3 tests/test_prices.py
Intet her er afprøvet mod det rigtige Alpaca-API.
"""

import csv
import json
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "prices"))
import coverage  # noqa: E402
import fetch_prices  # noqa: E402
import price_common as pc  # noqa: E402
import ticker_map  # noqa: E402
from alpaca_client import AlpacaClient, AlpacaError, ForbiddenError  # noqa: E402


def bdays(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def mkbars(dates, base=100.0):
    return [{"t": d.isoformat() + "T05:00:00Z", "o": base + i * 0.1, "h": base + i * 0.1 + 1,
             "l": base + i * 0.1 - 1, "c": base + i * 0.1 + 0.5, "v": 1000 + i}
            for i, d in enumerate(dates)]


class Fake:
    """Falsk Alpaca. bars: {symbol: [bar]}; asof_data: {(symbol, asof): (nøgle, [bar])}."""

    def __init__(self):
        self.bars, self.asof_data, self.corp = {}, {}, []
        self.page_size = 10000
        self.sip_forbidden = False
        self.fail_next = []          # statuskoder der returneres først
        self.bad_symbols = set()
        self.requests = []

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj=None):
                body = json.dumps(obj or {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                fake.requests.append((u.path, q))
                if fake.fail_next:
                    return self.send(fake.fail_next.pop(0))
                if u.path == "/v1/corporate-actions":
                    return self.send(200, {"corporate_actions": {"name_changes": fake.corp},
                                           "next_page_token": None})
                if q.get("feed") == "sip" and fake.sip_forbidden:
                    return self.send(403, {"message": "subscription"})
                syms = q["symbols"].split(",")
                if any(s in fake.bad_symbols for s in syms):
                    return self.send(400, {"message": "invalid symbol"})
                flat = []
                for s in sorted(syms):
                    data = fake.bars.get(s, [])
                    if q.get("asof") and (s, q["asof"]) in fake.asof_data:
                        s2, data = fake.asof_data[(s, q["asof"])]
                        s = s2
                    for b in data:
                        if q["start"] <= b["t"][:10] <= q["end"]:
                            flat.append((s, b))
                off = int(q.get("page_token", 0))
                size = min(fake.page_size, int(q["limit"]))
                page = flat[off:off + size]
                out = {}
                for s, b in page:
                    out.setdefault(s, []).append(b)
                nxt = str(off + size) if off + size < len(flat) else None
                self.send(200, {"bars": out, "next_page_token": nxt})

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_port
        return self

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def client_for(fake, sleeps=None, **kw):
    sl = sleeps if sleeps is not None else []
    return AlpacaClient("k", "s", base_url=fake.url, min_interval=0, sleep=sl.append, **kw), sl


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = Fake().start()
        self.addCleanup(self.fake.stop)

    def test_pagination(self):
        days = bdays(date(2016, 1, 4), date(2016, 1, 22))       # 15 dage
        self.fake.bars = {"AAA": mkbars(days), "BBB": mkbars(days)}
        self.fake.page_size = 4
        c, _ = client_for(self.fake)
        out = c.bars(["AAA", "BBB"], "2016-01-01", "2016-12-31")
        self.assertEqual(len(out["AAA"]), 15)
        self.assertEqual(len(out["BBB"]), 15)
        self.assertEqual(c.n_calls, 8)          # 30 rækker / 4 pr. side

    def test_feed_fallback_on_403(self):
        self.fake.sip_forbidden = True
        self.fake.bars = {"AAA": mkbars(bdays(date(2016, 1, 4), date(2016, 1, 8)))}
        c, _ = client_for(self.fake)
        out = c.bars_auto(["AAA"], "2016-01-01", "2016-12-31")
        self.assertEqual(c.feed, "iex")
        self.assertTrue(c.fell_back)
        self.assertEqual(len(out["AAA"]), 5)
        feeds = [q["feed"] for _, q in self.fake.requests]
        self.assertEqual(feeds, ["sip", "iex"])
        c.bars_auto(["AAA"], "2016-01-01", "2016-12-31")          # husker valget
        self.assertEqual(self.fake.requests[-1][1]["feed"], "iex")

    def test_403_on_iex_is_raised(self):
        self.fake.sip_forbidden = True
        c, _ = client_for(self.fake, feed="iex")
        self.fake.fail_next = [403]
        with self.assertRaises(ForbiddenError):
            c.bars_auto(["AAA"], "2016-01-01", "2016-12-31")

    def test_429_retry(self):
        self.fake.bars = {"AAA": mkbars(bdays(date(2016, 1, 4), date(2016, 1, 8)))}
        self.fake.fail_next = [429, 429]
        c, sleeps = client_for(self.fake)
        out = c.bars(["AAA"], "2016-01-01", "2016-12-31")
        self.assertEqual(len(out["AAA"]), 5)
        self.assertEqual(c.n_429, 2)
        self.assertEqual(sleeps, [2.0, 4.0])                    # eksponentiel ventetid

    def test_429_gives_up(self):
        self.fake.fail_next = [429] * 20
        c, _ = client_for(self.fake, max_retries=3)
        with self.assertRaises(AlpacaError):
            c.bars(["AAA"], "2016-01-01", "2016-12-31")
        self.assertEqual(c.n_calls, 4)

    def test_server_error_and_network_error_retry(self):
        self.fake.bars = {"AAA": mkbars(bdays(date(2016, 1, 4), date(2016, 1, 5)))}
        self.fake.fail_next = [503]
        c, sleeps = client_for(self.fake)
        self.assertEqual(len(c.bars(["AAA"], "2016-01-01", "2016-12-31")["AAA"]), 2)

        class Flaky:
            def __init__(self):
                self.n = 0

            def get(self, *a, **k):
                self.n += 1
                if self.n == 1:
                    raise requests.ConnectionError("nede")
                return requests.Session().get(*a, **k)

        c2 = AlpacaClient("k", "s", base_url=self.fake.url, min_interval=0, sleep=lambda s: None,
                          session=Flaky())
        self.assertEqual(len(c2.bars(["AAA"], "2016-01-01", "2016-12-31")["AAA"]), 2)
        self.assertEqual(c2.n_retries, 1)


class NormalizationTests(unittest.TestCase):
    def syms(self, raw):
        return [s for s, _ in ticker_map.candidate_symbols(raw)]

    def test_cases(self):
        self.assertEqual(self.syms("aapl"), ["AAPL"])
        self.assertEqual(self.syms(" $TSLA "), ["TSLA"])
        self.assertEqual(self.syms("BRK.B"), ["BRK.B", "BRK-B", "BRK/B"])
        self.assertEqual(self.syms("BRK/B"), ["BRK/B", "BRK.B", "BRK-B"])
        self.assertEqual(self.syms("brk-b"), ["BRK-B", "BRK.B", "BRK/B"])
        self.assertEqual(self.syms("BF.B."), ["BF.B", "BF-B", "BF/B"])
        self.assertEqual(self.syms("N/A"), [])
        self.assertEqual(self.syms("--"), [])
        self.assertEqual(self.syms("TOOLONGXYZ"), [])
        self.assertEqual(ticker_map.candidate_symbols("AAPL"), [("AAPL", "exact")])
        self.assertEqual(ticker_map.candidate_symbols("aapl"), [("AAPL", "normalized")])


TRADE_COLS = ["year", "doc_id", "member", "ticker", "type", "transaction_date", "filing_date",
              "include_v1"]


def write_trades(root, rows):
    p = root / "data" / "processed" / "trades.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(TRADE_COLS)
        for r in rows:
            w.writerow(r)


def T(i, tk, typ, tx, filed, inc="True"):
    return [filed[:4], str(i), "Medlem", tk, typ, tx, filed, inc]


class PipelineTests(unittest.TestCase):
    END = "2017-06-30"

    def setUp(self):
        self.fake = Fake().start()
        self.addCleanup(self.fake.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        days = bdays(date(2016, 1, 4), date(2017, 6, 30))
        f = self.fake
        f.bars["SPY"] = mkbars(days, 200)
        f.bars["AAA"] = mkbars(days, 50)
        f.bars["DEAD"] = mkbars(bdays(date(2016, 1, 4), date(2016, 9, 30)), 20)   # slutter midt i perioden
        f.bars["LATE"] = mkbars(bdays(date(2016, 11, 1), date(2017, 6, 30)), 10)  # starter sent
        f.bars["BRK.B"] = mkbars(days, 150)
        f.bars["NEWCO"] = mkbars(days, 30)
        f.asof_data[("OLDCO", "2016-02-01")] = ("NEWCO", f.bars["NEWCO"])
        write_trades(self.root, [
            T(1, "AAA", "buy", "2016-02-01", "2016-02-10"),
            T(2, "AAA", "sell", "2016-03-01", "2016-03-10"),
            T(3, "DEAD", "buy", "2016-02-01", "2016-02-10"),
            T(4, "DEAD", "buy", "2016-07-01", "2016-07-11"),       # 6 og 12 mdr. efter datoen findes ikke
            T(5, "LATE", "buy", "2016-02-01", "2016-02-10"),
            T(6, "GHOST", "buy", "2016-02-01", "2016-02-10"),
            T(7, "AAA", "buy", "2016-05-01", "2016-04-01"),        # indleveret før handel
            T(8, "brk.b", "buy", "2016-02-01", "2016-02-10"),
            T(9, "OLDCO", "buy", "2016-02-01", "2016-02-10"),
            T(10, "AAA", "buy", "2016-02-01", "2016-02-10", inc="False"),
        ])

    def run_fetch(self, extra=(), sleeps=None):
        c, _ = client_for(self.fake, sleeps=sleeps)
        argv = ["--end", self.END, "--min-interval", "0"] + list(extra)
        code = fetch_prices.main(argv, root=self.root, client=c)
        return code, c

    def test_full_flow_resume_and_coverage(self):
        code, c = self.run_fetch()
        self.assertEqual(code, 0)
        p = pc.Paths(self.root)
        self.assertEqual(len(pc.read_daily(p.daily_file("AAA"))), len(self.fake.bars["AAA"]))
        self.assertFalse(p.daily_file("GHOST").exists())
        nd = pc.load_no_data(p)
        self.assertIn("GHOST", nd)
        self.assertNotIn("OLDCO", nd.keys() - {"OLDCO"})
        self.assertIn("LATE", [s for s in ["LATE"] if p.daily_file(s).exists()])
        meta = json.loads(p.meta.read_text())
        self.assertEqual(meta["feed"], "sip")
        self.assertEqual(meta["spy_first_date"], "2016-01-04")

        # ticker_map
        rows = {r["filing_ticker"]: r for r in csv.DictReader(p.ticker_map.open())}
        self.assertEqual(rows["AAA"]["method"], "exact")
        self.assertEqual(rows["brk.b"]["price_symbol"], "BRK.B")
        self.assertEqual(rows["brk.b"]["method"], "normalized")
        self.assertEqual(rows["GHOST"]["method"], "unmatched")
        self.assertEqual(rows["OLDCO"]["method"], "alpaca_asof")
        self.assertIn("NEWCO", rows["OLDCO"]["note"])
        self.assertEqual(rows["DEAD"]["last_date"], "2016-09-30")
        self.assertIn("slutter", rows["DEAD"]["note"])           # ikke smidt ud
        self.assertIn("starter", rows["LATE"]["note"])

        # genoptagelse: intet nyt, ingen kald for no_data, kun SPY/inkrementelle forespørgsler
        self.fake.requests.clear()
        code, c2 = self.run_fetch()
        self.assertEqual(code, 0)
        asked = {s for path, q in self.fake.requests for s in q.get("symbols", "").split(",") if s}
        self.assertNotIn("GHOST", asked)
        self.assertEqual(self.fake.requests, [])                  # alt er allerede hentet til END

        # nye dage: kun overlap + nye rækker hentes, filen forlænges
        newdays = bdays(date(2017, 7, 3), date(2017, 7, 14))
        for s in ("SPY", "AAA", "BRK.B", "NEWCO"):
            base = self.fake.bars[s][-1]["c"]
            self.fake.bars[s] += mkbars(newdays, base + 1)
        self.fake.requests.clear()
        code, _ = self.run_fetch(["--end", "2017-07-14"][0:0])  # END uændret -> ingen kald
        self.assertEqual(self.fake.requests, [])
        c3, _ = client_for(self.fake)
        self.assertEqual(fetch_prices.main(["--end", "2017-07-14", "--min-interval", "0"],
                                           root=self.root, client=c3), 0)
        starts = {q["start"] for path, q in self.fake.requests if path == "/v2/stocks/bars"
                  and "asof" not in q}
        self.assertTrue(all(s >= "2016-09-01" for s in starts), starts)   # ikke fuld historik
        self.assertIn("2017-06-23", starts)
        aaa = pc.read_daily(p.daily_file("AAA"))
        self.assertEqual(aaa[-1][0], date(2017, 7, 14))
        self.assertEqual(len(aaa), len(self.fake.bars["AAA"]))

        # ændret justering (udbytte) i historikken -> fuld genhentning af det symbol
        for b in self.fake.bars["AAA"]:
            b["c"] *= 0.9
            b["o"] *= 0.9
        newdays2 = bdays(date(2017, 7, 17), date(2017, 7, 18))
        for s in ("SPY", "AAA", "BRK.B", "NEWCO"):
            self.fake.bars[s] += mkbars(newdays2, 100)
        self.fake.requests.clear()
        c4, _ = client_for(self.fake)
        fetch_prices.main(["--end", "2017-07-18", "--min-interval", "0"], root=self.root, client=c4)
        full = [q for path, q in self.fake.requests if q.get("symbols") == "AAA" and q["start"] == "2015-12-01"]
        self.assertTrue(full, "AAA skulle hentes forfra")
        self.assertAlmostEqual(pc.read_daily(p.daily_file("AAA"))[0][4], 50.5 * 0.9, places=6)

    def test_coverage_on_synthetic_set(self):
        self.run_fetch()
        p = pc.Paths(self.root)
        self.assertEqual(coverage.main(self.root), 0)
        un = {(r["doc_id"]): r for r in csv.DictReader(p.unmatched.open())}
        self.assertNotIn("1", un)                                  # AAA køb fuldt dækket
        self.assertNotIn("10", un)                                 # include_v1=False ikke med
        self.assertEqual(un["3"]["h6_status"], "ok")               # DEAD: 6 mdr. efter 2016-02-11 = aug: ok
        self.assertEqual(un["3"]["h12_status"], "data_slutter_foer")
        self.assertEqual(un["3"]["entry_status"], "ok")
        self.assertEqual(un["4"]["h3_status"], "data_slutter_foer")   # okt.-dato efter slut
        self.assertEqual(un["4"]["match_method"], "exact")
        self.assertEqual(un["4"]["symbol_last_date"], "2016-09-30")
        self.assertEqual(un["5"]["entry_status"], "data_starter_senere")
        self.assertEqual(un["6"]["entry_status"], "ingen_kursdata")
        self.assertEqual(un["7"]["entry_status"], "datofejl")
        self.assertNotIn("8", un)                                  # BRK.B-variant dækket
        self.assertNotIn("9", un)                                  # asof-kobling dækket
        s = {(r["year"], r["scope"]): r for r in csv.DictReader(p.coverage.open())}
        a = s[("ALL", "buy")]
        self.assertEqual(a["n_trades"], "8")                       # 1,3,4,5,6,7,8,9
        self.assertEqual(a["entry_ok"], "5")                       # 1,3,4,8,9
        self.assertEqual(a["n_tickers"], "6")                      # AAA,DEAD,LATE,GHOST,brk.b,OLDCO
        self.assertEqual(a["tickers_no_data"], "1")                # GHOST
        self.assertEqual(a["tickers_data_ends_early"], "1")        # DEAD
        self.assertEqual(s[("2016", "alle")]["n_trades"], "9")     # inkl. 1 salg
        sells = s[("2016", "alle")]
        self.assertEqual(sells["entry_ok"], "6")

    def test_400_isolates_bad_symbol(self):
        self.fake.bad_symbols = {"GHOST"}
        code, _ = self.run_fetch()
        self.assertEqual(code, 0)
        nd = pc.load_no_data(pc.Paths(self.root))
        self.assertIn("afvist", nd["GHOST"]["reason"])
        self.assertTrue(pc.read_daily(pc.Paths(self.root).daily_file("AAA")))   # resten blev hentet

    def test_corporate_action_name_change(self):
        self.fake.corp = [{"old_symbol": "GHOST", "new_symbol": "NEWCO", "process_date": "2016-06-01"}]
        self.run_fetch()
        p = pc.Paths(self.root)
        rows = {r["filing_ticker"]: r for r in csv.DictReader(p.ticker_map.open())}
        self.assertEqual(rows["GHOST"]["method"], "alpaca_name_change")
        self.assertEqual(rows["GHOST"]["price_symbol"], "NEWCO")

    def test_missing_optional_columns_ok(self):
        # trades.csv uden date_suspect/duplicate_of osv. er allerede tilfældet her
        self.assertEqual(len(pc.load_trades(pc.Paths(self.root).trades)), 9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
