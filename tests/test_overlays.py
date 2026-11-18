"""Unit tests for src/backtest/overlays.py (risk overlays on the locked v1 strategy).
Run:  python3 -m unittest tests.test_overlays

Synthetic data only (no files under data/ are read).
"""
import unittest

import numpy as np
import pandas as pd

from src.backtest import config
from src.backtest import overlay_config as oc
from src.backtest.overlays import (apply_cap, apply_vt, member_cap_weights, notional, position_panel, run_book,
                                   select_overlay, select_risk_reduction, vol_target, water_fill)
from src.backtest.portfolio import naive_portfolio_daily_returns, portfolio_daily_returns


def synthetic_book(seed=3, n_days=400, n_sym=12, n_tr=150, big=None):
    """Random closes and trades; optionally one huge trade (index `big`)."""
    rng = np.random.default_rng(seed)
    Cz = 50 * np.cumprod(1 + rng.normal(0.0003, 0.02, (n_days, n_sym)), axis=0)
    col = rng.integers(0, n_sym, n_tr)
    e = rng.integers(1, n_days - 130, n_tr)
    x = e + rng.integers(0, 126, n_tr)
    entry_px = Cz[e - 1, col] * (1 + rng.normal(0, 0.005, n_tr))
    exit_px = Cz[x, col]
    amt = rng.choice([8000.5, 32500.5, 75000.5, 175000.5], n_tr)
    if big is not None:
        amt[big] = 15_000_000.0
    member = rng.integers(0, 9, n_tr)
    return Cz, col, e, x, entry_px, exit_px, amt, member


class TestWeighting(unittest.TestCase):
    def test_sqrt_amount(self):
        a = np.array([1001.0, 8000.5, 15000.0, 15_000_000.0])
        np.testing.assert_allclose(notional(a, "sqrt_amount"), np.sqrt(a))
        np.testing.assert_allclose(notional(a, "amount"), a)
        np.testing.assert_allclose(notional(a, "equal"), np.ones(4))
        # sqrt compresses the ratio of the largest to the smallest trade
        self.assertAlmostEqual(notional(a, "sqrt_amount")[3] / notional(a, "sqrt_amount")[0],
                               np.sqrt(a[3] / a[0]))

    def test_sqrt_amount_book_uses_sqrt_notional(self):
        Cz, col, e, x, ep, xp, amt, _ = synthetic_book()
        p = position_panel(Cz, col, e, x, ep, xp, notional(amt, "sqrt_amount"))
        f, l, r = portfolio_daily_returns(Cz, col, e, x, ep, xp, np.sqrt(amt))
        b = run_book(p, np.zeros(len(e), int), "none", with_conc=False)
        np.testing.assert_allclose(b["r"], r, atol=1e-13)

    def test_unknown_weighting(self):
        with self.assertRaises(ValueError):
            notional(np.ones(2), "log_amount")


class TestCaps(unittest.TestCase):
    def test_water_fill_caps_and_sums_to_one(self):
        rng = np.random.default_rng(1)
        for _ in range(200):
            n = rng.integers(21, 300)
            w = rng.pareto(1.2, n) + 1e-6
            w /= w.sum()
            c = water_fill(w, 0.05)
            self.assertLessEqual(c.max(), 0.05 + 1e-12)
            self.assertAlmostEqual(c.sum(), 1.0, places=12)          # n >= 20: everything placed
            self.assertTrue((c >= 0).all())

    def test_water_fill_redistributes_pro_rata(self):
        w = np.array([0.5, 0.1, 0.1, 0.2, 0.1])
        c = water_fill(w, 0.25)
        # 0.5 -> 0.25; 0.25 excess pro rata (0.1:0.1:0.2:0.1) -> 0.15, 0.15, 0.30, 0.15;
        # 0.30 -> 0.25, its 0.05 excess to the three 0.15 -> 1/6 each
        np.testing.assert_allclose(c, [0.25, 1 / 6, 1 / 6, 0.25, 1 / 6], atol=1e-12)
        # uncapped ratios preserved
        self.assertAlmostEqual(c[1] / c[2], 1.0)
        self.assertAlmostEqual(c.sum(), 1.0)

    def test_water_fill_iterates_when_redistribution_pushes_over_cap(self):
        w = np.array([0.6, 0.2, 0.1, 0.05, 0.05])
        c = water_fill(w, 0.3)
        self.assertLessEqual(c.max(), 0.3 + 1e-12)
        self.assertAlmostEqual(c.sum(), 1.0, places=12)
        np.testing.assert_allclose(c[:2], [0.3, 0.3], atol=1e-12)    # 0.2 is pushed over and capped too
        np.testing.assert_allclose(c[2:], np.array([0.1, 0.05, 0.05]) * 0.4 / 0.2, atol=1e-12)

    def test_too_few_positions_remainder_is_cash(self):
        w = np.array([0.5, 0.3, 0.2])
        c = water_fill(w, 0.05)
        np.testing.assert_allclose(c, [0.05, 0.05, 0.05])
        self.assertAlmostEqual(c.sum(), 0.15)                        # 85 % cash

    def test_no_cap_binding_is_identity(self):
        w = np.full(40, 1 / 40)
        np.testing.assert_array_equal(water_fill(w, 0.05), w)

    def test_member_cap(self):
        rng = np.random.default_rng(5)
        for _ in range(100):
            n = rng.integers(10, 200)
            w = rng.pareto(1.0, n) + 1e-6
            w /= w.sum()
            m = rng.integers(0, 12, n)
            c = member_cap_weights(w, m, 0.15)
            W = pd.Series(c).groupby(m).sum()
            self.assertLessEqual(W.max(), 0.15 + 1e-12)
            self.assertLessEqual(c.sum(), 1.0 + 1e-12)
            if len(np.unique(m)) >= 7:
                self.assertAlmostEqual(c.sum(), 1.0, places=10)
            # positions inside a member keep their relative sizes
            for mm in np.unique(m):
                k = m == mm
                if k.sum() > 1:
                    np.testing.assert_allclose(c[k] / c[k].sum(), w[k] / w[k].sum(), atol=1e-12)

    def test_member_cap_too_few_members_cash(self):
        w = np.array([0.4, 0.4, 0.2])
        m = np.array([0, 0, 1])
        c = member_cap_weights(w, m, 0.15)
        np.testing.assert_allclose([c[:2].sum(), c[2]], [0.15, 0.15])
        np.testing.assert_allclose(c[0], c[1])

    def test_book_daily_weights_capped_and_sum_le_1(self):
        Cz, col, e, x, ep, xp, amt, mem = synthetic_book(big=7)
        p = position_panel(Cz, col, e, x, ep, xp, notional(amt, "amount"))
        for cap, lim in (("position", oc.POSITION_CAP), ("member", oc.MEMBER_CAP)):
            b = run_book(p, mem, cap, keep_weights=True)
            inv = b["invested"]
            self.assertTrue((inv <= 1 + 1e-12).all())
            if cap == "position":
                self.assertLessEqual(b["w_cap"].max(), lim + 1e-12)
                np.testing.assert_allclose(b["top_position_share"].max(), lim, atol=1e-9)
            else:
                ms = pd.DataFrame({"d": b["day"], "m": b["member"], "w": b["w_cap"]}).groupby(["d", "m"])["w"].sum()
                self.assertLessEqual(ms.max(), lim + 1e-12)
            # where enough names are open, all weight is placed (redistributed, not cash)
            npos = b["n_positions"]
            nm = pd.DataFrame({"d": b["day"], "m": b["member"]}).groupby("d")["m"].nunique().reindex(
                range(len(inv)), fill_value=0).to_numpy()
            full = (npos >= 20) if cap == "position" else (nm >= 7)
            np.testing.assert_allclose(inv[full], 1.0, atol=1e-10)
            self.assertTrue((inv[~full & (npos > 0)] <= 1 + 1e-12).all())

    def test_caps_use_only_information_up_to_t(self):
        """Capped weights on day t depend on closes up to t-1 and entries at t only."""
        Cz, col, e, x, ep, xp, amt, mem = synthetic_book(big=3)
        t = 200
        Cz2 = Cz.copy()
        Cz2[t:] *= np.random.default_rng(9).uniform(0.5, 2.0, Cz2[t:].shape)
        xp2 = np.where(x >= t, xp * 1.7, xp)
        for cap in ("position", "member"):
            a = run_book(position_panel(Cz, col, e, x, ep, xp, amt), mem, cap, keep_weights=True)
            b = run_book(position_panel(Cz2, col, e, x, ep, xp2, amt), mem, cap, keep_weights=True)
            k = a["day"] <= t - a["first"]
            np.testing.assert_allclose(a["w_cap"][k], b["w_cap"][k], atol=1e-14)
            self.assertFalse(np.allclose(a["w_cap"][~k], b["w_cap"][~k]))

    def test_no_cap_reproduces_v1_portfolio_exactly(self):
        Cz, col, e, x, ep, xp, amt, mem = synthetic_book()
        for wt in ("equal", "amount"):
            w = notional(amt, wt)
            f, l, r = portfolio_daily_returns(Cz, col, e, x, ep, xp, w)
            fn, ln, rn = naive_portfolio_daily_returns(Cz, col, e, x, ep, xp, w)
            b = run_book(position_panel(Cz, col, e, x, ep, xp, w), mem, "none")
            self.assertEqual((b["first"], b["last"]), (f, l))
            np.testing.assert_allclose(b["r"], r, atol=1e-14)
            np.testing.assert_allclose(b["r"], rn, atol=1e-12)
            self.assertEqual(b["cap_cost"].sum(), 0.0)

    def test_capped_return_is_weighted_position_return(self):
        """Natural weights through the capped code path give the v1 return (cap above every weight)."""
        Cz, col, e, x, ep, xp, amt, mem = synthetic_book()
        p = position_panel(Cz, col, e, x, ep, xp, amt)
        b0 = run_book(p, mem, "none")
        old = oc.POSITION_CAP
        try:
            oc.POSITION_CAP = 1.0
            b1 = run_book(p, mem, "position")
        finally:
            oc.POSITION_CAP = old
        np.testing.assert_allclose(b1["r_gross"], b0["r"], atol=1e-13)
        np.testing.assert_allclose(b1["cap_cost"], 0.0, atol=1e-15)     # no reshuffling -> no cap cost

    def test_cap_cost_charged_on_reshuffling(self):
        Cz, col, e, x, ep, xp, amt, mem = synthetic_book(big=11)
        b = run_book(position_panel(Cz, col, e, x, ep, xp, amt), mem, "position")
        self.assertTrue((b["cap_cost"] >= 0).all())
        self.assertGreater(b["cap_cost"].sum(), 0)
        np.testing.assert_allclose(b["r"], b["r_gross"] - b["cap_cost"])


class TestVolTargeting(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(2)
        self.r = np.r_[rng.normal(0.0005, 0.008, 300), rng.normal(0, 0.03, 300), rng.normal(0.0005, 0.01, 300)]

    def test_exposure_le_1_and_positive(self):
        v = vol_target(self.r)
        self.assertTrue((v["exposure"] <= 1.0).all())
        self.assertTrue((v["exposure"] > 0).all())
        self.assertLess(v["exposure"][350:600].max(), 1.0)          # high-vol regime is scaled down

    def test_warm_up_exposure_is_one(self):
        v = vol_target(self.r)
        np.testing.assert_array_equal(v["exposure"][:oc.VT_WINDOW], 1.0)
        self.assertTrue(np.isnan(v["sigma"][:oc.VT_WINDOW]).all())
        self.assertTrue(np.isfinite(v["sigma"][oc.VT_WINDOW]))

    def test_exposure_formula_uses_t_minus_63_to_t_minus_1(self):
        v = vol_target(self.r)
        for t in (63, 64, 400, 899):
            s = np.std(self.r[t - 63:t], ddof=1) * np.sqrt(252)
            self.assertAlmostEqual(v["sigma"][t], s, places=12)
            self.assertAlmostEqual(v["exposure"][t], min(1.0, 0.15 / s), places=12)

    def test_perturbing_returns_from_t_does_not_change_exposure_at_t(self):
        v = vol_target(self.r)
        rng = np.random.default_rng(7)
        for t in (63, 200, 450, 700):
            r2 = self.r.copy()
            r2[t:] = rng.normal(0, 0.08, len(r2) - t)
            v2 = vol_target(r2)
            np.testing.assert_array_equal(v["exposure"][:t + 1], v2["exposure"][:t + 1])
            # and the change does show up afterwards
            self.assertFalse(np.allclose(v["exposure"][t + 1:], v2["exposure"][t + 1:]))

    def test_cost_on_exposure_change(self):
        v = vol_target(self.r)
        e, r = v["exposure"], self.r
        drift = np.r_[1.0, e[:-1] * (1 + r[:-1]) / (1 + e[:-1] * r[:-1])]
        np.testing.assert_allclose(v["cost"], oc.VT_COST * np.abs(e - drift), atol=1e-16)
        np.testing.assert_allclose(v["r"], e * r - v["cost"], atol=1e-16)
        self.assertGreater(v["cost"].sum(), 0)

    def test_cost_by_hand(self):
        # 63 quiet days (vol << 15 %) then a jump in vol halves the exposure
        r = np.r_[np.tile([0.001, -0.001], 40), np.tile([0.04, -0.04], 40)]
        v = vol_target(r)
        t = int(np.flatnonzero(v["exposure"] < 1)[0])
        e_prev, e_t = v["exposure"][t - 1], v["exposure"][t]
        self.assertEqual(e_prev, 1.0)
        drifted = e_prev * (1 + r[t - 1]) / (1 + e_prev * r[t - 1])     # = 1 when fully invested
        self.assertAlmostEqual(drifted, 1.0)
        self.assertAlmostEqual(v["cost"][t], 0.001 * (1 - e_t), places=15)
        self.assertAlmostEqual(v["r"][t], e_t * r[t] - 0.001 * (1 - e_t), places=15)

    def test_full_exposure_costs_nothing(self):
        r = np.tile([0.0005, -0.0004], 200)                         # vol ~ 0.7 % << 15 %
        v = vol_target(r)
        np.testing.assert_array_equal(v["exposure"], 1.0)
        np.testing.assert_array_equal(v["cost"], 0.0)
        np.testing.assert_array_equal(v["r"], r)

    def test_vt_off_is_identity(self):
        v = apply_vt(self.r, "off")
        np.testing.assert_array_equal(v["r"], self.r)
        np.testing.assert_array_equal(v["exposure"], 1.0)

    def test_zero_vol_window_full_exposure(self):
        r = np.r_[np.zeros(100), 0.01, -0.01]
        v = vol_target(r)
        np.testing.assert_array_equal(v["exposure"][:101], 1.0)


class TestSelection(unittest.TestCase):
    def grid(self, sample=config.SELECTION_SAMPLE):
        rows = []
        rng = np.random.default_rng(0)
        for w in oc.WEIGHTINGS:
            for c in oc.CAPS:
                for v in oc.VOL_TARGETS:
                    rows.append({"sample": sample, "weighting": w, "cap": c, "vol_target": v, "n_trades": 8716,
                                 "sharpe": rng.uniform(0.8, 1.1), "cagr": rng.uniform(0.15, 0.22),
                                 "volatility": rng.uniform(0.12, 0.22)})
        return pd.DataFrame(rows)

    def test_refuses_other_samples(self):
        g = pd.concat([self.grid(), self.grid("oos_continuous")])
        with self.assertRaises(ValueError):
            select_overlay(g)
        with self.assertRaises(ValueError):
            select_risk_reduction(g, 0.2)

    def test_primary_pick_is_max_sharpe_with_grid_order_ties(self):
        g = self.grid()
        best = g.loc[g["sharpe"].idxmax()]
        p = select_overlay(g)
        self.assertEqual((p["weighting"], p["cap"], p["vol_target"]), (best["weighting"], best["cap"], best["vol_target"]))
        g["sharpe"] = 1.0
        p = select_overlay(g)
        self.assertEqual((p["weighting"], p["cap"], p["vol_target"]), ("equal", "none", "off"))

    def test_risk_pick_lowest_vol_within_1pp(self):
        g = self.grid()
        g["cagr"] = 0.20
        g.loc[0, ["cagr", "volatility"]] = [0.25, 0.01]      # better CAGR by 5 pp: excluded by two-sided rule
        g.loc[1, ["cagr", "volatility"]] = [0.191, 0.05]     # within 1 pp
        g.loc[2, ["cagr", "volatility"]] = [0.18, 0.02]      # 2 pp worse: excluded
        p = select_risk_reduction(g, 0.20)
        self.assertEqual(p.name, 1)
        p1 = select_risk_reduction(g, 0.20, two_sided=False)
        self.assertEqual(p1.name, 0)


if __name__ == "__main__":
    unittest.main()
