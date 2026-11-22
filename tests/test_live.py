"""Tests of the live paper fund (live/).  No network: House Clerk and Alpaca are faked.

Run:  python3 -m unittest tests.test_live
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from live import execution, house, member_filter, run_daily, signals, site, store
from live import strategy as strat
from live.broker import Broker, DuplicateClientOrderId, OrderRejected
from live.safety import NotPaperError, redact, validate_paper_url
from prices.alpaca_client import AlpacaError

PROJECT = Path(__file__).resolve().parent.parent
KEY, SECRET = "TESTKEY12345", "TESTSECRET67890"
PAPER = "https://paper-api.alpaca.markets"
ENV = {"ALPACA_KEY_ID": KEY, "ALPACA_SECRET_KEY": SECRET, "ALPACA_BASE_URL": PAPER}
NOW = datetime(2026, 11, 18, 12, 45, tzinfo=timezone.utc)          # Wednesday, before the US open
CLOCK_PRE_OPEN = {"timestamp": "2026-11-18T07:45:00-05:00", "is_open": False,
                  "next_open": "2026-11-18T09:30:00-05:00", "next_close": "2026-11-18T16:00:00-05:00"}


def bar(day: str, close: float, vol: int = 1000) -> dict:
    return {"t": f"{day}T05:00:00Z", "o": close, "h": close, "l": close, "c": close, "v": vol}


def bars_series(ref_day: str, ref: float, last_day: str, last: float) -> list[dict]:
    return [bar("2026-11-10", ref), bar(ref_day, ref), bar("2026-11-16", last), bar(last_day, last)]


class FakeBroker:
    """Stands in for live.broker.Broker (same method names)."""

    def __init__(self, equity=100000.0, cash=100000.0):
        self.equity, self.cash = equity, cash
        self.pos: list[dict] = []
        self.orders_by_cid: dict[str, dict] = {}
        self.submitted: list[dict] = []
        self.clock_ = dict(CLOCK_PRE_OPEN)
        self.bar_data: dict[str, list] = {}
        self.assets: dict[str, dict] = {}
        self.reject_notional_opg = False
        self.snap = {}
        self.blocked = False

    def account(self):
        return {"equity": str(self.equity), "cash": str(self.cash), "trading_blocked": self.blocked,
                "account_blocked": False}

    def clock(self):
        return self.clock_

    def calendar(self, start, end):
        return []

    def positions(self):
        return self.pos

    def orders(self, after=None, status="all", limit=500):
        o = list(self.orders_by_cid.values())
        return [x for x in o if x["status"] in ("new", "accepted")] if status == "open" else o

    def order_by_client_id(self, cid):
        return self.orders_by_cid.get(cid)

    def asset(self, symbol):
        if symbol in self.assets:
            return self.assets[symbol]
        if symbol in ("ZZZZ",):
            return None
        return {"symbol": symbol, "status": "active", "tradable": True, "fractionable": True, "exchange": "NASDAQ"}

    def submit_order(self, symbol, side, tif, client_order_id, qty=None, notional=None):
        if client_order_id in self.orders_by_cid:
            raise DuplicateClientOrderId("client_order_id must be unique")
        if notional is not None and tif == "opg" and self.reject_notional_opg:
            raise OrderRejected("fractional orders must be DAY orders")
        o = {"id": f"id-{len(self.submitted)+1}", "client_order_id": client_order_id, "symbol": symbol, "side": side,
             "time_in_force": tif, "qty": qty, "notional": notional, "status": "accepted", "filled_qty": "0"}
        self.orders_by_cid[client_order_id] = o
        self.submitted.append(o)
        return o

    def fill(self, cid, price, qty, when="2026-11-18T14:30:00Z"):
        o = self.orders_by_cid[cid]
        o.update(status="filled", filled_qty=str(qty), filled_avg_price=str(price), filled_at=when)

    def bars(self, symbols, start, end):
        return {s: self.bar_data[s] for s in symbols if s in self.bar_data}

    def snapshots(self, symbols):
        return {s: self.snap[s] for s in symbols if s in self.snap}


def trade_row(doc_id, ticker, ttype="buy", txn="2026-11-12", low="15001", high="50000", filing="2026-11-17",
              member="Jane Doe", owner="self", **kw) -> dict:
    row = {"year": filing[:4], "doc_id": doc_id, "member": member, "state_dst": "CA11", "owner": owner,
           "asset": f"{ticker} Inc ({ticker})", "ticker": ticker, "asset_type": "ST", "asset_type_source": "code",
           "type": ttype, "transaction_date": txn, "notification_date": txn, "filing_date": filing,
           "amount_low": low, "amount_high": high, "amount_flag": "", "description": "", "sub_holding": "",
           "location": "", "comments": "", "filing_status": "New", "option_like": False, "short_related": False,
           "date_suspect": "", "duplicate_of": "", "deleted_by": "", "include_v1": True, "include_reason": "kode ST",
           "template": "new_2022", "parse_source": "plain"}
    row.update(kw)
    return row


class FakeResp:
    def __init__(self, status, content=b""):
        self.status_code, self.content = status, content


class FakeHouseSession:
    def __init__(self, filings: list[dict], index_status=200):
        """filings: dicts with doc_id, filing_date (YYYY-MM-DD), first, last."""
        self.filings, self.index_status = filings, index_status
        self.urls: list[str] = []
        self.headers = {"User-Agent": house.USER_AGENT}

    def _zip(self, year):
        members = "".join(
            f"<Member><Prefix>Hon.</Prefix><Last>{f.get('last','Doe')}</Last><First>{f.get('first','Jane')}</First>"
            f"<Suffix></Suffix><FilingType>{f.get('type','P')}</FilingType><StateDst>CA11</StateDst>"
            f"<Year>{year}</Year><FilingDate>{int(f['filing_date'][5:7])}/{int(f['filing_date'][8:10])}/{f['filing_date'][:4]}"
            f"</FilingDate><DocID>{f['doc_id']}</DocID></Member>" for f in self.filings)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"{year}FD.xml", f"<FinancialDisclosure>{members}</FinancialDisclosure>")
        return buf.getvalue()

    def get(self, url, timeout=None):
        self.urls.append(url)
        if url.endswith("FD.zip"):
            if self.index_status != 200:
                return FakeResp(self.index_status)
            return FakeResp(200, self._zip(url.rsplit("/", 1)[1][:4]))
        return FakeResp(200, b"%PDF-1.4 fake")


class Env:
    """Temp project root + fakes + a helper to run the daily runner."""

    def __init__(self, filings, trades_by_doc, broker=None, index_status=200):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "live").mkdir()
        shutil.copy(PROJECT / "live" / "strategy.json", self.root / "live" / "strategy.json")
        self.broker = broker or FakeBroker()
        self.filings, self.trades_by_doc = filings, trades_by_doc
        self.session = FakeHouseSession(filings, index_status)
        self.broker.bar_data.setdefault("AAA", bars_series("2026-11-12", 100.0, "2026-11-17", 105.0))   # +5 %
        self.broker.bar_data.setdefault("BBB", bars_series("2026-11-12", 100.0, "2026-11-17", 115.0))   # +15 %
        self.broker.bar_data.setdefault("CCC", bars_series("2026-11-12", 100.0, "2026-11-17", 90.0))    # -10 %
        self.broker.bar_data.setdefault("SPY", [bar("2026-11-16", 600.0), bar("2026-11-17", 602.0)])

    def parse(self, root, row, pdf):
        return {"status": "parsed", "trades": copy.deepcopy(self.trades_by_doc[row["doc_id"]]), "warnings": []}

    def run(self, argv=(), now=NOW, env=ENV, broker=None, session=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(house, "parse_filing", self.parse), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_daily.run_main(list(argv), dict(env), root=self.root, broker=broker or self.broker,
                                      house_session=session or self.session, now=now, sleep=lambda s: None)
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def p(self, name):
        return self.root / "data" / "live" / name

    def state(self):
        return json.loads(self.p("state.json").read_text())

    def live_json(self, name="live.json"):
        return json.loads((self.root / "docs" / "data" / name).read_text())

    def log_rows(self):
        return store.read_csv(self.p("trades_log.csv"))

    def state_dry(self):
        return json.loads((self.root / "data" / "live" / "dryrun" / "state.json").read_text())

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def standard_env(**kw) -> Env:
    filings = [{"doc_id": "20040001", "filing_date": "2026-11-17"}]
    trades = {"20040001": [trade_row("20040001", "AAA"), trade_row("20040001", "BBB"), trade_row("20040001", "CCC")]}
    return Env(filings, trades, **kw)


ARGS = ["--start-date", "2026-11-10"]


# ----------------------------------------------------------------------------- safety

class TestPaperGuard(unittest.TestCase):
    def test_accepts_exact_paper_host(self):
        self.assertEqual(validate_paper_url(PAPER), PAPER)
        self.assertEqual(validate_paper_url(PAPER + "/v2/"), PAPER)

    def test_rejects_live_and_lookalike_hosts(self):
        for bad in ("https://api.alpaca.markets", "http://paper-api.alpaca.markets",
                    "https://paper-api.alpaca.markets.evil.com", "https://paper-api.alpaca.markets@evil.com",
                    "https://evil.com/paper-api.alpaca.markets", "https://paper-api.alpaca.markets:8443",
                    "https://user:pw@paper-api.alpaca.markets", "https://paper-api.alpaca.markets./", "", None,
                    "https://paper-api.alpaca.markets/v2/orders"):
            with self.subTest(url=bad), self.assertRaises(NotPaperError):
                validate_paper_url(bad)

    def test_broker_constructor_refuses_live_url(self):
        with self.assertRaises(NotPaperError):
            Broker(KEY, SECRET, "https://api.alpaca.markets")

    def test_runner_refuses_non_paper_url_before_any_call(self):
        e = standard_env()
        boom = mock.Mock(side_effect=AssertionError("broker must not be touched"))
        e.broker.account = boom
        code = e.run(ARGS, env={**ENV, "ALPACA_BASE_URL": "https://api.alpaca.markets"})
        self.assertEqual(code, 2)
        self.assertIn("AFVIST", e.err)
        boom.assert_not_called()
        self.assertEqual(e.broker.submitted, [])
        self.assertFalse(e.p("state.json").exists())
        e.cleanup()

    def test_redact_removes_keys(self):
        self.assertNotIn(KEY, redact(f"boom {KEY} and {SECRET}", [KEY, SECRET]))
        self.assertNotIn("abcdef123", redact("{'APCA-API-KEY-ID': 'abcdef123'}"))

    def test_keys_never_printed(self):
        e = standard_env()
        e.run(ARGS)
        self.assertNotIn(KEY, e.out + e.err)
        self.assertNotIn(SECRET, e.out + e.err)
        self.assertNotIn(KEY, repr(Broker(KEY, SECRET, PAPER)))
        blob = "".join(p.read_text() for p in e.root.rglob("*") if p.is_file() and p.suffix in (".json", ".csv"))
        self.assertNotIn(SECRET, blob)
        e.cleanup()


# ----------------------------------------------------------------------------- sizing, skip rule, exits

SIZING = strat.load()["sizing"]


class TestSizing(unittest.TestCase):
    def test_default_amount_is_one_percent_at_median(self):
        self.assertAlmostEqual(signals.target_notional(100000, "amount", 8000.5, SIZING), 1000.0)

    def test_clip_bounds(self):
        self.assertAlmostEqual(signals.target_notional(100000, "amount", 1.0, SIZING), 250.0)          # 0.25 x
        self.assertAlmostEqual(signals.target_notional(100000, "amount", 5_000_000, SIZING), 10000.0)    # 10 x
        self.assertAlmostEqual(signals.target_notional(100000, "amount", 16000.0, SIZING), 1999.875, places=3)

    def test_equal_weighting_is_flat(self):
        self.assertEqual(signals.target_notional(50000, "equal", 5_000_000, SIZING), 500.0)

    def test_midpoint_reuses_backtest(self):
        self.assertEqual(signals.midpoint({"amount_low": 1001, "amount_high": 15000, "amount_flag": ""}), 8000.5)
        self.assertEqual(signals.midpoint({"amount_low": 1000000, "amount_high": None, "amount_flag": "open_over"}), 1000000.0)

    def test_position_cap(self):
        n, capped = signals.apply_position_cap(10000, 100000, 0.0, SIZING)
        self.assertEqual((n, capped), (10000, False))
        n, capped = signals.apply_position_cap(10000, 100000, 4000.0, SIZING)    # cap 10 000 - 4 000 existing
        self.assertEqual((n, capped), (6000.0, True))
        n, capped = signals.apply_position_cap(500, 100000, 12000.0, SIZING)      # already above the cap
        self.assertEqual((n, capped), (0.0, True))

    def test_cash_limit_never_negative(self):
        n, scaled = signals.apply_cash_limit(1000, 500, 2.0)
        self.assertTrue(scaled)
        self.assertAlmostEqual(n, 500 / 1.02)
        self.assertEqual(signals.apply_cash_limit(1000, -50, 2.0), (0.0, True))
        self.assertEqual(signals.apply_cash_limit(100, 5000, 2.0), (100, False))

    def test_whole_shares(self):
        self.assertEqual(signals.whole_shares(1000, 105), 9)
        self.assertEqual(signals.whole_shares(100, 105), 0)

    def test_end_to_end_sizing_and_hard_cap_in_orders(self):
        e = standard_env()
        e.trades_by_doc["20040001"] = [trade_row("20040001", "AAA", low="5000001", high="25000000")]   # 10 x clip
        e.run(ARGS)
        o = e.broker.submitted[0]
        self.assertEqual(o["notional"], "10000.00")            # 100 000 x 1 % x 10 = 10 % cap, not more
        e.cleanup()

    def test_cash_scale_down_logged(self):
        e = standard_env(broker=FakeBroker(equity=100000.0, cash=500.0))
        e.run(ARGS)
        self.assertTrue(e.broker.submitted)
        n = float(e.broker.submitted[0]["notional"])
        self.assertLessEqual(n * 1.02, 500.0 + 1e-6)
        self.assertTrue(any("nedskaleret" in r["reason"] for r in e.log_rows() if r["kind"] == "signal"))
        e.cleanup()

    def test_no_cash_means_skip_not_negative(self):
        e = standard_env(broker=FakeBroker(equity=100000.0, cash=0.0))
        e.run(ARGS)
        self.assertEqual(e.broker.submitted, [])
        self.assertTrue(any("kontanter" in r["reason"] for r in e.log_rows()))
        e.cleanup()


class TestSkipRule(unittest.TestCase):
    def test_skips_only_on_rise_above_x(self):
        self.assertTrue(signals.should_skip(0.1001, 10.0))
        self.assertFalse(signals.should_skip(0.10, 10.0))          # boundary is taken
        self.assertFalse(signals.should_skip(0.05, 10.0))
        self.assertFalse(signals.should_skip(-0.40, 10.0))         # a fall NEVER skips
        self.assertFalse(signals.should_skip(5.0, None))           # X = none never skips

    def test_reference_is_close_on_or_before_txn_date(self):
        bars = [bar("2026-11-09", 90), bar("2026-11-12", 100), bar("2026-11-13", 999)]
        self.assertEqual(signals.reference_close(bars, "2026-11-12")[:2], (100.0, "2026-11-12"))
        self.assertEqual(signals.reference_close(bars, "2026-11-11")[:2], (90.0, "2026-11-09"))   # never a later close
        self.assertEqual(signals.reference_close(bars, "2026-11-25")[2], "ref_missing_stale")
        self.assertEqual(signals.reference_close([bar("2026-11-13", 5)], "2026-11-12")[2],
                         "ref_missing_no_bar_on_or_before_txn_date")
        self.assertIsNone(signals.reference_close([bar("2026-11-12", 100, vol=0)], "2026-11-12")[0])  # invalid bar

    def test_end_to_end_rise_skipped_fall_taken(self):
        e = standard_env()
        e.run(ARGS)
        dec = {r["ticker"]: r for r in e.log_rows() if r["kind"] == "signal"}
        self.assertEqual(dec["AAA"]["decision"], "taken")
        self.assertEqual(dec["BBB"]["decision"], "skipped")
        self.assertIn("steget", dec["BBB"]["reason"])
        self.assertEqual(dec["CCC"]["decision"], "taken")                 # -10 % is NOT skipped
        self.assertEqual(sorted(o["symbol"] for o in e.broker.submitted), ["AAA", "CCC"])
        self.assertEqual(float(dec["BBB"]["rise_pct"]), 15.0)
        e.cleanup()

    def test_snapshot_used_only_when_daily_close_is_stale(self):
        e = standard_env()
        e.broker.bar_data["AAA"] = [bar("2026-11-12", 100.0), bar("2026-11-09", 100.0)]   # last daily bar 6 days old
        e.broker.snap = {"AAA": {"latestTrade": {"p": 130.0}}}
        e.run(ARGS)
        dec = {r["ticker"]: r for r in e.log_rows() if r["kind"] == "signal"}
        self.assertEqual(dec["AAA"]["decision"], "skipped")               # 130 vs 100 = +30 %
        e.cleanup()


class TestExitScheduling(unittest.TestCase):
    def test_planned_exit_is_entry_plus_calendar_months(self):
        self.assertEqual(execution.planned_exit_date("2026-11-18", "hold_6m"), "2027-05-18")
        self.assertEqual(execution.planned_exit_date("2026-08-31", "hold_6m"), "2027-02-28")
        self.assertEqual(execution.planned_exit_date("2026-11-18", "hold_3m"), "2027-02-18")
        self.assertEqual(execution.planned_exit_date("2026-11-18", "hold_12m"), "2027-11-18")
        self.assertIsNone(execution.planned_exit_date("2026-11-18", "member_sells"))

    def test_exit_due_on_or_after_planned_date(self):
        lot = {"status": "open", "exit_rule": "hold_6m", "planned_exit": "2027-05-18"}
        self.assertIsNone(execution.exit_due(lot, "2027-05-17"))
        self.assertTrue(execution.exit_due(lot, "2027-05-18"))
        self.assertTrue(execution.exit_due(lot, "2027-05-20"))            # overdue still due
        self.assertIsNone(execution.exit_due({**lot, "status": "closed"}, "2027-06-01"))
        ms = {"status": "open", "exit_rule": "member_sells", "planned_exit": None}
        self.assertIsNone(execution.exit_due(ms, "2027-06-01"))
        self.assertTrue(execution.exit_due({**ms, "member_sale": {"doc_id": "1"}}, "2027-06-01"))

    def test_windows(self):
        w = execution.trading_windows(CLOCK_PRE_OPEN, NOW)
        self.assertEqual((w.opg_session, w.cls_session, w.today), ("2026-11-18", "2026-11-18", "2026-11-18"))
        late = datetime(2026, 11, 18, 14, 28, tzinfo=timezone.utc)         # 09:28 ET, 2 min before the open
        self.assertIsNone(execution.trading_windows(CLOCK_PRE_OPEN, late).opg_session)
        holiday = {"timestamp": "2026-11-26T07:45:00-05:00", "is_open": False,
                   "next_open": "2026-11-27T09:30:00-05:00", "next_close": "2026-11-27T13:00:00-05:00"}
        w = execution.trading_windows(holiday, datetime(2026, 11, 26, 12, 45, tzinfo=timezone.utc))
        self.assertEqual(w.opg_session, "2026-11-27")                      # next session is targeted
        open_clock = {"timestamp": "2026-11-18T11:00:00-05:00", "is_open": True,
                      "next_open": "2026-11-19T09:30:00-05:00", "next_close": "2026-11-18T16:00:00-05:00"}
        w = execution.trading_windows(open_clock, datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc))
        self.assertIsNone(w.opg_session)
        self.assertEqual(w.cls_session, "2026-11-18")
        w = execution.trading_windows(open_clock, datetime(2026, 11, 18, 20, 55, tzinfo=timezone.utc))   # 15:55 ET
        self.assertIsNone(w.cls_session)

    def test_opg_window_starts_at_19_et(self):
        after_close = {"timestamp": "2026-11-20T16:25:00-05:00", "is_open": False,
                       "next_open": "2026-11-23T09:30:00-05:00", "next_close": "2026-11-23T16:00:00-05:00"}
        w = execution.trading_windows(after_close, datetime(2026, 11, 20, 21, 25, tzinfo=timezone.utc))   # 16:25 ET
        self.assertIsNone(w.opg_session)
        self.assertIn("19:00", w.opg_reason)
        self.assertIsNone(w.cls_session)                                 # cls also closed 15:50-19:00 ET
        evening = execution.trading_windows(after_close, datetime(2026, 11, 21, 3, 17, tzinfo=timezone.utc))  # 22:17 ET
        self.assertEqual((evening.opg_session, evening.cls_session), ("2026-11-23", "2026-11-23"))
        sunday_noon = execution.trading_windows(after_close, datetime(2026, 11, 22, 17, 0, tzinfo=timezone.utc))
        self.assertIsNone(sunday_noon.opg_session)
        summer = {"timestamp": "2027-06-01T08:00:00-04:00", "is_open": False,
                  "next_open": "2027-06-01T09:30:00-04:00", "next_close": "2027-06-01T16:00:00-04:00"}
        self.assertEqual(execution.trading_windows(summer, datetime(2027, 6, 1, 12, 0, tzinfo=timezone.utc)).opg_session,
                         "2027-06-01")                                   # 08:00 EDT

    def _executor(self, lot, broker=None, dry=False):
        b = broker or FakeBroker()
        state = store.new_state("2026-11-10", 100000.0, "x", "h", "n")
        state["lots"][lot["lot_id"]] = lot
        logs = []
        ex = execution.Executor(b, state, strat.load(), NOW, dry, lambda **k: logs.append(k), lambda: None, {},
                                {lot["symbol"]: {"qty": str(lot["qty"]), "qty_available": str(lot["qty"])}})
        return b, state, ex, logs

    def _lot(self, **kw):
        lot = {"lot_id": "cf-1-abc", "doc_id": "1", "member": "Jane Doe", "member_canon": "Jane Doe", "owner": "self",
               "raw_ticker": "AAA", "symbol": "AAA", "filing_date": "2026-11-01", "txn_date": "2026-10-30",
               "amount_mid": 8000.5, "exit_rule": "hold_6m", "status": "open", "qty": 12.0, "entry_qty": 12.0,
               "entry_price": 100.0, "entry_date": "2026-11-02", "planned_exit": "2027-05-02", "exit_attempts": 0,
               "member_sale": None}
        lot.update(kw)
        return lot

    def test_due_lot_sold_with_cls_and_only_once(self):
        b, state, ex, _ = self._executor(self._lot(planned_exit="2026-11-18"))
        w = execution.trading_windows(CLOCK_PRE_OPEN, NOW)
        ex.run_exits(w)
        self.assertEqual(len(b.submitted), 1)
        o = b.submitted[0]
        self.assertEqual((o["side"], o["time_in_force"], o["qty"], o["symbol"]), ("sell", "cls", "12", "AAA"))
        ex.run_exits(w)                                                    # rerun: pending sell blocks a second one
        self.assertEqual(len(b.submitted), 1)

    def test_not_due_lot_untouched(self):
        b, _, ex, _ = self._executor(self._lot(planned_exit="2026-11-19"))
        ex.run_exits(execution.trading_windows(CLOCK_PRE_OPEN, NOW))
        self.assertEqual(b.submitted, [])

    def test_exit_deferred_when_window_missed(self):
        b, _, ex, _ = self._executor(self._lot(planned_exit="2026-11-18"))
        open_clock = {"timestamp": "2026-11-18T15:55:00-05:00", "is_open": True,
                      "next_open": "2026-11-19T09:30:00-05:00", "next_close": "2026-11-18T16:00:00-05:00"}
        notes = ex.run_exits(execution.trading_windows(open_clock, datetime(2026, 11, 18, 20, 55, tzinfo=timezone.utc)))
        self.assertEqual(b.submitted, [])
        self.assertTrue(any("udskudt" in n for n in notes))

    def test_member_sells_exit_uses_opg(self):
        lot = self._lot(exit_rule="member_sells", planned_exit=None)
        b, state, ex, _ = self._executor(lot)
        ex.run_exits(execution.trading_windows(CLOCK_PRE_OPEN, NOW))
        self.assertEqual(b.submitted, [])                                  # no sale yet
        self.assertEqual(ex.record_member_sale("Jane Doe", "AAA", "2026-11-05", {"doc_id": "9"}), 1)
        ex.run_exits(execution.trading_windows(CLOCK_PRE_OPEN, NOW))
        self.assertEqual((b.submitted[0]["side"], b.submitted[0]["time_in_force"]), ("sell", "opg"))

    def test_member_sale_before_buy_txn_does_not_match(self):
        _, _, ex, _ = self._executor(self._lot(exit_rule="member_sells", planned_exit=None))
        self.assertEqual(ex.record_member_sale("Jane Doe", "AAA", "2026-10-01", {"doc_id": "9"}), 0)
        self.assertEqual(ex.record_member_sale("Other", "AAA", "2026-11-05", {"doc_id": "9"}), 0)

    def test_never_sells_more_than_position(self):
        b, state, ex, _ = self._executor(self._lot(planned_exit="2026-11-18", qty=12.0))
        ex.positions["AAA"]["qty_available"] = "5"
        ex.run_exits(execution.trading_windows(CLOCK_PRE_OPEN, NOW))
        self.assertEqual(b.submitted[0]["qty"], "5")

    def test_dry_run_exit_sends_nothing(self):
        b, _, ex, logs = self._executor(self._lot(planned_exit="2026-11-18"), dry=True)
        ex.run_exits(execution.trading_windows(CLOCK_PRE_OPEN, NOW))
        self.assertEqual(b.submitted, [])
        self.assertTrue(any(l.get("order_status") == "dry_run" for l in logs))

    def test_fractional_cls_rejected_falls_back_to_day(self):
        class B(FakeBroker):
            def submit_order(self, symbol, side, tif, client_order_id, qty=None, notional=None):
                if tif == "cls":
                    raise OrderRejected("fractional orders must be DAY orders")
                return super().submit_order(symbol, side, tif, client_order_id, qty, notional)
        b, _, ex, _ = self._executor(self._lot(planned_exit="2026-11-18", qty=2.5), broker=B())
        ex.run_exits(execution.trading_windows(CLOCK_PRE_OPEN, NOW))
        self.assertEqual((b.submitted[0]["time_in_force"], b.submitted[0]["qty"]), ("day", "2.5"))


# ----------------------------------------------------------------------------- member filter hook

class TestMemberFilter(unittest.TestCase):
    CFG = {"enabled": True, "rule": "score_gt_0", "trailing_window_days": 365, "min_completed_trades": 3,
           "shrinkage_k": 2, "top_percentile": 50, "on_insufficient_history": "skip"}
    AS_OF = date(2026, 11, 18)

    @staticmethod
    def hist(data):
        return lambda as_of: {m: [{"exit_date": d, "excess": x} for d, x in ts] for m, ts in data.items()}

    def test_disabled_allows_everyone(self):
        mf = member_filter.MemberFilter({**self.CFG, "enabled": False}, self.hist({}))
        self.assertTrue(mf.allows("Anyone", self.AS_OF)[0])

    def test_shrunk_score(self):
        self.assertAlmostEqual(member_filter.shrunk_score([0.1, 0.1, 0.1], 2), 0.3 / 5)

    def test_score_gt_0_and_window(self):
        data = {"Good": [("2026-05-01", 0.05), ("2026-06-01", 0.02), ("2026-07-01", 0.04)],
                "Bad": [("2026-05-01", -0.05), ("2026-06-01", 0.01), ("2026-07-01", -0.04)],
                "OldOnly": [("2024-01-01", 0.5)] * 5,                      # outside the trailing window
                "Few": [("2026-05-01", 0.9)]}
        mf = member_filter.MemberFilter(self.CFG, self.hist(data))
        self.assertTrue(mf.allows("Good", self.AS_OF)[0])
        self.assertFalse(mf.allows("Bad", self.AS_OF)[0])
        self.assertFalse(mf.allows("OldOnly", self.AS_OF)[0])              # insufficient history -> skip
        ok, why = mf.allows("Few", self.AS_OF)
        self.assertFalse(ok)
        self.assertIn("for få", why)
        self.assertFalse(mf.allows("Unknown", self.AS_OF)[0])

    def test_allow_on_insufficient_history(self):
        mf = member_filter.MemberFilter({**self.CFG, "on_insufficient_history": "allow"}, self.hist({}))
        self.assertTrue(mf.allows("Unknown", self.AS_OF)[0])

    def test_top_percentile(self):
        mk = lambda x: [("2026-05-01", x), ("2026-06-01", x), ("2026-07-01", x)]
        data = {"A": mk(0.10), "B": mk(0.05), "C": mk(0.01), "D": mk(-0.02)}
        mf = member_filter.MemberFilter({**self.CFG, "rule": "top_percentile", "top_percentile": 50}, self.hist(data))
        self.assertEqual([m for m in data if mf.allows(m, self.AS_OF)[0]], ["A", "B"])

    def test_lots_history_uses_backtest_cost_model(self):
        lots = {"1": {"status": "closed", "member_canon": "M", "entry_price": 100, "exit_price": 110, "spy_entry": 500,
                      "spy_exit": 505, "exit_date": "2026-06-01"},
                "2": {"status": "closed", "member_canon": "M", "entry_price": 100, "exit_price": 110, "spy_entry": None,
                      "spy_exit": 505, "exit_date": "2026-06-01"},                        # no SPY price: left out
                "3": {"status": "open", "member_canon": "M"}}
        h = member_filter.lots_history(lots)(self.AS_OF)
        self.assertEqual(len(h["M"]), 1)
        self.assertAlmostEqual(h["M"][0]["excess"], (0.999 ** 2) * 1.1 - 1 - 0.01)

    def test_hook_wired_in_runner_and_blocks_signal(self):
        e = standard_env()
        cfg = json.loads((e.root / "live" / "strategy.json").read_text())
        cfg["member_filter"].update(enabled=True)                                     # no history -> skip everything
        (e.root / "live" / "strategy.json").write_text(json.dumps(cfg))
        e.run(ARGS)
        self.assertEqual(e.broker.submitted, [])
        self.assertTrue(any("medlemsfilter" in r["reason"] for r in e.log_rows() if r["kind"] == "signal"))
        e.cleanup()

    def test_default_strategy_has_filter_disabled(self):
        self.assertFalse(strat.load()["member_filter"]["enabled"])


class TestStrategy(unittest.TestCase):
    def test_default_is_v1_preregistered(self):
        c = strat.load()
        self.assertEqual((c["exit"]["rule"], c["weighting"], c["skip_rise_pct"]), ("hold_6m", "amount", 10.0))
        self.assertEqual(strat.hold_months(c), 6)
        self.assertIn("10 %", strat.describe_da(c))

    def test_invalid_configs_rejected(self):
        base = strat.load()
        for mutate in (lambda c: c["exit"].update(rule="hold_9m"), lambda c: c.update(weighting="kelly"),
                       lambda c: c.update(skip_rise_pct=-1), lambda c: c["orders"].update(mode="margin"),
                       lambda c: c["sizing"].update(clip_min=11), lambda c: c["member_filter"].update(enabled="yes")):
            c = copy.deepcopy(base)
            mutate(c)
            with self.assertRaises(strat.StrategyError):
                strat.validate(c)

    def test_strategy_swappable_without_code_change(self):
        e = standard_env()
        cfg = json.loads((e.root / "live" / "strategy.json").read_text())
        cfg.update(weighting="equal", skip_rise_pct=None)                              # never skip, flat 1 %
        cfg["exit"]["rule"] = "hold_3m"
        (e.root / "live" / "strategy.json").write_text(json.dumps(cfg))
        e.run(ARGS)
        self.assertEqual(sorted(o["symbol"] for o in e.broker.submitted), ["AAA", "BBB", "CCC"])
        self.assertTrue(all(o["notional"] == "1000.00" for o in e.broker.submitted))
        e.cleanup()

    def test_hash_changes_with_rule_not_with_note(self):
        c = strat.load()
        h = strat.strategy_hash(c)
        self.assertEqual(h, strat.strategy_hash({**c, "locked_note": "other"}))
        c2 = copy.deepcopy(c)
        c2["skip_rise_pct"] = 12.0
        self.assertNotEqual(h, strat.strategy_hash(c2))


# ----------------------------------------------------------------------------- the daily run

class TestDryRun(unittest.TestCase):
    def test_dry_run_sends_no_orders_and_leaves_real_files_alone(self):
        e = standard_env()
        code = e.run(["--dry-run"] + ARGS)
        self.assertEqual(code, 0)
        self.assertEqual(e.broker.submitted, [])
        self.assertFalse(e.p("state.json").exists())                       # real state not created
        self.assertFalse((e.root / "docs" / "data" / "live.json").exists())
        self.assertTrue((e.root / "data" / "live" / "dryrun" / "state.json").exists())
        j = e.live_json("live_dryrun.json")
        self.assertEqual(j["last_run"]["status"], "dry_run")
        self.assertTrue(j["last_run"]["dry_run"])
        sig = [r for r in j["decisions"] if r["decision"] == "taken"]
        self.assertEqual(len(sig), 2)                                      # AAA and CCC would be bought
        self.assertEqual(e.state_dry()["pending_orders"], {})
        e.cleanup()

    def test_dry_run_does_not_consume_filings_for_the_real_run(self):
        e = standard_env()
        e.run(["--dry-run"] + ARGS)
        e.run(ARGS)
        self.assertEqual(len(e.broker.submitted), 2)
        e.cleanup()

    def test_dry_run_nonempty_account_only_warns(self):
        b = FakeBroker()
        b.pos = [{"symbol": "XYZ", "qty": "1", "qty_available": "1", "market_value": "10", "current_price": "10"}]
        e = standard_env(broker=b)
        self.assertEqual(e.run(["--dry-run"] + ARGS), 0)
        self.assertIn("ADVARSEL", e.out)
        e.cleanup()


class TestRealRun(unittest.TestCase):
    def test_orders_are_market_on_open_with_client_ids(self):
        e = standard_env()
        self.assertEqual(e.run(ARGS), 0)
        self.assertEqual(len(e.broker.submitted), 2)
        for o in e.broker.submitted:
            self.assertEqual((o["side"], o["time_in_force"]), ("buy", "opg"))
            self.assertTrue(o["client_order_id"].startswith("cf-20040001-"))
            self.assertIsNotNone(o["notional"])                            # fractional notional accepted in fake
        e.cleanup()

    def test_first_run_locks_start_date_and_equity(self):
        e = standard_env()
        e.run(ARGS)
        s = e.state()
        self.assertEqual((s["start_date"], s["start_equity"]), ("2026-11-10", 100000.0))
        e.run(["--start-date", "2020-01-01"])
        self.assertEqual(e.state()["start_date"], "2026-11-10")            # locked
        e.cleanup()

    def test_idempotent_rerun_no_double_buy(self):
        e = standard_env()
        e.run(ARGS)
        n = len(e.broker.submitted)
        e.run(ARGS)
        self.assertEqual(len(e.broker.submitted), n)
        e.cleanup()

    def test_idempotent_after_crash_before_state_saved(self):
        """Orders exist at Alpaca but the state forgot the filing: client_order_id lookup prevents a double buy."""
        e = standard_env()
        e.run(ARGS)
        n = len(e.broker.submitted)
        s = e.state()
        s["seen_doc_ids"], s["pending_orders"] = {}, {}
        e.p("state.json").write_text(json.dumps(s))
        e.run(ARGS)
        self.assertEqual(len(e.broker.submitted), n)
        self.assertEqual(len(e.state()["pending_orders"]), n)             # adopted, not re-sent
        e.cleanup()

    def test_idempotent_when_prefetch_of_orders_misses_them(self):
        e = standard_env()
        e.run(ARGS)
        first = list(e.broker.submitted)
        s = e.state()
        s["seen_doc_ids"], s["pending_orders"] = {}, {}
        e.p("state.json").write_text(json.dumps(s))
        e.broker.orders = lambda after=None, status="all", limit=500: []      # prefetch misses them
        e.run(ARGS)
        self.assertEqual(e.broker.submitted, first)
        e.cleanup()

    def test_history_state_log_and_site_files_written(self):
        e = standard_env()
        e.run(ARGS)
        hist = store.read_csv(e.p("history.csv"))
        self.assertEqual(list(hist[0]), store.HISTORY_FIELDS)
        self.assertEqual((hist[0]["date"], hist[0]["equity"], hist[0]["spy_close"]), ("2026-11-18", "100000.00", "602.0000"))
        e.run(ARGS)                                                        # same date again: row replaced, not duplicated
        self.assertEqual(len(store.read_csv(e.p("history.csv"))), 1)
        log = e.log_rows()
        self.assertEqual({r["kind"] for r in log}, {"signal", "order"})
        self.assertEqual(sum(1 for r in log if r["kind"] == "order"), 2)
        parsed = house.read_parsed(e.p("parsed_trades.csv"))
        self.assertEqual(len(parsed), 3)
        e.cleanup()

    def test_scanned_filing_logged_ikke_laesbar(self):
        e = Env([{"doc_id": "9100001", "filing_date": "2026-11-17"}], {})
        self.assertEqual(e.run(ARGS), 0)
        self.assertTrue(any("ikke læsbar" in r["reason"] for r in e.log_rows()))
        self.assertIn("9100001", e.state()["seen_doc_ids"])
        self.assertFalse(any("9100001.pdf" in u for u in e.session.urls))   # not even downloaded
        self.assertEqual(e.live_json()["last_run"]["filings_checked"], 1)
        e.cleanup()

    def test_textless_pdf_logged_ikke_laesbar(self):
        e = Env([{"doc_id": "20040009", "filing_date": "2026-11-17"}], {})
        e.parse = lambda root, row, pdf: {"status": "unreadable", "trades": [], "warnings": ["scannet"]}
        self.assertEqual(e.run(ARGS), 0)
        self.assertTrue(any("ikke læsbar" in r["reason"] for r in e.log_rows()))
        self.assertEqual(e.state()["seen_doc_ids"]["20040009"]["status"], "unreadable")
        e.cleanup()

    def test_only_filings_since_start_and_before_session_date(self):
        filings = [{"doc_id": "20040001", "filing_date": "2026-11-17"},
                   {"doc_id": "20040002", "filing_date": "2026-11-05"},     # before the start date
                   {"doc_id": "20040003", "filing_date": "2026-11-18"},     # filed on the session day: wait
                   {"doc_id": "20040004", "filing_date": "2026-11-17", "type": "A"}]   # not a PTR
        e = Env(filings, {"20040001": [trade_row("20040001", "AAA")], "20040002": [], "20040003": []})
        e.run(ARGS)
        self.assertEqual(list(e.state()["seen_doc_ids"]), ["20040001"])
        e.cleanup()

    def test_too_late_entry_is_ignored(self):
        e = Env([{"doc_id": "20040001", "filing_date": "2026-11-05"}],
                {"20040001": [trade_row("20040001", "AAA", filing="2026-11-05", txn="2026-11-02")]})
        e.run(["--start-date", "2026-11-01"])
        self.assertEqual(e.broker.submitted, [])
        self.assertTrue(any("for sent" in r["reason"] for r in e.log_rows()))
        e.cleanup()

    def test_rule_exclusions_come_from_backtest_logic(self):
        trades = [trade_row("20040001", "AAA", description="RSU vesting grant"),            # non-discretionary
                  trade_row("20040001", "BBB", short_related=True),                          # short-related
                  trade_row("20040001", "CCC", include_v1=False, include_reason="kode OP"),  # not stock/ETF
                  trade_row("20040001", "AAA", ttype="sell")]
        e = Env([{"doc_id": "20040001", "filing_date": "2026-11-17"}], {"20040001": trades})
        e.run(ARGS)
        self.assertEqual(e.broker.submitted, [])
        by = {(r["ticker"], r["trade_type"]): r["reason"] for r in e.log_rows() if r["kind"] == "signal"}
        self.assertIn("ikke medlemmets eget valg", by[("AAA", "buy")])
        self.assertIn("short", by[("BBB", "buy")])
        self.assertEqual(by[("CCC", "buy")], "kode OP")
        self.assertEqual(e.live_json()["last_run"]["new_signals"], 0)
        e.cleanup()

    def test_duplicate_across_filings_in_live_data(self):
        t1, t2 = trade_row("20040001", "AAA"), trade_row("20040002", "AAA", filing="2026-11-17", filing_status="Amended")
        e = Env([{"doc_id": "20040001", "filing_date": "2026-11-16"}, {"doc_id": "20040002", "filing_date": "2026-11-17"}],
                {"20040001": [{**t1, "filing_date": "2026-11-16"}], "20040002": [t2]})
        e.run(ARGS)
        self.assertEqual(len(e.broker.submitted), 1)
        e.cleanup()

    def test_amended_filing_without_known_original_is_ignored(self):
        e = Env([{"doc_id": "20040001", "filing_date": "2026-11-17"}],
                {"20040001": [trade_row("20040001", "AAA", filing_status="Amended")]})
        e.run(ARGS)
        self.assertEqual(e.broker.submitted, [])
        self.assertTrue(any("Amended" in r["reason"] for r in e.log_rows()))
        e.cleanup()

    def test_later_deletion_of_a_ordered_trade_is_logged_and_position_kept(self):
        e = Env([{"doc_id": "20040001", "filing_date": "2026-11-16"}],
                {"20040001": [trade_row("20040001", "AAA", filing="2026-11-16")]})
        e.run(ARGS)
        self.assertEqual(len(e.broker.submitted), 1)
        e.filings.append({"doc_id": "20040002", "filing_date": "2026-11-17"})
        e.trades_by_doc["20040002"] = [trade_row("20040002", "AAA", filing="2026-11-17", filing_status="Deleted")]
        e.broker.clock_ = {"timestamp": "2026-11-19T07:45:00-05:00", "is_open": False,
                           "next_open": "2026-11-19T09:30:00-05:00", "next_close": "2026-11-19T16:00:00-05:00"}
        e.run(ARGS, now=datetime(2026, 11, 19, 12, 45, tzinfo=timezone.utc))
        self.assertTrue(any("slettet af indberetning" in r["reason"] for r in e.log_rows()))
        self.assertEqual(len(e.broker.submitted), 1)
        e.cleanup()

    def test_untradable_symbol_logged_not_crashing(self):
        e = standard_env()
        e.broker.assets["AAA"] = {"symbol": "AAA", "status": "inactive", "tradable": False, "exchange": "NASDAQ"}
        self.assertEqual(e.run(ARGS), 0)
        self.assertTrue(any("ikke handelbar" in r["reason"] for r in e.log_rows()))
        e.cleanup()

    def test_rejected_order_logged_not_crashing(self):
        e = standard_env()
        e.broker.submit_order = mock.Mock(side_effect=OrderRejected("asset is halted"))
        self.assertEqual(e.run(ARGS), 0)
        rows = [r for r in e.log_rows() if r["kind"] == "signal" and r["ticker"] == "AAA"]
        self.assertEqual(rows[0]["decision"], "ignored")
        self.assertIn("afvist", rows[0]["reason"])
        e.cleanup()

    def test_fractional_opg_rejected_falls_back_to_whole_shares_and_is_remembered(self):
        e = standard_env()
        e.broker.reject_notional_opg = True
        e.run(ARGS)
        self.assertEqual(len(e.broker.submitted), 2)
        for o in e.broker.submitted:
            self.assertIsNone(o["notional"])
            self.assertTrue(float(o["qty"]).is_integer())
        self.assertFalse(e.state()["capabilities"]["notional_opg"])
        e.broker.submitted.clear()                       # the next filing goes straight to whole shares
        e.filings.append({"doc_id": "20040005", "filing_date": "2026-11-17"})
        e.trades_by_doc["20040005"] = [trade_row("20040005", "AAA", owner="spouse", txn="2026-11-13")]
        e.run(ARGS)
        self.assertEqual(len(e.broker.submitted), 1)
        self.assertIsNone(e.broker.submitted[0]["notional"])
        e.cleanup()

    def test_whole_share_too_expensive_is_skipped(self):
        e = standard_env()
        cfg = json.loads((e.root / "live" / "strategy.json").read_text())
        cfg["orders"]["mode"] = "whole_shares"
        cfg["sizing"]["base_pct_of_equity"] = 0.05          # 50 USD target: below the 105 USD share price
        (e.root / "live" / "strategy.json").write_text(json.dumps(cfg))
        e.trades_by_doc["20040001"] = [trade_row("20040001", "AAA", low="1001", high="15000")]   # midpoint = median
        e.run(ARGS)
        self.assertEqual(e.broker.submitted, [])
        self.assertTrue(any("for dyrt" in r["reason"] for r in e.log_rows()))
        e.cleanup()

    def test_non_empty_account_refused_on_first_real_run(self):
        b = FakeBroker()
        b.pos = [{"symbol": "XYZ", "qty": "1", "qty_available": "1", "market_value": "10", "current_price": "10"}]
        e = standard_env(broker=b)
        self.assertEqual(e.run(ARGS), 2)
        self.assertEqual(b.submitted, [])
        self.assertFalse(e.p("state.json").exists())
        e.cleanup()

    def test_market_open_defers_new_filings(self):
        e = standard_env()
        e.broker.clock_ = {"timestamp": "2026-11-18T11:00:00-05:00", "is_open": True,
                           "next_open": "2026-11-19T09:30:00-05:00", "next_close": "2026-11-18T16:00:00-05:00"}
        e.run(ARGS)
        self.assertEqual(e.broker.submitted, [])
        self.assertEqual(e.state()["seen_doc_ids"], {})                    # untouched: picked up by a later run
        e.cleanup()

    def test_alpaca_error_status_and_exit_code(self):
        e = standard_env()
        e.broker.account = mock.Mock(side_effect=AlpacaError("HTTP 500"))
        self.assertEqual(e.run(ARGS), 4)
        e.cleanup()


class TestLifecycle(unittest.TestCase):
    def test_fill_creates_lot_with_planned_exit_then_exit_order(self):
        e = standard_env()
        e.run(ARGS)
        cid = next(o["client_order_id"] for o in e.broker.submitted if o["symbol"] == "AAA")
        e.broker.fill(cid, 104.0, 39)
        e.broker.pos = [{"symbol": "AAA", "qty": "39", "qty_available": "39", "market_value": "4100",
                         "current_price": "105.0"}]
        e.broker.clock_ = {"timestamp": "2026-11-19T07:45:00-05:00", "is_open": False,
                           "next_open": "2026-11-19T09:30:00-05:00", "next_close": "2026-11-19T16:00:00-05:00"}
        e.run(ARGS, now=datetime(2026, 11, 19, 12, 45, tzinfo=timezone.utc))
        lot = e.state()["lots"][cid]
        self.assertEqual((lot["status"], lot["entry_price"], lot["qty"], lot["entry_date"]), ("open", 104.0, 39.0, "2026-11-18"))
        self.assertEqual(lot["planned_exit"], "2027-05-18")
        fills = [r for r in e.log_rows() if r["kind"] == "fill"]
        self.assertEqual((fills[0]["fill_price"], fills[0]["filled_qty"]), ("104.0", "39.0"))
        j = e.live_json()
        self.assertEqual(j["positions"][0]["ticker"], "AAA")
        self.assertEqual(j["positions"][0]["return_pct"], round((105 / 104 - 1) * 100, 2))
        self.assertEqual(j["positions"][0]["planned_exit"], "2027-05-18")
        # the exit date arrives: cls sell of exactly the lot's quantity
        e.broker.submitted.clear()
        e.broker.clock_ = {"timestamp": "2027-05-18T07:45:00-04:00", "is_open": False,
                           "next_open": "2027-05-18T09:30:00-04:00", "next_close": "2027-05-18T16:00:00-04:00"}
        e.run(ARGS, now=datetime(2027, 5, 18, 12, 45, tzinfo=timezone.utc))
        sells = [o for o in e.broker.submitted if o["side"] == "sell"]
        self.assertEqual([(o["symbol"], o["qty"], o["time_in_force"]) for o in sells], [("AAA", "39", "cls")])
        # sell fills: lot closed
        e.broker.fill(sells[0]["client_order_id"], 120.0, 39, when="2027-05-18T20:00:00Z")
        e.broker.clock_ = {"timestamp": "2027-05-19T07:45:00-04:00", "is_open": False,
                           "next_open": "2027-05-19T09:30:00-04:00", "next_close": "2027-05-19T16:00:00-04:00"}
        e.broker.pos = []
        e.run(ARGS, now=datetime(2027, 5, 19, 12, 45, tzinfo=timezone.utc))
        lot = e.state()["lots"][cid]
        self.assertEqual((lot["status"], lot["exit_price"], lot["exit_date"]), ("closed", 120.0, "2027-05-18"))
        e.cleanup()

    def test_canceled_buy_creates_no_lot(self):
        e = standard_env()
        e.run(ARGS)
        cid = e.broker.submitted[0]["client_order_id"]
        e.broker.orders_by_cid[cid]["status"] = "canceled"
        e.run(ARGS, now=datetime(2026, 11, 19, 12, 45, tzinfo=timezone.utc))
        self.assertNotIn(cid, e.state()["lots"])
        self.assertNotIn(cid, e.state()["pending_orders"])
        e.cleanup()

    def test_member_sale_filing_triggers_exit_under_member_sells(self):
        e = standard_env()
        cfg = json.loads((e.root / "live" / "strategy.json").read_text())
        cfg["exit"]["rule"] = "member_sells"
        (e.root / "live" / "strategy.json").write_text(json.dumps(cfg))
        e.run(ARGS)
        cid = next(o["client_order_id"] for o in e.broker.submitted if o["symbol"] == "AAA")
        e.broker.fill(cid, 104.0, 39)
        e.broker.pos = [{"symbol": "AAA", "qty": "39", "qty_available": "39", "market_value": "4100", "current_price": "105"}]
        e.filings.append({"doc_id": "20040010", "filing_date": "2026-11-18"})
        e.trades_by_doc["20040010"] = [trade_row("20040010", "AAA", ttype="sell", txn="2026-11-17", filing="2026-11-18")]
        e.broker.clock_ = {"timestamp": "2026-11-19T07:45:00-05:00", "is_open": False,
                           "next_open": "2026-11-19T09:30:00-05:00", "next_close": "2026-11-19T16:00:00-05:00"}
        e.broker.submitted.clear()
        e.run(ARGS, now=datetime(2026, 11, 19, 12, 45, tzinfo=timezone.utc))
        sells = [o for o in e.broker.submitted if o["side"] == "sell"]
        self.assertEqual([(o["symbol"], o["time_in_force"], o["qty"]) for o in sells], [("AAA", "opg", "39")])
        e.cleanup()


class TestSimulatedFill(unittest.TestCase):
    TIMING = "opg orders must be submitted after 7:00pm and before 9:28am"

    def _queue_run(self):
        e = standard_env()
        real = e.broker.submit_order

        def submit(symbol, side, tif, client_order_id, qty=None, notional=None):
            if side == "buy" and symbol == "AAA":
                raise OrderRejected(self.TIMING)
            return real(symbol, side, tif, client_order_id, qty=qty, notional=notional)
        e.broker.submit_order = submit
        self.assertEqual(e.run(ARGS), 0)
        return e

    def test_timing_rejection_is_queued_and_logged_as_simulated_buy(self):
        e = self._queue_run()
        st = e.state()
        self.assertEqual(len(st["sim_pending"]), 1)
        p = next(iter(st["sim_pending"].values()))
        self.assertEqual((p["symbol"], p["session"]), ("AAA", "2026-11-18"))
        sig = [r for r in e.log_rows() if r["kind"] == "signal" and r["ticker"] == "AAA"][0]
        self.assertEqual(sig["decision"], "taken")
        self.assertIn("simuleret fyld", sig["reason"])
        e.cleanup()

    def test_other_rejections_are_not_simulated(self):
        e = standard_env()
        e.broker.submit_order = mock.Mock(side_effect=OrderRejected("asset is halted"))
        e.run(ARGS)
        self.assertEqual(e.state()["sim_pending"], {})
        e.cleanup()

    def test_resolved_at_official_open_valued_and_closed_on_paper(self):
        e = self._queue_run()
        cid = next(iter(e.state()["sim_pending"]))
        target = e.state()["sim_pending"][cid]["target"]
        e.broker.bar_data["AAA"] = [{"t": "2026-11-18T05:00:00Z", "o": 101.0, "h": 104, "l": 99, "c": 103.0, "v": 9},
                                    {"t": "2026-11-19T05:00:00Z", "o": 103.0, "h": 111, "l": 102, "c": 110.0, "v": 9}]
        e.broker.bar_data["SPY"] = [bar("2026-11-17", 602.0), bar("2026-11-18", 604.0), bar("2026-11-19", 606.0)]
        e.broker.clock_ = {"timestamp": "2026-11-20T07:45:00-05:00", "is_open": False,
                           "next_open": "2026-11-20T09:30:00-05:00", "next_close": "2026-11-20T16:00:00-05:00"}
        e.run(ARGS, now=datetime(2026, 11, 20, 12, 45, tzinfo=timezone.utc))
        st = e.state()
        lot = st["lots"][cid]
        qty = int(target // 101.0)
        self.assertEqual((lot["simulated"], lot["entry_price"], lot["entry_date"], lot["qty"]), (True, 101.0, "2026-11-18", float(qty)))
        self.assertEqual(lot["planned_exit"], "2027-05-18")
        self.assertEqual(st["sim_pending"], {})
        j = e.live_json()
        pos = [x for x in j["positions"] if x["ticker"] == "AAA"][0]
        self.assertTrue(pos["simulated"])
        self.assertEqual(pos["return_pct"], round((110 / 101 - 1) * 100, 2))
        hist = store.read_csv(e.p("history.csv"))
        self.assertAlmostEqual(float(hist[-1]["sim_pnl"]), qty * 9.0, places=2)
        self.assertAlmostEqual(float(hist[-1]["equity"]), 100000 + qty * 9.0, places=2)
        # no real sell is ever sent for a simulated lot, and reconciliation ignores it
        self.assertFalse([o for o in e.broker.submitted if o["symbol"] == "AAA" and o["side"] == "sell"])
        self.assertNotIn("afstemning", e.out)
        # planned exit reached: closed on paper at that session's close
        e.broker.bar_data["AAA"].append({"t": "2027-05-18T04:00:00Z", "o": 120, "h": 126, "l": 119, "c": 125.0, "v": 9})
        e.broker.clock_ = {"timestamp": "2027-05-19T07:45:00-04:00", "is_open": False,
                           "next_open": "2027-05-19T09:30:00-04:00", "next_close": "2027-05-19T16:00:00-04:00"}
        e.run(ARGS, now=datetime(2027, 5, 19, 12, 0, tzinfo=timezone.utc))
        lot = e.state()["lots"][cid]
        self.assertEqual((lot["status"], lot["exit_price"], lot["exit_date"]), ("closed", 125.0, "2027-05-18"))
        self.assertFalse([o for o in e.broker.submitted if o["symbol"] == "AAA" and o["side"] == "sell"])
        e.cleanup()

    def test_history_rows_before_the_simulated_lot_are_backfilled(self):
        e = self._queue_run()
        cid = next(iter(e.state()["sim_pending"]))
        # an earlier row written after the 18/11 close but before the simulated lot existed (as the real 20/11 row)
        hist = store.read_csv(e.p("history.csv"))
        self.assertEqual(hist[0]["sim_pnl"], "")
        hist[0].update(spy_close_date="2026-11-18")
        store.rewrite_history(e.p("history.csv"), hist)
        e.broker.bar_data["AAA"] = [{"t": "2026-11-18T05:00:00Z", "o": 100.0, "h": 104, "l": 99, "c": 104.0, "v": 9},
                                    {"t": "2026-11-19T05:00:00Z", "o": 103.0, "h": 111, "l": 102, "c": 110.0, "v": 9}]
        e.broker.bar_data["SPY"] = [bar("2026-11-17", 602.0), bar("2026-11-18", 604.0), bar("2026-11-19", 606.0)]
        e.broker.clock_ = {"timestamp": "2026-11-20T07:45:00-05:00", "is_open": False,
                           "next_open": "2026-11-20T09:30:00-05:00", "next_close": "2026-11-20T16:00:00-05:00"}
        e.run(ARGS, now=datetime(2026, 11, 20, 12, 45, tzinfo=timezone.utc))
        hist = {h["date"]: h for h in store.read_csv(e.p("history.csv"))}
        qty = e.state()["lots"][cid]["qty"]
        self.assertAlmostEqual(float(hist["2026-11-18"]["sim_pnl"]), qty * 4.0, places=2)      # close 18/11
        self.assertAlmostEqual(float(hist["2026-11-18"]["equity"]), 100000 + qty * 4.0, places=2)
        self.assertAlmostEqual(float(hist["2026-11-20"]["sim_pnl"]), qty * 10.0, places=2)     # latest close
        # a rerun does not add the P&L twice
        e.run(ARGS, now=datetime(2026, 11, 20, 12, 50, tzinfo=timezone.utc))
        hist = {h["date"]: h for h in store.read_csv(e.p("history.csv"))}
        self.assertAlmostEqual(float(hist["2026-11-18"]["equity"]), 100000 + qty * 4.0, places=2)
        e.cleanup()

    def test_missing_open_is_never_guessed(self):
        e = self._queue_run()
        e.broker.bar_data["AAA"] = []
        e.broker.clock_ = {"timestamp": "2026-12-01T07:45:00-05:00", "is_open": False,
                           "next_open": "2026-12-01T09:30:00-05:00", "next_close": "2026-12-01T16:00:00-05:00"}
        e.run(ARGS, now=datetime(2026, 12, 1, 12, 45, tzinfo=timezone.utc))
        st = e.state()
        self.assertEqual(st["sim_pending"], {})
        self.assertFalse([l for l in st["lots"].values() if l.get("simulated")])
        self.assertTrue([r for r in e.log_rows() if r.get("order_status") == "sim_dropped"])
        e.cleanup()


class TestHouseUnreachable(unittest.TestCase):
    def test_blocked_index_writes_status_and_trades_nothing(self):
        e = standard_env(index_status=403)
        code = e.run(ARGS)
        self.assertEqual(code, 3)
        self.assertEqual(e.broker.submitted, [])
        j = e.live_json()
        self.assertEqual(j["last_run"]["status"], "house_unreachable")
        self.assertIn("house.gov", j["last_run"]["message"])
        self.assertEqual(e.state()["seen_doc_ids"], {})
        e.cleanup()

    def test_network_error_also_house_unreachable_and_no_exit_orders(self):
        import requests

        class Down:
            headers = {}

            def get(self, url, timeout=None):
                raise requests.ConnectionError("down")
        e = standard_env()
        e.run(ARGS)
        cid = next(o["client_order_id"] for o in e.broker.submitted if o["symbol"] == "AAA")
        e.broker.fill(cid, 100.0, 10)
        e.broker.pos = [{"symbol": "AAA", "qty": "10", "qty_available": "10", "market_value": "1000",
                         "current_price": "100"}]
        e.broker.clock_ = {"timestamp": "2026-11-19T07:45:00-05:00", "is_open": False,
                           "next_open": "2026-11-19T09:30:00-05:00", "next_close": "2026-11-19T16:00:00-05:00"}
        day2 = datetime(2026, 11, 19, 12, 45, tzinfo=timezone.utc)
        e.run(ARGS, now=day2)                                           # creates the lot from the fill
        s = e.state()
        s["lots"][cid]["planned_exit"] = "2026-11-19"                    # make the exit due today
        e.p("state.json").write_text(json.dumps(s))
        e.broker.submitted.clear()
        with mock.patch("time.sleep"):
            code = e.run(ARGS, now=day2, session=Down())
        self.assertEqual(code, 3)
        self.assertEqual(e.broker.submitted, [])                         # no trading at all
        self.assertEqual(e.live_json()["last_run"]["status"], "house_unreachable")
        e.run(ARGS, now=day2)                                            # house back: the due exit goes out
        self.assertEqual([(o["side"], o["time_in_force"]) for o in e.broker.submitted], [("sell", "cls")])
        e.cleanup()

    def test_index_and_pdf_requests_pause_half_a_second(self):
        sleeps = []
        sess = FakeHouseSession([{"doc_id": "20040001", "filing_date": "2026-11-17"}])
        rows = house.fetch_index(sess, 2026, sleeps.append)
        house.download_pdf(sess, rows[0], sleeps.append)
        self.assertEqual(sleeps, [0.5, 0.5])

    def test_requests_use_generic_user_agent_and_pause(self):
        self.assertEqual(house.USER_AGENT, "congress-fund-research/0.1")
        self.assertEqual(house.PAUSE_SECONDS, 0.5)
        self.assertEqual(house.make_session().headers["User-Agent"], "congress-fund-research/0.1")
        self.assertEqual(house.index_years(date(2027, 1, 5)), [2026, 2027])
        self.assertEqual(house.index_years(date(2026, 11, 5)), [2026])


class TestBrokerHttp(unittest.TestCase):
    """live.broker.Broker against a mocked requests session (no network)."""

    class Sess:
        def __init__(self, responses):
            self.responses, self.calls = list(responses), []

        def request(self, method, url, headers=None, params=None, json=None, timeout=None):
            self.calls.append((method, url, headers, json))
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    class Resp:
        def __init__(self, status, body=None, text=""):
            self.status_code, self._b, self.text = status, body, text

        def json(self):
            if self._b is None:
                raise ValueError("no json")
            return self._b

    def broker(self, responses):
        s = self.Sess(responses)
        return Broker(KEY, SECRET, PAPER, session=s, sleep=lambda x: None), s

    def test_requests_go_to_paper_host_with_headers(self):
        b, s = self.broker([self.Resp(200, {"equity": "1"})])
        b.account()
        self.assertEqual(s.calls[0][1], PAPER + "/v2/account")
        self.assertEqual(s.calls[0][2]["APCA-API-KEY-ID"], KEY)

    def test_order_payload_and_rejection_mapping(self):
        b, s = self.broker([self.Resp(200, {"id": "1"}), self.Resp(422, {"message": "fractional orders must be DAY orders"}),
                            self.Resp(422, {"message": "client_order_id must be unique"})])
        b.submit_order("AAA", "buy", "opg", "cid1", notional="100.00")
        self.assertEqual(s.calls[0][3], {"symbol": "AAA", "side": "buy", "type": "market", "time_in_force": "opg",
                                         "client_order_id": "cid1", "notional": "100.00"})
        with self.assertRaises(OrderRejected):
            b.submit_order("AAA", "buy", "opg", "cid2", notional="100.00")
        with self.assertRaises(DuplicateClientOrderId):
            b.submit_order("AAA", "buy", "opg", "cid1", qty="1")

    def test_post_is_never_retried_but_get_is(self):
        import requests
        b, s = self.broker([requests.ConnectionError("x")])
        with self.assertRaises(AlpacaError):
            b.submit_order("AAA", "buy", "opg", "cid", qty="1")
        self.assertEqual(len(s.calls), 1)
        b, s = self.broker([self.Resp(503), self.Resp(200, {"is_open": False})])
        self.assertEqual(b.clock(), {"is_open": False})
        self.assertEqual(len(s.calls), 2)

    def test_404_lookup_returns_none_and_errors_are_redacted(self):
        b, _ = self.broker([self.Resp(404, {})])
        self.assertIsNone(b.order_by_client_id("nope"))
        b, _ = self.broker([self.Resp(403, None, text=f"denied for {KEY} {SECRET}")])
        with self.assertRaises(AlpacaError) as cm:
            b.account()
        self.assertNotIn(KEY, str(cm.exception))
        self.assertNotIn(SECRET, str(cm.exception))


class TestStatePersistence(unittest.TestCase):
    def test_roundtrip(self):
        d = Path(tempfile.mkdtemp())
        s = store.new_state("2026-11-10", 100000.0, "t", "h", "n")
        s["lots"]["a"] = {"qty": 1.5, "status": "open"}
        s["seen_doc_ids"]["1"] = {"status": "parsed"}
        store.save_state(d / "state.json", s)
        self.assertEqual(store.load_state(d / "state.json"), s)
        self.assertIsNone(store.load_state(d / "missing.json"))
        shutil.rmtree(d)

    def test_state_survives_between_runs(self):
        e = standard_env()
        e.run(ARGS)
        s1 = e.state()
        self.assertEqual(sorted(s1["seen_doc_ids"]), ["20040001"])
        self.assertEqual(len(s1["pending_orders"]), 2)
        e.run(ARGS)
        s2 = e.state()
        self.assertEqual(s1["seen_doc_ids"], s2["seen_doc_ids"])
        self.assertEqual(s1["pending_orders"], s2["pending_orders"])
        e.cleanup()

    def test_history_upsert(self):
        d = Path(tempfile.mkdtemp())
        store.upsert_history(d / "h.csv", {"date": "2026-11-18", "equity": "1"})
        store.upsert_history(d / "h.csv", {"date": "2026-11-17", "equity": "2"})
        rows = store.upsert_history(d / "h.csv", {"date": "2026-11-18", "equity": "3"})
        self.assertEqual([(r["date"], r["equity"]) for r in rows], [("2026-11-17", "2"), ("2026-11-18", "3")])
        shutil.rmtree(d)


class TestLiveJsonSchema(unittest.TestCase):
    def test_schema_and_values(self):
        e = standard_env()
        e.run(ARGS)
        j = e.live_json()
        self.assertEqual(set(j), {"schema_version", "generated_at", "start_date", "start_equity", "last_run",
                                  "latest_equity", "curve", "total_return", "positions", "decisions", "strategy", "simulated_fills",
                                  "disclaimer"})
        self.assertEqual(j["disclaimer"], "Papirfond med fiktive penge. Ikke investeringsrådgivning.")
        self.assertEqual((j["start_date"], j["start_equity"]), ("2026-11-10", 100000.0))
        self.assertEqual(set(j["last_run"]), {"time_utc", "status", "filings_checked", "new_signals", "orders_sent",
                                              "message", "dry_run"})
        self.assertEqual(j["last_run"]["status"], "ok")
        self.assertEqual((j["last_run"]["filings_checked"], j["last_run"]["new_signals"], j["last_run"]["orders_sent"]), (1, 3, 2))
        self.assertEqual(set(j["curve"]), {"dates", "fund", "spy"})
        self.assertEqual((j["curve"]["fund"], j["curve"]["spy"]), ([100.0], [100.0]))             # normalised to 100
        self.assertEqual(set(j["total_return"]), {"fund_pct", "spy_pct", "excess_pct_points"})
        self.assertEqual(j["positions"], [])                                                       # nothing filled yet
        self.assertEqual(len(j["decisions"]), 3)
        for d in j["decisions"]:
            self.assertEqual(set(d), {"member", "ticker", "filed", "decision", "reason"})
            self.assertIn(d["decision"], ("taken", "skipped", "ignored"))
        self.assertEqual(set(j["strategy"]), {"name", "hash", "changed_since_start", "description", "config"})
        self.assertIn("Fonden kopierer", j["strategy"]["description"])
        json.dumps(j)                                                                              # serialisable

    def test_curve_normalisation_and_total_return(self):
        hist = [{"date": "2026-11-18", "equity": "100000", "spy_close": "600"},
                {"date": "2026-11-19", "equity": "103000", "spy_close": "606"},
                {"date": "2026-11-20", "equity": "", "spy_close": "610"}]       # incomplete rows are left out
        c = site.build_curve(hist)
        self.assertEqual(c, {"dates": ["2026-11-18", "2026-11-19"], "fund": [100.0, 103.0], "spy": [100.0, 101.0]})
        self.assertEqual(site.total_return(c), {"fund_pct": 3.0, "spy_pct": 1.0, "excess_pct_points": 2.0})

    def test_last_25_decisions_newest_first(self):
        rows = [{"kind": "signal", "on_site": "1", "member": "M", "ticker": f"T{i}", "filing_date": "2026-11-17",
                 "decision": "taken", "reason": "r"} for i in range(40)]
        rows.insert(5, {"kind": "signal", "on_site": "0", "member": "M", "ticker": "NOISE", "filing_date": "x",
                        "decision": "ignored", "reason": "bond"})
        d = site.build_decisions(rows)
        self.assertEqual(len(d), 25)
        self.assertEqual((d[0]["ticker"], d[-1]["ticker"]), ("T39", "T15"))
        self.assertNotIn("NOISE", [x["ticker"] for x in d])

    def test_house_unreachable_json_has_same_schema(self):
        e = standard_env(index_status=403)
        e.run(ARGS)
        self.assertEqual(e.live_json()["last_run"]["status"], "house_unreachable")
        self.assertIn("curve", e.live_json())
        e.cleanup()


class TestRealPdfPipeline(unittest.TestCase):
    """The real 2016 fixture PDF through extract -> parse (backtest parser) -> signals; only HTTP and Alpaca are faked."""

    def test_pdf_end_to_end(self):
        root = Path(tempfile.mkdtemp())
        (root / "live").mkdir()
        shutil.copy(PROJECT / "live" / "strategy.json", root / "live" / "strategy.json")
        pdf = (PROJECT / "tests" / "fixtures" / "ptr" / "pdf" / "20006116.pdf").read_bytes()
        sess = FakeHouseSession([{"doc_id": "20006116", "filing_date": "2016-10-28", "first": "Lou", "last": "Barletta"}])
        sess.get = lambda url, timeout=None, _o=sess.get: FakeResp(200, pdf) if url.endswith(".pdf") else _o(url, timeout)
        b = FakeBroker()
        b.bar_data = {"AMZN": [bar("2016-10-27", 800.0), bar("2016-10-28", 800.0), bar("2016-10-31", 805.0)],
                      "SPY": [bar("2016-10-31", 212.0)]}
        clock = {"timestamp": "2016-11-01T07:45:00-04:00", "is_open": False, "next_open": "2016-11-01T09:30:00-04:00",
                 "next_close": "2016-11-01T16:00:00-04:00"}
        b.clock_ = clock
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_daily.run_main(["--start-date", "2016-10-01"], dict(ENV), root=root, broker=b, house_session=sess,
                                      now=datetime(2016, 11, 1, 11, 45, tzinfo=timezone.utc), sleep=lambda s: None)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual([o["symbol"] for o in b.submitted], ["AMZN"])
        self.assertEqual(b.submitted[0]["time_in_force"], "opg")
        self.assertTrue((root / "data" / "live" / "cache" / "pdf" / "2016" / "20006116.pdf").exists())
        log = store.read_csv(root / "data" / "live" / "trades_log.csv")
        amzn = [r for r in log if r["ticker"] == "AMZN"][0]
        self.assertEqual((amzn["member"], amzn["decision"]), ("Lou Barletta", "taken"))
        self.assertEqual(amzn["ref_close"], "800.0")
        shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
