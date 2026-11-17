"""Unit tests for src/backtest/member_selection.py.
Run:  python3 -m unittest tests.test_member_selection

Synthetic data only (no files under data/ are read).
"""
import unittest

import numpy as np
import pandas as pd

from src.backtest import ms_config as mc
from src.backtest.member_selection import (decide, deflated_sr, expected_max_sr, member_codes_of, score_panel,
                                           select_ms_configuration, shrunk_score, signal_decisions, top_cutoff,
                                           track_record_table, window_start_idx)
from src.backtest.prices import PriceBook
from src.backtest.run import build_signal_table

CAL = pd.bdate_range("2020-01-02", periods=520)
CALD = CAL.values.astype("datetime64[D]")
SYMS = ["SPY", "AAA", "BBB", "CCC", "DDD"]


def make_frames(seed=1):
    rng = np.random.default_rng(seed)
    frames = {}
    for s in SYMS:
        r = rng.normal(0.0004, 0.015, len(CAL))
        c = 100 * np.cumprod(1 + r)
        o = c * (1 + rng.normal(0, 0.004, len(CAL)))
        frames[s] = pd.DataFrame({"date": CAL.strftime("%Y-%m-%d"), "open": o, "high": np.maximum(o, c),
                                  "low": np.minimum(o, c), "close": c, "volume": 1000})
    return frames


def perturb(frames, from_idx, lo=None, seed=99):
    """Multiply every price of every symbol on days >= from_idx (or < lo if given) by random factors."""
    rng = np.random.default_rng(seed)
    out = {}
    for s, f in frames.items():
        f = f.copy()
        idx = np.arange(len(f))
        m = idx >= from_idx if lo is None else idx < lo
        fac = rng.uniform(0.7, 1.4, m.sum())
        for c in ("open", "high", "low", "close"):
            f.loc[m, c] = f.loc[m, c].to_numpy() * fac
        out[s] = f
    return out


def make_trades():
    """Three members buying every few days; filing 3 business days after the transaction."""
    rows = []
    plan = [("Alice Able", "CA01", ["AAA", "BBB"], 4), ("Bob Baker", "TX02", ["CCC"], 6),
            ("Carl Cole", "NY03", ["DDD", "AAA"], 9)]
    rid = 0
    for member, st, tick, step in plan:
        for k, i in enumerate(range(5, 470, step)):
            rows.append({"member": member, "state_dst": st, "ticker": tick[k % len(tick)],
                         "transaction_date": CAL[i - 3].strftime("%Y-%m-%d"), "filing_date": CAL[i].strftime("%Y-%m-%d"),
                         "type": "buy", "include_v1": True, "short_related": False, "option_like": False,
                         "owner": "self", "amount_low": 1001.0, "amount_high": 15000.0 + k, "amount_flag": "",
                         "doc_id": f"D{rid}", "description": np.nan, "comments": np.nan, "year": int(CAL[i].year),
                         "asset_type": "stock", "filing_status": "new"})
            rid += 1
    t = pd.DataFrame(rows)
    t["row_id"] = t.index
    tmap = pd.DataFrame({"filing_ticker": SYMS[1:], "price_symbol": SYMS[1:]})
    return t, tmap


def build(frames):
    pb = PriceBook(CALD, frames)
    trades, tmap = make_trades()
    sig = build_signal_table(trades, tmap, pb)
    return pb, sig


def tr_frame(rows):
    """rows: (member, entry_idx, exit_idx, known_idx, excess)"""
    df = pd.DataFrame(rows, columns=["member_canon", "entry_idx", "exit_idx", "known_idx", "excess"])
    df["row_id"] = range(len(df))
    df["exit_reason"] = "horizon"
    return df


class TestShrinkage(unittest.TestCase):
    def test_formula(self):
        s = np.array([0.5, 0.5, -0.3, 0.0])
        n = np.array([5, 5, 30, 0])
        got = shrunk_score(s, n, 10)
        self.assertAlmostEqual(got[0], (0.5 / 5) * 5 / (5 + 10))
        self.assertAlmostEqual(got[2], (-0.3 / 30) * 30 / 40)
        self.assertTrue(np.isnan(got[3]))
        self.assertAlmostEqual(shrunk_score(s, n, 0)[0], 0.1)          # k=0 -> plain mean
        self.assertAlmostEqual(shrunk_score(s, n, 30)[0], 0.1 * 5 / 35)

    def test_shrinkage_never_changes_sign(self):
        rng = np.random.default_rng(0)
        s, n = rng.normal(size=200), rng.integers(1, 50, 200)
        for k in (0, 10, 30):
            self.assertTrue(np.array_equal(np.sign(shrunk_score(s, n, k)), np.sign(s)))


class TestEligibilityAndRanking(unittest.TestCase):
    def test_K_eligibility(self):
        K = 5
        n = np.array([[K - 1, K, K + 3]], float)
        s = np.array([[1.0, 0.1, 0.2]])          # member 0 has the best mean but too few trades
        d = decide(n, s, K, "score_pos", 10)
        self.assertEqual(d["eligible"].tolist(), [[False, True, True]])
        self.assertEqual(d["selected"].tolist(), [[False, True, True]])
        self.assertEqual(int(d["n_eligible"][0]), 2)
        d = decide(n, s, K, "top20", 10)       # N=2 -> cutoff 1 -> only the best eligible
        self.assertEqual(d["selected"].tolist(), [[False, False, True]])

    def test_score_pos(self):
        n = np.array([[10, 10, 10]], float)
        s = np.array([[0.5, 0.0, -0.5]])
        self.assertEqual(decide(n, s, 5, "score_pos", 10)["selected"].tolist(), [[True, False, False]])

    def test_top20_cutoff_integer_arithmetic(self):
        self.assertEqual(top_cutoff([1, 4, 5, 6, 10, 11, 15, 20, 0]).tolist(), [1, 1, 1, 2, 2, 3, 3, 4, 0])

    def test_top20_ranking_at_time_t(self):
        # 15 eligible members (cutoff 3, a float trap: 0.2*15 = 3.0000000000000004), 2 ineligible
        rng = np.random.default_rng(3)
        M = 17
        n = np.full((2, M), 20.0)
        n[:, 15:] = 2                                        # ineligible (K=5)
        s = np.vstack([rng.permutation(np.arange(M, dtype=float)), rng.permutation(np.arange(M, dtype=float))])
        s[:, 15:] = 1000.0                                   # huge scores but ineligible: must not count
        d = decide(n, s, 5, "top20", 10)
        for k in range(2):
            self.assertEqual(int(d["n_eligible"][k]), 15)
            self.assertEqual(int(d["selected"][k].sum()), 3)
            best3 = set(np.argsort(-s[k, :15])[:3])
            self.assertEqual(set(np.flatnonzero(d["selected"][k])), best3)
        # the selected set differs between the two days -> ranking is done per day t
        self.assertNotEqual(set(np.flatnonzero(d["selected"][0])), set(np.flatnonzero(d["selected"][1])))

    def test_top20_ties_share_rank(self):
        n = np.full((1, 5), 10.0)
        s = np.array([[1.0, 1.0, 0.5, 0.2, 0.1]])
        d = decide(n, s, 5, "top20", 10)                   # cutoff 1, two tied at the top
        self.assertEqual(d["selected"].tolist(), [[True, True, False, False, False]])
        self.assertEqual(d["rank"][0, :3].tolist(), [1, 1, 3])

    def test_top20_can_select_negative_scores(self):
        n = np.full((1, 5), 10.0)
        s = np.array([[-0.1, -0.2, -0.3, -0.4, -0.5]])
        self.assertEqual(decide(n, s, 5, "top20", 10)["selected"].tolist(), [[True, False, False, False, False]])


class TestTrackRecordWindow(unittest.TestCase):
    def setUp(self):
        self.codes = {"A": 0, "B": 1}

    def test_only_results_known_before_t(self):
        t = 100
        tr = tr_frame([("A", 10, t - 1, t - 1, 0.10),    # exit the day before entry -> counts
                       ("A", 20, t, t, 0.50),            # exit on the entry day (close) -> NOT known at the open
                       ("A", 30, t + 5, t + 5, 0.70),    # later
                       ("B", 40, 90, 90 + mc.DELISTED_KNOWN_LAG, 0.30)])  # delisted, not yet certain
        n, s = score_panel(tr, [t], CALD, None, self.codes)
        self.assertEqual(n[0].tolist(), [1, 0])
        self.assertAlmostEqual(s[0, 0], 0.10)
        n2, _ = score_panel(tr, [90 + mc.DELISTED_KNOWN_LAG + 1], CALD, None, self.codes)
        self.assertEqual(n2[0, 1], 1)

    def test_trailing_window_on_exit_date(self):
        # W = 12 months so the window start lies inside the synthetic calendar (the grid uses 24)
        t = 515
        start = np.datetime64((pd.Timestamp(CALD[t]) - pd.DateOffset(months=12)).date())
        w0 = int(window_start_idx(CALD, [t], 12)[0])
        self.assertGreater(w0, 0)
        self.assertGreaterEqual(CALD[w0], start)
        self.assertLess(CALD[w0 - 1], start)
        tr = tr_frame([("A", 0, w0 - 1, w0 - 1, 1.0), ("A", 0, w0, w0, 0.2), ("A", 300, 400, 400, 0.4)])
        n, s = score_panel(tr, [t], CALD, 12, self.codes)
        self.assertEqual(n[0, 0], 2)
        self.assertAlmostEqual(s[0, 0], 0.6)
        n, s = score_panel(tr, [t], CALD, None, self.codes)     # all history
        self.assertEqual(n[0, 0], 3)
        self.assertEqual(int(window_start_idx(CALD, [t], None)[0]), 0)

    def test_track_record_table_rules(self):
        sig = pd.DataFrame({"row_id": [1, 2, 3, 4], "member_canon": ["A"] * 4, "drop_reason": ["", "", "", "dup"],
                            "entry_idx": [10, 20, 30, 40], "hold_6m_exit_idx": [50, 60, 70, 80],
                            "hold_6m_exit_reason": ["horizon", "delisted", "open_at_end", "horizon"],
                            "hold_6m_net_ret": [0.1, -0.2, 0.3, 0.4], "hold_6m_spy_ret": [0.05, 0.0, 0.0, 0.0]})
        tr = track_record_table(sig)
        self.assertEqual(tr["row_id"].tolist(), [1, 2])                  # open_at_end and dropped rows excluded
        self.assertEqual(tr["known_idx"].tolist(), [50, 60 + mc.DELISTED_KNOWN_LAG])
        self.assertTrue(np.allclose(tr["excess"], [0.05, -0.2]))         # net - SPY, after costs


class TestNoLookAheadEndToEnd(unittest.TestCase):
    """Full pipeline: synthetic prices -> v1 signal table -> track record -> decision."""

    @classmethod
    def setUpClass(cls):
        cls.frames = make_frames()
        cls.pb, cls.sig = build(cls.frames)
        cls.codes = member_codes_of(cls.sig)

    def decisions(self, sig, W, K, rule):
        return signal_decisions(sig, track_record_table(sig), CALD, W, K, rule, mc.K_SHRINK, self.codes).set_index("row_id")

    def test_perturbing_later_prices_does_not_change_decision(self):
        el = self.sig[self.sig["drop_reason"] == ""]
        self.assertGreater(len(el), 150)
        for t in (300, 380, 450):
            pb2, sig2 = build(perturb(self.frames, t))
            for W in (24, None):
                for K in (5, 15):
                    for rule in ("score_pos", "top20"):
                        a = self.decisions(self.sig, W, K, rule)
                        b = self.decisions(sig2, W, K, rule)
                        ids = a.index[a["entry_idx"] <= t]   # every signal entering on/before t
                        self.assertGreater(len(ids), 50)
                        for c in ("selected", "eligible", "n_track", "n_eligible_members"):
                            self.assertTrue((a.loc[ids, c].to_numpy() == b.loc[ids, c].to_numpy()).all(), (t, W, K, rule, c))
                        self.assertTrue(np.allclose(a.loc[ids, "score"], b.loc[ids, "score"], equal_nan=True))
            # sanity: the perturbation does change returns of trades entering after t
            a6 = self.sig.set_index("row_id")["hold_6m_net_ret"]
            b6 = sig2.set_index("row_id")["hold_6m_net_ret"]
            late = self.sig.loc[(self.sig["drop_reason"] == "") & (self.sig["entry_idx"] > t), "row_id"]
            self.assertFalse(np.allclose(a6[late], b6[late]))

    def test_perturbing_earlier_prices_does_change_scores(self):
        """Guard against a vacuous test: the track record does react to pre-t prices."""
        pb2, sig2 = build(perturb(self.frames, 0, lo=200))
        a = self.decisions(self.sig, None, 5, "score_pos")
        b = self.decisions(sig2, None, 5, "score_pos")
        ids = a.index[(a["entry_idx"] > 350) & (a["n_track"] > 0)]
        self.assertFalse(np.allclose(a.loc[ids, "score"], b.loc[ids, "score"]))

    def test_track_record_trades_ended_before_entry(self):
        tr = track_record_table(self.sig)
        d = self.decisions(self.sig, None, 1, "score_pos")
        for rid in d.index[::17]:
            t = d.loc[rid, "entry_idx"]
            m = d.loc[rid, "member_canon"]
            own = tr[(tr["member_canon"] == m) & (tr["known_idx"] < t)]
            self.assertEqual(d.loc[rid, "n_track"], len(own))
            self.assertTrue((own["exit_idx"] < t).all())
            if len(own):
                self.assertAlmostEqual(d.loc[rid, "mean_excess_track"], own["excess"].mean())
            # nothing filed on/after the entry day can be in the record
            fd = self.sig.set_index("row_id").loc[own["row_id"], "filing_date"]
            self.assertTrue((fd < str(CALD[t])).all())


class TestNoOOSInformationInSelection(unittest.TestCase):
    def test_in_sample_decisions_ignore_later_results(self):
        rng = np.random.default_rng(5)
        rows = []
        for m in "ABCDEFG":
            for i in range(60):
                e = int(rng.integers(0, 480))
                rows.append((m, e, e + 126, e + 126, float(rng.normal(0, 0.1))))
        tr = tr_frame(rows)
        codes = {m: i for i, m in enumerate("ABCDEFG")}
        cut = 300
        t_is = np.arange(130, cut + 1, 7)
        tr2 = tr.copy()
        late = tr2["known_idx"] >= cut
        tr2.loc[late, "excess"] = rng.normal(5, 3, late.sum())   # wildly different "OOS" results
        tr3 = tr[~late]                                             # or removed altogether
        for W in (24, None):
            p1 = score_panel(tr, t_is, CALD, W, codes)
            for other in (tr2, tr3):
                p2 = score_panel(other, t_is, CALD, W, codes)
                for K in (5, 15):
                    for rule in ("score_pos", "top20"):
                        self.assertTrue(np.array_equal(decide(*p1, K, rule, 10)["selected"],
                                                       decide(*p2, K, rule, 10)["selected"]))

    def grid(self, sample="in_sample_cut2021"):
        rows = []
        for W in ("24m", "all"):
            for K in (5, 15):
                for sel in ("score_pos", "top20"):
                    for ex in mc.EXIT_RULES:
                        for wt in mc.WEIGHTINGS:
                            for X in ("10", "none"):
                                rows.append({"sample": sample, "W": W, "K": K, "selection": sel, "exit_rule": ex,
                                             "weighting": wt, "X": X, "sharpe": 0.5, "n_trades": 500,
                                             "config": f"{W}/{K}/{sel}/{ex}/{wt}/{X}"})
        return pd.DataFrame(rows)

    def test_selection_refuses_oos_rows(self):
        g = pd.concat([self.grid(), self.grid("oos_continuous")])
        with self.assertRaises(ValueError):
            select_ms_configuration(g)
        with self.assertRaises(ValueError):
            select_ms_configuration(self.grid("oos_continuous"))

    def test_selection_criterion(self):
        g = self.grid()
        self.assertEqual(len(g), 128)
        # all tied -> first in grid order
        c = select_ms_configuration(g)
        self.assertEqual(c["config"], "24m/5/score_pos/member_sells/equal/10")
        # best Sharpe with < 200 trades is ineligible
        g.loc[5, ["sharpe", "n_trades"]] = [3.0, 199]
        g.loc[77, "sharpe"] = 2.0
        self.assertEqual(select_ms_configuration(g)["config"], g.loc[77, "config"])


class TestMultipleTesting(unittest.TestCase):
    def test_expected_max_sr(self):
        self.assertEqual(expected_max_sr(0.01, 1), 0.0)
        self.assertGreater(expected_max_sr(0.01, 128), expected_max_sr(0.01, 8))
        # Bailey & Lopez de Prado (2014) numerical example: V=1/2 ... E[max] ~ grows like sqrt(2 ln N)
        self.assertAlmostEqual(expected_max_sr(1.0, 1000), 3.2551, places=2)

    def test_dsr_noise_is_not_significant(self):
        rng = np.random.default_rng(11)
        trials = rng.normal(0, 0.01, (128, 1000))
        srs = trials.mean(1) / trials.std(1, ddof=1)
        best = trials[np.argmax(srs)]
        self.assertLess(deflated_sr(best, srs, 128)["dsr"], 0.95)


if __name__ == "__main__":
    unittest.main()
