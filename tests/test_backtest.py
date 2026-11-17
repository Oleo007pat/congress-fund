"""Unit tests for src/backtest.   Run:  python3 -m unittest tests.test_backtest

All tests use small synthetic price series (no files under data/ are read).
"""
import unittest

import numpy as np
import pandas as pd

from src.backtest import config
from src.backtest.analysis import newey_west_ols, significance, yearly_table
from src.backtest.engine import (add_canonical_member, member_key, nondiscretionary_category, select_buy_signals,
                                 select_sales)
from src.backtest.engine import (cut_exit, exit_fixed_hold, exit_member_sells, find_entry, find_reference,
                                 first_sale_exit_day, sample_of, skip_decision, skip_mask, trade_returns,
                                 amount_weight, bench_window_return)
from src.backtest.portfolio import (naive_portfolio_daily_returns, portfolio_daily_returns, series_metrics,
                                    select_configuration)
from src.backtest.prices import PriceBook

CAL = pd.bdate_range("2020-01-02", periods=400)  # Mon-Fri synthetic trading calendar


def frame(dates, opens, closes, volume=1000):
    vol = volume if np.ndim(volume) else [volume] * len(dates)
    return pd.DataFrame({"date": [d.strftime("%Y-%m-%d") for d in dates], "open": opens, "high": closes,
                         "low": opens, "close": closes, "volume": vol})


def linear_book(extra=None):
    """SPY + AAA: open_t = 100 + t, close_t = 100.5 + t on every calendar day."""
    t = np.arange(len(CAL))
    frames = {"SPY": frame(CAL, 300 + t, 300.5 + t), "AAA": frame(CAL, 100.0 + t, 100.5 + t)}
    frames.update(extra or {})
    return PriceBook(CAL.values, frames)


def d(i):
    return CAL[i].strftime("%Y-%m-%d")


class TestEntry(unittest.TestCase):
    def setUp(self):
        self.pb = linear_book()
        self.a = self.pb.col["AAA"]

    def test_entry_is_next_trading_day_after_filing(self):
        e = find_entry(self.pb, self.a, d(10))
        self.assertEqual(e.idx, 11)
        self.assertEqual(e.price, self.pb.open[11, self.a])  # the OPEN of that day

    def test_filing_on_weekend_enters_next_monday(self):
        fri = CAL[1]  # 2020-01-03
        self.assertEqual(fri.dayofweek, 4)
        sat = (fri + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        e = find_entry(self.pb, self.a, sat)
        self.assertEqual(str(self.pb.cal[e.idx]), "2020-01-06")  # Monday
        # filing on the Friday itself -> also Monday (strictly after)
        self.assertEqual(find_entry(self.pb, self.a, fri.strftime("%Y-%m-%d")).idx, 2)

    def test_entry_never_uses_transaction_date(self):
        # find_entry takes only the filing date; the entry must be strictly after it
        for fi in (0, 20, 100):
            e = find_entry(self.pb, self.a, d(fi))
            self.assertGreater(self.pb.cal[e.idx], np.datetime64(CAL[fi].date()))
            self.assertEqual(e.idx, fi + 1)

    def test_drop_reasons(self):
        t = np.arange(50)
        late = frame(CAL[100:150], 50 + t, 50 + t)   # data starts later
        early = frame(CAL[:50], 50 + t, 50 + t)      # data ends early
        pb = linear_book({"LATE": late, "EARLY": early})
        self.assertEqual(find_entry(pb, None, d(10)).drop_reason, "no_symbol_mapping")
        self.assertEqual(find_entry(pb, pb.col["LATE"], d(10)).drop_reason, "data_starts_after_entry")
        self.assertEqual(find_entry(pb, pb.col["EARLY"], d(200)).drop_reason, "data_ended_before_entry")
        self.assertEqual(find_entry(pb, pb.col["AAA"], d(len(CAL) - 1)).drop_reason, "entry_after_end_of_data")


class TestSkipRule(unittest.TestCase):
    def test_fall_never_skips(self):
        for x in config.X_GRID:
            for rise in (-0.9, -0.5, -0.1, -1e-6, 0.0):
                self.assertFalse(skip_decision(rise, x), (rise, x))

    def test_threshold_boundary(self):
        # rise exactly at X -> taken; above X -> skipped
        self.assertFalse(skip_decision(0.10, 10.0))
        self.assertTrue(skip_decision(0.10 + 1e-6, 10.0))
        self.assertFalse(skip_decision(0.0, 0.0))
        self.assertTrue(skip_decision(1e-6, 0.0))
        # computed from prices: 102/100 - 1 is 0.020000000000000018 in floating point
        self.assertFalse(skip_decision(102.0 / 100.0 - 1.0, 2.0))
        self.assertTrue(skip_decision(102.01 / 100.0 - 1.0, 2.0))

    def test_none_never_skips_and_missing_ref(self):
        self.assertFalse(skip_decision(5.0, None))
        self.assertFalse(skip_decision(float("nan"), None))
        self.assertTrue(skip_decision(float("nan"), 30.0))  # conservative: unverifiable -> no buy

    def test_vectorised_matches_scalar(self):
        rng = np.random.default_rng(1)
        rise = np.r_[rng.normal(0, 0.2, 500), np.nan, 0.02, 0.0, -0.3]
        for x in config.X_GRID:
            self.assertTrue(np.array_equal(skip_mask(rise, x), [skip_decision(r, x) for r in rise]))


class TestReferenceAndLookahead(unittest.TestCase):
    def test_reference_is_close_on_or_before_transaction_date(self):
        pb = linear_book()
        a = pb.col["AAA"]
        ref = find_reference(pb, a, d(5), entry_idx=20)
        self.assertEqual(ref.idx, 5)
        self.assertEqual(ref.price, pb.close[5, a])
        sat = (CAL[1] + pd.Timedelta(days=1)).strftime("%Y-%m-%d")  # Saturday -> Friday's close
        self.assertEqual(find_reference(pb, a, sat, entry_idx=20).idx, 1)

    def test_reference_missing_before_data(self):
        pb = linear_book()
        r = find_reference(pb, pb.col["AAA"], "2019-06-01", entry_idx=20)
        self.assertNotEqual(r.status, "ok")

    def test_decision_independent_of_future_prices(self):
        """Perturb every price after the entry open -> entry, reference, rise and the
        skip decision for every X are unchanged."""
        t = np.arange(len(CAL))
        base_o, base_c = 100.0 + t, 100.5 + t
        filing, txn = 50, 40
        rng = np.random.default_rng(0)
        decisions = []
        for trial in range(5):
            o, c = base_o.copy(), base_c.copy()
            if trial:
                c[filing + 1:] *= rng.uniform(0.2, 5.0, len(c) - filing - 1)  # entry-day close onwards
                o[filing + 2:] *= rng.uniform(0.2, 5.0, len(o) - filing - 2)  # opens after entry day
            pb = PriceBook(CAL.values, {"SPY": frame(CAL, 300 + t, 300.5 + t), "AAA": frame(CAL, o, c)})
            a = pb.col["AAA"]
            e = find_entry(pb, a, d(filing))
            r = find_reference(pb, a, d(txn), e.idx)
            rise = e.price / r.price - 1
            decisions.append((e.idx, e.price, r.price, rise, tuple(skip_decision(rise, x) for x in config.X_GRID)))
        self.assertTrue(all(x == decisions[0] for x in decisions))

    def test_reference_not_taken_across_segments(self):
        t = np.arange(len(CAL))
        v = np.ones(len(CAL)) * 1000
        v[100:150] = 0  # 50 zero-volume days, then a DIFFERENT price level -> new piece (reused ticker)
        px = np.where(t >= 150, 5.0, 1.0) * (10.0 + t)
        pb = linear_book({"GAP": frame(CAL, px, px + 0.5, v)})
        g = pb.col["GAP"]
        self.assertEqual(pb.n_segments[g], 2)
        e = find_entry(pb, g, d(160))
        self.assertEqual(e.idx, 161)
        self.assertEqual(find_reference(pb, g, d(90), e.idx).status, "missing_ref_in_other_segment")
        self.assertEqual(find_entry(pb, g, d(120)).drop_reason, "entry_in_data_gap")


class TestExits(unittest.TestCase):
    def setUp(self):
        self.pb = linear_book()
        self.a = self.pb.col["AAA"]

    def test_fixed_hold_close_of_first_trading_day_on_or_after(self):
        e = 3  # 2020-01-07
        for months in (3, 6, 12):
            ex = exit_fixed_hold(self.pb, self.a, e, months)
            target = pd.Timestamp(CAL[e]) + pd.DateOffset(months=months)
            exp = int(np.searchsorted(self.pb.cal, np.datetime64(target.date()), "left"))
            self.assertEqual(ex.idx, exp)
            self.assertGreaterEqual(self.pb.cal[ex.idx], np.datetime64(target.date()))
            self.assertEqual(ex.kind, "close")
            self.assertEqual(ex.price, self.pb.close[exp, self.a])
            self.assertEqual(ex.reason, "horizon")

    def test_fixed_hold_target_on_weekend(self):
        # entry Fri 2020-01-31 + 1 month = Sat 2020-02-29 ... use 3 months: 2020-04-30 (Thu); use a weekend case:
        e = int(np.flatnonzero(CAL == pd.Timestamp("2020-02-07"))[0])  # +3m = Thu 2020-05-07; pick Mar 6 +2m?
        e = int(np.flatnonzero(CAL == pd.Timestamp("2020-02-14"))[0])  # +3m = 2020-05-14 Thu
        e = int(np.flatnonzero(CAL == pd.Timestamp("2020-06-12"))[0])  # +3m = 2020-09-12 Sat -> Mon 09-14
        ex = exit_fixed_hold(self.pb, self.a, e, 3)
        self.assertEqual(str(self.pb.cal[ex.idx]), "2020-09-14")

    def test_member_sells_exit_at_open_after_sale_filing(self):
        sales = pd.DataFrame({"transaction_date": [d(5), d(30)], "filing_date": [d(10), d(60)]})
        # buy txn on day 20: the sale with txn day 5 is before the buy -> ignored
        day, f = first_sale_exit_day(self.pb, sales, d(20))
        self.assertEqual(day, 61)
        self.assertEqual(f, d(60))
        ex = exit_member_sells(self.pb, self.a, 25, day, f)
        self.assertEqual((ex.idx, ex.kind, ex.reason), (61, "open", "member_sale"))
        self.assertEqual(ex.price, self.pb.open[61, self.a])

    def test_member_sells_sale_already_public_at_entry(self):
        sales = pd.DataFrame({"transaction_date": [d(22)], "filing_date": [d(23)]})
        day, f = first_sale_exit_day(self.pb, sales, d(20))
        ex = exit_member_sells(self.pb, self.a, 30, day, f)  # we enter on day 30, after the sale was public
        self.assertEqual((ex.idx, ex.kind, ex.reason), (30, "open", "member_sale_at_entry"))
        g, n = trade_returns(self.pb.open[30, self.a], ex.price)
        self.assertEqual(g, 0.0)
        self.assertAlmostEqual(n, (1 - config.COST) ** 2 - 1)

    def test_member_sells_earliest_filed_sale_wins(self):
        sales = pd.DataFrame({"transaction_date": [d(40), d(30)], "filing_date": [d(80), d(70)]})
        day, f = first_sale_exit_day(self.pb, sales, d(20))
        self.assertEqual((day, f), (71, d(70)))

    def test_member_sells_no_sale_open_at_end(self):
        day, f = first_sale_exit_day(self.pb, None, d(20))
        ex = exit_member_sells(self.pb, self.a, 25, day, f)
        self.assertEqual((ex.idx, ex.reason, ex.kind), (len(CAL) - 1, "open_at_end", "close"))

    def test_delisting_exit_at_last_valid_close(self):
        t = np.arange(len(CAL))
        v = np.ones(len(CAL)) * 1000
        v[200:] = 0  # zero-volume filler after day 199 = delisted
        pb = linear_book({"DEL": frame(CAL, 10.0 + t, 10.5 + t, v)})
        c = pb.col["DEL"]
        self.assertFalse(pb.alive[c])
        ex = exit_fixed_hold(pb, c, 150, 12)
        self.assertEqual((ex.idx, ex.reason, ex.kind), (199, "delisted", "close"))
        self.assertEqual(ex.price, 10.5 + 199)
        day, f = first_sale_exit_day(pb, pd.DataFrame({"transaction_date": [d(150)], "filing_date": [d(250)]}), d(140))
        self.assertEqual(exit_member_sells(pb, c, 150, day, f).reason, "delisted")
        # short horizon that ends before the delisting is a normal exit
        self.assertEqual(exit_fixed_hold(pb, c, 100, 3).reason, "horizon")

    def test_cut_exit(self):
        ex = exit_fixed_hold(self.pb, self.a, 100, 12)
        cx = cut_exit(self.pb, self.a, ex, 150)
        self.assertEqual((cx.idx, cx.kind, cx.reason), (150, "close", "cut_2021"))
        self.assertIs(cut_exit(self.pb, self.a, exit_fixed_hold(self.pb, self.a, 10, 3), 150).reason, "horizon")

    def test_bench_window(self):
        b = self.pb.col["SPY"]
        ex = exit_fixed_hold(self.pb, self.a, 10, 3)
        self.assertAlmostEqual(bench_window_return(self.pb, b, 10, ex),
                               self.pb.close[ex.idx, b] / self.pb.open[10, b] - 1)


class TestCostsAndPortfolio(unittest.TestCase):
    def test_costs(self):
        g, n = trade_returns(100.0, 110.0)
        self.assertAlmostEqual(g, 0.10)
        self.assertAlmostEqual(n, 0.999 * 0.999 * 1.10 - 1)
        g, n = trade_returns(50.0, 50.0)
        self.assertAlmostEqual(n, -0.001999, places=12)

    def test_single_trade_portfolio_equals_trade_return(self):
        pb = linear_book()
        a = pb.col["AAA"]
        Cz = np.nan_to_num(pb.close_ff, nan=0.0)
        e, x = 10, 70
        f, l, r = portfolio_daily_returns(Cz, np.array([a]), np.array([e]), np.array([x]),
                                          np.array([pb.open[e, a]]), np.array([pb.close[x, a]]), np.array([1.0]))
        _, net = trade_returns(pb.open[e, a], pb.close[x, a])
        self.assertEqual((f, l), (e, x))
        self.assertAlmostEqual(np.prod(1 + r) - 1, net, places=12)

    def test_portfolio_matches_naive_loop(self):
        rng = np.random.default_rng(3)
        n, S, k = 120, 6, 40
        C = np.cumprod(1 + rng.normal(0, 0.02, (n, S)), axis=0) * 50
        O = C * (1 + rng.normal(0, 0.005, (n, S)))
        col = rng.integers(0, S, k)
        e = rng.integers(0, 100, k)
        x = np.minimum(e + rng.integers(0, 30, k), n - 1)
        kind_open = rng.random(k) < 0.5
        entry_px = O[e, col]
        exit_px = np.where(kind_open, O[x, col], C[x, col])
        exit_px[x == e] = entry_px[x == e]  # same-day exits are round trips at the entry open
        w = rng.uniform(1, 100, k)
        f1, l1, r1 = portfolio_daily_returns(C, col, e, x, entry_px, exit_px, w)
        f2, l2, r2 = naive_portfolio_daily_returns(C, col, e, x, entry_px, exit_px, w)
        self.assertEqual((f1, l1), (f2, l2))
        np.testing.assert_allclose(r1, r2, rtol=1e-10, atol=1e-12)

    def test_cash_days_earn_zero(self):
        pb = linear_book()
        a = pb.col["AAA"]
        Cz = np.nan_to_num(pb.close_ff, nan=0.0)
        e = np.array([10, 50]); x = np.array([20, 60])
        f, l, r = portfolio_daily_returns(Cz, np.array([a, a]), e, x, pb.open[e, a], pb.close[x, a], np.ones(2))
        self.assertTrue(np.all(r[21 - f:50 - f] == 0.0))

    def test_metrics(self):
        dates = CAL.values[:3].astype("datetime64[D]")
        m = series_metrics(dates, np.array([0.1, -0.5, 0.2]))
        self.assertAlmostEqual(m["total_return"], 1.1 * 0.5 * 1.2 - 1)
        self.assertAlmostEqual(m["max_drawdown"], -0.5)

    def test_amount_weight(self):
        self.assertEqual(amount_weight(1001.0, 15000.0, ""), 8000.5)
        self.assertEqual(amount_weight(50000001.0, float("nan"), "open_over"), 50000001.0)
        self.assertEqual(amount_weight(439.99, 439.99, "exact"), 439.99)


class TestSampleSplit(unittest.TestCase):
    def test_split_on_filing_date(self):
        self.assertEqual(sample_of("2016-01-04"), "in_sample")
        self.assertEqual(sample_of("2021-12-31"), "in_sample")
        self.assertEqual(sample_of("2022-01-01"), "out_of_sample")
        self.assertEqual(sample_of("2026-11-09"), "out_of_sample")

    def test_selection_sees_only_selection_sample(self):
        rows = pd.DataFrame({"sample": [config.SELECTION_SAMPLE, "out_of_sample"], "exit_rule": ["hold_3m"] * 2,
                             "weighting": ["equal"] * 2, "X": ["0", "0"], "n_trades": [500, 500], "sharpe": [1.0, 9.0]})
        with self.assertRaises(ValueError):
            select_configuration(rows)
        ok = rows.iloc[:1].copy()
        self.assertEqual(select_configuration(ok)["X"], "0")

    def test_selection_picks_highest_sharpe_with_min_trades(self):
        rows = pd.DataFrame({"sample": [config.SELECTION_SAMPLE] * 3, "exit_rule": ["hold_3m"] * 3,
                             "weighting": ["equal"] * 3, "X": ["0", "2", "4"], "n_trades": [500, 10, 500],
                             "sharpe": [0.5, 3.0, 0.7]})
        self.assertEqual(select_configuration(rows)["X"], "4")


# ============================================================================
# Round 2 tests (one class per review finding)
# ============================================================================
TRADE_COLS = ["year", "doc_id", "member", "state_dst", "owner", "asset", "ticker", "asset_type", "asset_type_source",
              "type", "transaction_date", "notification_date", "filing_date", "amount_low", "amount_high", "amount_flag",
              "description", "sub_holding", "location", "comments", "filing_status", "option_like", "short_related",
              "date_suspect", "duplicate_of", "deleted_by", "include_v1", "include_reason", "template", "parse_source"]


def make_trades(rows):
    """Synthetic trades.csv-like frame; each row is a dict of overrides."""
    base = {"year": 2020, "doc_id": 1, "member": "Jane Doe", "state_dst": "CA01", "owner": "self", "asset": "x",
            "ticker": "AAA", "asset_type": "ST", "asset_type_source": "code", "type": "buy",
            "transaction_date": d(20), "notification_date": d(20), "filing_date": d(30), "amount_low": 1001.0,
            "amount_high": 15000.0, "amount_flag": np.nan, "description": np.nan, "sub_holding": np.nan,
            "location": np.nan, "comments": np.nan, "filing_status": "New", "option_like": False, "short_related": False,
            "date_suspect": np.nan, "duplicate_of": np.nan, "deleted_by": np.nan, "include_v1": True,
            "include_reason": "kode ST", "template": "t", "parse_source": "layout"}
    t = pd.DataFrame([{**base, **r} for r in rows], columns=TRADE_COLS)
    t["row_id"] = t.index
    return t


class TestF2ReferenceMissingDroppedEverywhere(unittest.TestCase):
    def test_ref_missing_signal_is_dropped_for_every_x_including_none(self):
        from src.backtest.run import Evaluator, build_signal_table
        pb = linear_book()
        tr = add_canonical_member(make_trades([
            {"transaction_date": "2019-06-03", "filing_date": d(30)},   # before price history -> no reference
            {"transaction_date": d(20), "filing_date": d(30)},
        ]))
        tmap = pd.DataFrame({"filing_ticker": ["AAA"], "price_symbol": ["AAA"]})
        sig = build_signal_table(tr, tmap, pb)
        self.assertTrue(sig.loc[0, "drop_reason"].startswith("ref_missing"))
        self.assertEqual(sig.loc[1, "drop_reason"], "")
        ev = Evaluator(sig, pb)
        for x in config.X_GRID:
            row = ev.evaluate("in_sample_full_hold", "hold_3m", "equal", x)["row"]
            # the same universe for every X: only the priced signal can be traded or skipped
            self.assertEqual(row["n_trades"] + row["n_skips"], 1, x)
            self.assertEqual(row["n_dropped_ref_missing"], 1)
        self.assertEqual(ev.evaluate("in_sample_full_hold", "hold_3m", "equal", None)["row"]["n_trades"], 1)


class TestF3NonDiscretionaryAndCleaning(unittest.TestCase):
    def test_keyword_categories(self):
        cases = {"Dividend Reinvestment": "dividend_reinvestment", "AUTOMATIC REINVESTMENT OF DIVIDENDS": "dividend_reinvestment",
                 "Transfer in of vested stock.": "vesting_compensation", "Employee stock plan distribution": "vesting_compensation",
                 "Gift from parent": "gift_inheritance", "Inherited shares": "gift_inheritance",
                 "Part of quarterly portfolio rebalancing conducted by asset manager": "third_party_managed",
                 "Shares acquired through a spinoff of LBTYA": "transfer_distribution_corporate_action",
                 "periodic purchase": "automatic_plan"}
        for txt, cat in cases.items():
            self.assertEqual(nondiscretionary_category(txt, np.nan), cat, txt)
        for txt in ("Bought 100 shares", "SUBSEQUENT INVESTMENT", "", "unsolicited trade", "New purchase"):
            self.assertEqual(nondiscretionary_category(txt, np.nan), "", txt)
        self.assertEqual(nondiscretionary_category(np.nan, "Reinvest shares"), "dividend_reinvestment")

    def test_select_buy_signals_drop_reasons(self):
        tr = make_trades([
            {"doc_id": 1},                                              # kept
            {"doc_id": 1},                                              # identical row -> exact duplicate
            {"doc_id": 1, "sub_holding": "IRA"},                        # other account, same doc -> kept
            {"doc_id": 2, "filing_date": d(40)},                        # same trade re-filed later -> cross-doc dup
            {"doc_id": 3, "ticker": "BBB", "description": "Dividend reinvestment"},
            {"doc_id": 3, "ticker": "CCC", "short_related": True},
        ])
        b = select_buy_signals(tr)
        self.assertEqual(list(b["drop_reason"]), ["", "exact_duplicate_row", "", "cross_doc_duplicate",
                                                  "nondiscretionary_dividend_reinvestment", "short_related_excluded"])

    def test_member_name_normalisation(self):
        self.assertEqual(member_key("Mark Dr Green", "TN07"), member_key("Mark Green", "TN07"))
        self.assertEqual(member_key("Neal Patrick MD, FACS Dunn", "FL02"), member_key("Neal P. Dunn", "FL02"))
        self.assertNotEqual(member_key("Mark Green", "TN07"), member_key("Mark Green", "OH01"))  # different person
        tr = add_canonical_member(make_trades([
            {"member": "Mark Green", "state_dst": "TN07"},
            {"member": "Mark Dr Green", "state_dst": "TN07", "type": "sell", "transaction_date": d(40), "filing_date": d(50)},
        ]))
        s = select_sales(tr)
        self.assertEqual(s["member_canon"].iloc[0], tr["member_canon"].iloc[0])  # the sale matches the buy


class TestF4Significance(unittest.TestCase):
    def test_alpha_beta_recovered(self):
        rng = np.random.default_rng(5)
        rb = rng.normal(0.0004, 0.01, 2000)
        r = 0.0002 + 1.5 * rb + rng.normal(0, 0.002, 2000)
        out = significance(r, rb)
        self.assertAlmostEqual(out["beta"], 1.5, delta=0.02)
        self.assertAlmostEqual(out["alpha_daily"], 0.0002, delta=0.0001)
        self.assertGreater(out["alpha_t_nw"], 2)
        ex = r - rb
        self.assertAlmostEqual(out["information_ratio"], ex.mean() / ex.std(ddof=1) * np.sqrt(252), places=10)

    def test_newey_west_lag0_is_white(self):
        rng = np.random.default_rng(6)
        X = np.column_stack([np.ones(300), rng.normal(size=300)])
        y = X @ [1.0, 2.0] + rng.normal(size=300)
        b, se, u, _ = newey_west_ols(y, X, lags=0)
        XtXi = np.linalg.inv(X.T @ X)
        white = np.sqrt(np.diag(XtXi @ (X.T * u ** 2) @ X @ XtXi))
        np.testing.assert_allclose(se, white, rtol=1e-10)

    def test_yearly_table(self):
        dates = pd.to_datetime(["2021-12-30", "2021-12-31", "2022-01-03"]).values
        y = yearly_table(dates, np.array([0.1, 0.1, -0.5]), np.array([0.0, 0.0, 0.0]))
        self.assertAlmostEqual(y.loc[y.year == 2021, "strategy_return"].iloc[0], 0.21)
        self.assertEqual(list(y["period"]), ["in_sample_years", "out_of_sample_years"])


class TestF1ContinuousBook(unittest.TestCase):
    def test_in_sample_positions_carry_into_oos_window(self):
        from src.backtest.run import Evaluator, build_signal_table
        cal = pd.bdate_range("2021-06-01", "2022-12-30")
        t = np.arange(len(cal))
        pb = PriceBook(cal.values, {"SPY": frame(cal, 300 + t, 300.5 + t), "AAA": frame(cal, 100.0 + t, 100.5 + t),
                                    "BBB": frame(cal, 50.0 + 0 * t, 50.0 + 0 * t)})
        dd = lambda i: cal[i].strftime("%Y-%m-%d")
        tr = add_canonical_member(make_trades([
            {"ticker": "AAA", "transaction_date": "2021-11-01", "filing_date": "2021-11-15"},   # in-sample, held 12m
            {"ticker": "BBB", "transaction_date": "2022-03-01", "filing_date": "2022-03-10"},   # OOS signal
        ]))
        tmap = pd.DataFrame({"filing_ticker": ["AAA", "BBB"], "price_symbol": ["AAA", "BBB"]})
        sig = build_signal_table(tr, tmap, pb)
        ev = Evaluator(sig, pb)
        prim = ev.evaluate("oos_continuous", "hold_12m", "equal", None)
        sec = ev.evaluate("oos_signals_only", "hold_12m", "equal", None)
        self.assertEqual(prim["row"]["period_start"], "2022-01-03")
        self.assertEqual(prim["row"]["n_trades_active_in_window"], 2)
        self.assertEqual(sec["row"]["n_trades"], 1)
        # the continuous book earns AAA's drift in Jan 2022 (only AAA is held), the OOS-only book is empty then
        self.assertGreater(prim["r"][0], 0)
        self.assertEqual(sec["row"]["period_start"], dd(int(np.flatnonzero(cal == pd.Timestamp("2022-03-11"))[0])))
        # window = slice of the full continuous series
        full = ev.evaluate("all_continuous", "hold_12m", "equal", None)
        k = len(full["r"]) - len(prim["r"])
        np.testing.assert_allclose(full["r"][k:], prim["r"])
        np.testing.assert_allclose(full["br"][k:], prim["br"])


class TestF5JumpFlags(unittest.TestCase):
    def test_jump15_catches_17_8_pct_but_100_flag_does_not(self):
        t = np.arange(len(CAL))
        c = 10.0 + 0 * t
        c[200:] *= 1.178
        pb = linear_book({"DD": frame(CAL, c, c)})
        j = pb.col["DD"]
        j15 = pb.jump_cum(config.JUMP15_UP, config.JUMP15_DOWN)
        self.assertEqual(j15[201, j] - j15[150, j], 1)          # window (150, 200] contains the jump
        self.assertEqual(pb.extreme_cum[201, j] - pb.extreme_cum[150, j], 0)
        self.assertEqual(j15[199, j] - j15[100, j], 0)          # window ending before the jump


class TestF6GapExits(unittest.TestCase):
    def test_continuous_halt_stays_one_piece_and_exit_waits_for_first_bar_after_gap(self):
        t = np.arange(len(CAL))
        v = np.ones(len(CAL)) * 1000
        v[100:130] = 0   # 30-day halt, price continuous on resumption
        pb = linear_book({"HLT": frame(CAL, 10.0 + t, 10.5 + t, v)})
        h = pb.col["HLT"]
        self.assertEqual(pb.n_segments[h], 1)
        # planned 3m exit from entry 40 falls inside the halt -> first valid bar after it, normal horizon exit
        e = 40
        target = pd.Timestamp(CAL[e]) + pd.DateOffset(months=3)
        p = int(np.searchsorted(pb.cal, np.datetime64(target.date()), "left"))
        self.assertTrue(100 <= p < 130)
        ex = exit_fixed_hold(pb, h, e, 3)
        self.assertEqual((ex.idx, ex.reason, ex.planned_idx), (130, "horizon", p))
        # the exit day does not depend on how long the halt will last: same rule, the
        # position is still open on every halted day
        self.assertTrue(np.all(pb.seg[100:130, h] == 0))

    def test_discontinuous_resumption_is_a_new_piece(self):
        t = np.arange(len(CAL))
        v = np.ones(len(CAL)) * 1000
        v[100:130] = 0
        px = np.where(t >= 130, 5.0, 1.0) * (10.0 + t)   # resumes at 5x the price: different security
        pb = linear_book({"RU": frame(CAL, px, px + 0.5, v)})
        j = pb.col["RU"]
        self.assertEqual(pb.n_segments[j], 2)
        ex = exit_fixed_hold(pb, j, 40, 3)
        self.assertEqual((ex.idx, ex.reason), (99, "delisted"))
        self.assertEqual(ex.price, pb.close[99, j])

    def test_long_gap_beyond_quarter_is_a_new_piece(self):
        t = np.arange(len(CAL))
        v = np.ones(len(CAL)) * 1000
        v[100:100 + config.PIECE_MAX_GAP + 5] = 0
        pb = linear_book({"LG": frame(CAL, 10.0 + t, 10.5 + t, v)})
        self.assertEqual(pb.n_segments[pb.col["LG"]], 2)


if __name__ == "__main__":
    unittest.main()
