"""Selection under alternative missing-reference handling; Mark Green weight over time; alpha/beta OOS."""
import numpy as np
import pandas as pd

from src.backtest import config
from src.backtest.engine import load_trades
from src.backtest.portfolio import x_label
from src.backtest.prices import load_pricebook
from src.backtest.run import Evaluator, build_signal_table

pd.set_option("display.width", 250)
trades = load_trades()
tmap = pd.read_csv(config.TICKER_MAP_CSV, keep_default_na=False, na_values=[])
pb = load_pricebook(set(tmap.loc[tmap["price_symbol"] != "", "price_symbol"]))
sig = build_signal_table(trades, tmap, pb)
sig["identical_row_repeat"] = False
ev = Evaluator(sig, pb)

take = lambda p: np.nan_to_num(p["rise_pct"].to_numpy(float) / 100.0, nan=-1.0)
excl = lambda tk, pr: np.isfinite(tk["rise_pct"].to_numpy(float))
variants = {"A_author_skip_missing": dict(), "B_take_missing": dict(rise_override=take),
            "C_drop_missing_for_all_X": dict(extra_mask=excl)}
for name, kw in variants.items():
    rows = []
    for rule in config.EXIT_RULES:
        for wt in config.WEIGHTINGS:
            for x in config.X_GRID:
                r = ev.evaluate("in_sample_cut2021", rule, wt, x, **kw)[0]
                rows.append(r)
    g = pd.DataFrame(rows)
    g = g[g.n_trades >= 200]
    best = g.sort_values("sharpe", ascending=False)
    bpair = best.groupby(["exit_rule", "weighting"]).head(1)
    print(f"\n== {name}: top 3")
    print(best[["exit_rule", "weighting", "X", "sharpe", "spy_sharpe", "n_trades"]].head(3).to_string())
    print("   best X per exit/weighting:", [(a, b, c) for a, b, c in zip(bpair.exit_rule, bpair.weighting, bpair.X)])
    c = best.iloc[0]
    xv = None if c.X == "none" else float(c.X)
    o = ev.evaluate("out_of_sample", c.exit_rule, c.weighting, xv, **kw)[0]
    print(f"   OOS of choice: total {o['total_return']:.4f} cagr {o['cagr']:.4f} sharpe {o['sharpe']:.4f} vs SPY {o['spy_total_return']:.4f}/{o['spy_sharpe']:.4f} n={o['n_trades']}")

# Mark Green share of OOS portfolio value (hold_12m amount)
s = sig[(sig["sample"] == "out_of_sample") & sig.entry_idx.notna()]
n = len(pb.cal)
tot = np.zeros(n); mg = np.zeros(n)
for r in s.itertuples(index=False):
    e, x = int(r.entry_idx), int(r.hold_12m_exit_idx)
    h = r.weight_amount * (1 - config.COST) / r.entry_open
    v = h * np.nan_to_num(pb.close_ff[e:x + 1, int(r.col)], nan=0)
    tot[e:x + 1] += v
    if r.member in ("Mark Green", "Mark Dr Green"):
        mg[e:x + 1] += v
share = pd.Series(np.where(tot > 0, mg / np.where(tot > 0, tot, 1), 0), index=pd.to_datetime(pb.cal))
share = share["2022-01-03":]
print("\nMark Green share of OOS portfolio value (hold_12m, amount): month-end samples")
print(share.resample("QE").last().round(3).to_string())
print("max share", share.max().round(3), "on", share.idxmax().date())

# OOS alpha / beta (daily), recommended config
row, dates, r, br = ev.evaluate("out_of_sample", "hold_12m", "amount", None)
X = np.c_[np.ones(len(br)), br]
coef, res, *_ = np.linalg.lstsq(X, r, rcond=None)
resid = r - X @ coef
se = np.sqrt(np.diag(resid.var(ddof=2) * np.linalg.inv(X.T @ X)))
te = (r - br).std(ddof=1) * np.sqrt(252)
ir = (r - br).mean() * 252 / te
print(f"\nOOS daily regression on SPY: alpha {coef[0]*252:.4f}/yr (t={coef[0]/se[0]:.2f}), beta {coef[1]:.3f}; "
      f"tracking error {te:.4f}, information ratio {ir:.3f}, t(IR)={ir*np.sqrt(len(r)/252):.2f}")
row, dates, r, br = ev.evaluate("out_of_sample", "hold_12m", "amount", None,
                                extra_mask=lambda tk, pr: ~tk.member.isin(["Mark Green", "Mark Dr Green"]))
X = np.c_[np.ones(len(br)), br]
coef, *_ = np.linalg.lstsq(X, r, rcond=None)
print(f"   excl. Mark Green: alpha {coef[0]*252:.4f}/yr beta {coef[1]:.3f}")
